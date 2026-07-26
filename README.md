# Invoice Compliance Agent

An agentic system that processes scanned PDF invoices, extracts structured fields, and verifies them against configurable compliance rules. Runs fully locally via Ollama — no cloud APIs, no data leaves the machine.

The agent reasons about what to do next at each step, picks the right tool, self-corrects on failure, and writes learnings to disk so future runs on the same invoice type are more accurate.

---

## How it works

### The agentic loop

```
┌─────────────────────────────────────────────────────────────┐
│                        Agent Loop                           │
│                                                             │
│  System prompt                                              │
│  (invoice type context, field hints, compliance rules,      │
│   tool descriptions, working-style guidance)                │
│           +                                                 │
│  State summary                                              │
│  (file info, page inventory, extracted fields, failed       │
│   rules, session notes, retry counts)                       │
│           +                                                 │
│  Last 15 actions                                            │
│           │                                                 │
│           ▼                                                 │
│   Reasoning model (qwen3:1.7b)                              │
│   returns: { tool, params, reasoning }                      │
│           │                                                 │
│           ▼                                                 │
│      Tool dispatch ──► Tool executes ──► Result             │
│           │                                 │               │
│           └────────── state updated ◄───────┘               │
│                                                             │
│  Repeat until: finish() called, max turns hit,              │
│  or unrecoverable error / loop detected                     │
└─────────────────────────────────────────────────────────────┘
```

The agent uses **two separate Ollama models**:
- **Reasoning model** (`qwen3:1.7b`) — text-only, drives the agentic loop: decides which tool to call next, reasons about results, writes learnings. Fast and cheap since it only sees text (state JSON + tool descriptions).
- **Vision model** (`qwen2.5vl:32b`) — multimodal, used for all image tasks: field extraction, document classification, page inventory, visual compliance checks.

This split means the reasoning loop stays fast while heavy vision work only happens when an image is actually needed.

### Two-phase processing

For multi-page documents, the agent follows a two-phase approach:

**Phase 1 — Structure scan (cheap)**
```
compress_pages(dpi=48, quality=30)   ← ALWAYS first, even for small files
       ↓
inventory_pages()   ← sends each low-res thumbnail to vision model
                      classifies as: INVOICE_HEADER | LINE_ITEMS | TOTALS |
                      SIGNATURE_STAMP | SUPPORTING_DOC | COVER_PAGE | BLANK
       ↓
classify_document_type()
```
This builds a page map before any expensive full-quality rendering. The agent knows exactly which page contains what before doing any extraction.

`compress_pages` must run before `inventory_pages` **even for small files** — the vision model processes thumbnails much faster than full-resolution pages, and the low-res copies are kept in a separate `compressed_page_paths` list in state so they remain available as the inventory source even after `convert_pdf_to_images` later overwrites `page_image_paths` with the full-quality render.

**Phase 2 — Targeted extraction (full quality)**
```
convert_pdf_to_images(dpi=150)
       ↓
extract_fields_vision()  ← targeted at pages identified in phase 1
       ↓
check_compliance()
       ↓
retry / crop / flag as needed
       ↓
check_compliance_visual()  ← stamps, signatures, etc.
       ↓
finish() + write_learning()
```

### OCR pre-pass

Before every `extract_fields_vision` call, the already-rendered page image is passed through **surya OCR**. The extracted text is injected into the vision model prompt as a "primary reference", so the model cross-checks its pixel reading against the OCR output. Especially useful for numbers, IBANs, and dates where visual misreads are common.

OCR languages are configured via `ocr.langs` in `config/config.yaml`. If you have French invoices, include `fr` (e.g. `["es", "en", "fr"]`).

Silently skipped if `surya-ocr` is not installed — install with `pip install surya-ocr`.

### Batch field extraction

When the agent calls `extract_fields_vision` for a subset of fields, the wrapper automatically expands the request to include every other field that has never been attempted. All un-tried fields get a free ride in the same vision model call, cutting total turns by 60–70% compared to extracting one field at a time.

### Compliance checks

Compliance rules fall into four tiers based on how they are evaluated:

---

**Tier 1 — Field-based rules** (run by `check_compliance`, pure Python, no LLM)

Rules evaluated deterministically against values already extracted into state:

| `check_type` | What it checks |
|------|---------------|
| `required` | Field must be non-null |
| `regex` | Value must match a pattern (e.g. NIF format) |
| `range` | Numeric value within bounds (e.g. VAT rate 0–30%) |
| `enum` | Value must be one of a fixed list |
| `cross_field` | Math relationship between fields (e.g. net × rate ≈ tax) |
| `conditional_check` | If field A = X, then field B must = Y |
| `required_one_of` | At least one of several fields must be present |

If a `cross_field` or `conditional_check` rule can't run because a required field value is missing, it is recorded as a **skipped check** — surfaced in the state summary and logs so the agent knows to extract the missing fields before finishing.

---

**Tier 2 — Visual/judgment rules** (run by `check_compliance_visual`, one batched LLM call)

Rules with `check_type = visual_check` are skipped by `check_compliance` and instead sent as a batch to the vision model together with a page image. Used for things that aren't extractable numeric fields:
- Physical stamps, seals, and signatures
- Official stamps with specific text (e.g. XUNTA DE GALICIA reference, expediente number)
- Subjective adequacy judgments (e.g. "is the expense description sufficient?")
- Payment method visibility

---

**Tier 3 — Multi-page cross-reference rules** (visual_check rules that use the page inventory)

Some rules require confirming that a second document is present within the same PDF — for example, a proof of payment page, a translation, or supplier quotes. Since `inventory_pages()` classifies every page as `INVOICE_HEADER`, `LINE_ITEMS`, `TOTALS`, `SIGNATURE_STAMP`, `SUPPORTING_DOC`, `COVER_PAGE`, or `BLANK`, these can be expressed as:

1. Check inventory for a `SUPPORTING_DOC` page (or `SIGNATURE_STAMP` for payment proof)
2. Send that page + the invoice page to `check_compliance_visual` to confirm they match

Examples:
- `proof_of_payment_attached` — is there a `SUPPORTING_DOC` or `SIGNATURE_STAMP` page whose amounts match the invoice?
- `documentation_match` — does the supporting page match the invoice date and amount?
- `translation_provided` — if language is non-standard (not ES/EN/FR/IT/PT), is there a second page with a translation?

---

**Tier 4 — Per-invoice threshold rules** (field-based, powered by `expense_category`)

Rules that check whether a specific invoice exceeds a cost limit, requires additional documentation, or is categorically ineligible. These are expressed as standard `conditional_check` and `range` rules in `compliance_rules.csv` and depend on the extracted `expense_category` field:

| Rule | Logic |
|------|-------|
| `audit_cost_limit` | if `expense_category = audit` → `total_amount ≤ 2500` |
| `evaluation_cost_limit` | if `expense_category = evaluation` → `total_amount ≤ 5000` |
| `three_quotes_required_works` | if `expense_category = works` AND `total_amount > 40000` → visual check for supplier quotes |
| `three_quotes_required_supplies_services` | if `expense_category = supplies/consulting/services` AND `total_amount > 15000` → same |
| `economy_class_flights_mandated` | if `expense_category = flight` → `ticket_class` must not be `Business` / `First Class` |
| `expatriate_housing_not_subsidized` | `expense_category = housing` → automatic error |
| `protocol_expenses_not_subsidized` | `expense_category = protocol` → automatic error |
| `dismissal_indemnities_not_subsidized` | `expense_category = dismissal_indemnity` → automatic error |
| `amortization_not_subsidized` | `expense_category = amortization` → automatic error |
| `procedure_code_mentioned` | `visual_check` — does "PR811A" appear anywhere in the document? |
| `project_execution_within_2023` | `regex` on `invoice_date` — must fall within 2023 |
| Eligibility rules (sanctions, judicial, protocol, etc.) | `conditional_check` — if `expense_category = X`, flag as ineligible error |

All 29 XUNTA DE GALICIA grant compliance rules are now configured in `compliance_rules.csv` and are active across all five invoice types (VIAJES, PERS_LOCAL, PERS_SEDE, EQUIPOS, CONSUMIBLES).

### Learning system

The agent improves across runs via `learnings/learnings.md`:

- At the **start** of each run, the agent loads learnings from `learnings/learnings.md` into `state.learnings_context` (GENERAL + the current invoice type).
  Injection into the system prompt is controlled by `agent.learnings_inject_enabled`.
- During a run, the agent calls `write_learning(category, content)` to record what worked and what didn't.
- In **learning mode** (`--learn`), after the normal run completes, a separate reflection loop receives the ground truth diff and writes targeted learnings about every field discrepancy.

Learnings are organised by invoice type and category:
```
## VIAJES
### approaches
- [2024-03-15] Use page 1 header for invoice_number, page 3 for totals
### extraction_patterns
- [2024-03-15] Tax ID always follows "NIF:" label in address block
### common_failures
- [2024-03-15] Vision model confuses reservation number with invoice number
```

### Loop guards

Several mechanisms prevent the agent from getting stuck. Guards live in `src/agent/loop_guards.py`:

| Guard | Trigger | Action |
|-------|---------|--------|
| **DuplicateActionGuard** | Same tool + same params called 2+ times in a row | Injects a `LOOP DETECTED` warning into session notes, visible in the next turn's state summary |
| **ConsecutiveFailureGuard** | Same tool fails 3 times in a row | Sets status to `ERROR`, stops the loop |
| **Max turns** | Turn count exceeds `agent.max_turns` | Sets status to `FAILED` |
| **Max field retries** | Field attempted more than `agent.max_field_retries` times | Agent is instructed to call `flag_for_human_review` instead |

---

## Setup

### Environment (Conda)

If you use a Conda env named `invoice-agent`:

```bash
conda activate invoice-agent
pip install -r requirements.txt
```

Or run one-off commands without activating:

```bash
conda run -n invoice-agent pip install -r requirements.txt
conda run -n invoice-agent python main.py --pdf invoices/
```

### Optional: `.env` for API keys

On startup, `main.py` loads a **`.env`** file from the **project root** (next to `main.py`), then from the **current working directory** (only variables not already set are filled in from the second file).

For Gemini, copy [`.env.example`](.env.example) to `.env` and set e.g. `GOOGLE_API_KEY=...` (or match `gemini.api_key_env` in `config/config.yaml`). For OpenAI, set `OPENAI_API_KEY=...` (or match `openai.api_key_env`). `.env` is gitignored.

### Optional: Google Drive invoice source

Google Drive ingestion reads PDFs from a Drive folder, downloads each PDF temporarily for processing, and deletes the downloaded copy after the run. Normal outputs, logs, rendered pages, and CSV files are preserved.

The app supports two authentication modes under `sources.google_drive.auth_mode`:

```yaml
sources:
  google_drive:
    auth_mode: oauth
```

```yaml
sources:
  google_drive:
    auth_mode: service_account
```

When `--pdf` is provided, local PDF processing is used. When `--pdf` is omitted and a Drive folder is configured, Drive ingestion is used.

#### OAuth mode

Use `auth_mode: oauth` for local browser-based authentication with a personal Google account.

1. Enable the **Google Drive API** in your Google Cloud project.
2. Configure an OAuth consent screen.
3. Create an OAuth client of type **Desktop app**.
4. Rename the downloaded JSON to:

```bash
.secrets/google-drive-oauth-client.json
```

The `.secrets/` folder and OAuth token files are gitignored. You can override the client JSON path with `GOOGLE_DRIVE_OAUTH_CLIENT_SECRET` or `--drive-oauth-client-secret`.

Authenticate once:

```bash
python main.py --drive-auth
```

OAuth access tokens refresh automatically. If the OAuth app remains in Google's Testing state, Drive refresh tokens may need re-authentication after 7 days; run `python main.py --drive-auth` again if that happens.

#### Service-account mode

Use `auth_mode: service_account` for non-browser local, Docker, CI, and AWS-style runs. A service account is a separate Google identity; it cannot see files in a personal Drive until those folders or files are explicitly shared with its `client_email`.

1. Enable the **Google Drive API** in your Google Cloud project.
2. Create a service account in Google Cloud IAM.
3. Create and download a JSON key for that service account.
4. Save the key locally as:

```bash
.secrets/google-drive-service-account.json
```

5. Set service-account auth in `config/config.yaml`:

```yaml
sources:
  google_drive:
    auth_mode: service_account
    service_account_file: ".secrets/google-drive-service-account.json"
    service_account_file_env: GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE
    service_account_json_env: GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON
```

`sources.google_drive.service_account_file` points to the local key file. `sources.google_drive.service_account_file_env` names the env var that can override the file path, usually `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE`. `sources.google_drive.service_account_json_env` names the env var that can contain the full JSON secret text, usually `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`; this takes priority when set.

6. Share the Drive invoice folder with the service account's `client_email` from the downloaded JSON key.
7. If Drive-backed configuration files or folders are used, share those config folders/files with the same `client_email` too. Share only the required folders/files, not broad personal Drive access.
8. Validate credentials without launching a browser:

```bash
python main.py --drive-auth
```

9. Process a Drive folder:

```bash
python main.py --google-drive-folder-id <folder-id>
```

Or set a default folder in `config/config.yaml` and omit the CLI folder flag:

```yaml
sources:
  google_drive:
    folder_url: "https://drive.google.com/drive/folders/<folder-id>"
```

Drive-backed config is separate from Drive PDF ingestion. If `sources.google_drive.config_folder.enabled: true`,
the app loads `invoice_types.csv`, `extraction_fields.csv`, `compliance_rules.csv`, and optional config files
from that Drive folder even when `--pdf` points to a local file. To force local `config_dir` CSVs for one run:

```bash
python main.py --pdf invoices/my_invoice.pdf --local-config
```

`--no-drive-config` is accepted as an alias for the same behavior.

Never commit service-account JSON keys. Keep `.secrets/google-drive-service-account.json` in `.secrets/`, pass the key through `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE`, or inject the secret text through `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`.

### Optional: Google Sheets workbook output

Normal local-folder and Google Drive batch runs write a canonical workbook CSV snapshot under `output/canonical_workbook/` in addition to the legacy per-invoice artifacts. Enable Google Sheets sync only when you want that same workbook snapshot uploaded after the local CSV files are written:

```yaml
output:
  google_sheets:
    enabled: true
    spreadsheet_id: "<existing-spreadsheet-id>"  # or use create_title below
    create_title: ""
    mode: replace
    value_input_option: RAW
    include_generated_views: true
```

Use exactly one target: `spreadsheet_id` to refresh an existing workbook, or `create_title` to create a new one. `include_generated_views: false` uploads only the raw `Invoice Summary` and `Compliance Results` tabs; `true` also uploads the generated matrix, review queue, and dashboard tabs. The writer uses replace semantics for managed tabs, so reruns refresh the current workbook snapshot instead of appending duplicate rows.

Upload pre-generated fixture CSVs without processing invoices:

```bash
python main.py --upload-workbook-csv-dir tests/fixtures/output --sheets-spreadsheet-id <spreadsheet-id>
python main.py --upload-workbook-csv-dir tests/fixtures/output --sheets-create-title "Invoice Review"
```

This fixture upload path bypasses config-store loading, `InvoiceAgent`, OCR, LLM providers, Drive PDF materialization, and invoice extraction.

### 1. Install Ollama
```
https://ollama.com
```

### 2. Pull models
```bash
ollama pull qwen3:1.7b
ollama pull qwen2.5vl:32b
```

Optional upgrades for better accuracy (requires more VRAM):
```bash
ollama pull qwen3:4b          # stronger reasoning than 1.7b
ollama pull qwen2.5vl:72b     # significantly higher extraction accuracy
```
Then update `config/config.yaml` to use the larger model names.

### 3. Install Python dependencies
```bash
pip install -r requirements.txt
```

Includes `google-genai` and `openai` SDKs for remote **Gemini** and **OpenAI** backends (API keys via `.env` or env vars).

### 4. Install surya OCR (optional but recommended)
```bash
pip install surya-ocr
```
Model weights (~300 MB) are downloaded automatically on first use.

---

## Running

```bash
# Single invoice, auto-detect type
python main.py --pdf invoices/my_invoice.pdf

# Single invoice, specify type
python main.py --pdf invoices/my_invoice.pdf --type VIAJES

# Batch — all PDFs in a folder
python main.py --pdf invoices/

# Batch — Google Drive folder
python main.py --google-drive-folder-id <folder-id>

# Learning mode — compare results to ground truth and write learnings
python main.py --pdf invoices/my_invoice.pdf --learn

# List all configured invoice types
python main.py --list-types

# Live demo — Rich phase-aware output (suppresses INFO logs)
python main.py --pdf invoices/my_invoice.pdf --presentation
```

Press `Ctrl+C` at any time to interrupt gracefully — partial results and the per-run log are saved.

### Presentation mode

For live demos or screen recordings, use `--presentation` (or set `logging.presentation: true` in `config/config.yaml`). This:

- Streams **phase banners** (SCAN → EXTRACT → VALIDATE) and human-readable tool labels to stdout
- Shows agent **reasoning** and per-step **elapsed time**
- Renders a **Rich summary panel** at the end instead of ASCII `===` boxes
- Sets console logging to **WARNING** so developer traces (`src.agent.*`, `src.tools.*`) stay quiet

The JSONL run log under `output/<invoice>/logs/` is unchanged — presentation mode only affects live terminal output.

---

## Docker

Run the agent in a container without installing Python on the host. First build downloads PyTorch and Surya OCR weights (~3–5 GB image; may take several minutes).

```bash
docker build -t invoice-agent .
```

**Gemini (recommended for quick testing):**

```bash
cp .env.example .env   # set GOOGLE_API_KEY
docker compose --profile gemini build
docker compose --profile gemini run --rm agent python main.py --pdf invoices/your.pdf
```

**Plain `docker run` (without Compose):**

```bash
docker run --rm -e GOOGLE_API_KEY=your-key \
  -v "${PWD}/invoices:/app/invoices" \
  -v "${PWD}/output:/app/output" \
  -v "${PWD}/learnings:/app/learnings" \
  invoice-agent python main.py --pdf invoices/your.pdf
```

**Ollama profile** (requires NVIDIA GPU + [Compose GPU support](https://docs.docker.com/compose/how-tos/gpu-support/)):

```bash
docker compose --profile ollama up -d ollama
docker exec -it $(docker compose --profile ollama ps -q ollama) ollama pull qwen3:1.7b
docker exec -it $(docker compose --profile ollama ps -q ollama) ollama pull qwen2.5vl:32b
docker compose --profile ollama run --rm agent-ollama python main.py --pdf invoices/your.pdf
```

Results land in `./output/` on the host. Ollama is for local dev only — AWS deployment uses Gemini.

---

## Docker

The service-account credential options are the same in containers as local runs:

- Mount a key file and set `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE`.
- Inject the full key JSON as `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` through the container runtime or managed secret platform.

For Docker Compose, keep the key at `.secrets/google-drive-service-account.json` on the host and mount `.secrets` read-only:

```bash
docker compose run --rm agent python main.py --drive-auth
docker compose run --rm agent python main.py --google-drive-folder-id <folder-id>
```

The included `docker-compose.yml` sets `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE=/app/.secrets/google-drive-service-account.json` and mounts `./.secrets:/app/.secrets:ro` for both `agent` and `agent-ollama`. Local PDF-only runs do not need the secret file unless `sources.google_drive.auth_mode: service_account` is selected.

Plain `docker run` with a mounted service-account file:

```bash
docker run --rm \
  -e GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE=/app/.secrets/google-drive-service-account.json \
  -v "${PWD}/.secrets:/app/.secrets:ro" \
  -v "${PWD}/invoices:/app/invoices" \
  -v "${PWD}/output:/app/output" \
  -v "${PWD}/learnings:/app/learnings" \
  -v "${PWD}/config:/app/config:ro" \
  invoice-agent:local python main.py --google-drive-folder-id <folder-id>
```

Plain `docker run` with secret text injected by the caller:

```bash
docker run --rm \
  --env GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON \
  -v "${PWD}/invoices:/app/invoices" \
  -v "${PWD}/output:/app/output" \
  -v "${PWD}/learnings:/app/learnings" \
  -v "${PWD}/config:/app/config:ro" \
  invoice-agent:local python main.py --google-drive-folder-id <folder-id>
```

Do not bake service-account JSON keys into container images or commit them to the repository.

### AWS managed secrets

For ECS, Lambda, or similar AWS deployments, store the service-account JSON key in AWS Secrets Manager or SSM Parameter Store. Prefer managed secret injection over committed files:

- Inject the full secret text into `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`.
- Or mount the managed secret as a file and set `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE` to that runtime path.

The app reads the injected env var or mounted file directly and does not need direct AWS SDK access for this phase.

---

## Run configurations

The agent behavior is controlled primarily by `agent.orchestration` in `config/config.yaml`.

### Baseline: deterministic `pipeline` (fixed sequence)
Use this when you want a stable “happy path” with minimal outer-loop variability.
- Set: `agent.orchestration: "pipeline"`
- Effect: uses `src/agent/pipeline.py` to run `inspect_file → compress_pages → inventory_pages → classify_document_type → convert_pdf_to_images → extract_fields_vision → check_compliance → check_compliance_visual(if needed) → finish`

Run:
```bash
python main.py --pdf invoices/
```

### Agentic: `loop` (LLM tool-calling with structured tools)
Use this when you want the model to decide the next structured tool call (with guards and phases).
- Set: `agent.orchestration: "loop"`
- Control which tools the LLM is allowed to call via:
  - `agent.tool_groups_enabled` (default `["pipeline"]`)
  - `agent.learnings_tools_enabled` (default `false`)

Run:
```bash
python main.py --pdf invoices/
```

### More granular-ish extraction: `loop` + `micro_tools_phase2`
Use this when dense/odd layouts cause extraction to miss fields; it reduces how many fields are requested per `extract_fields_vision` call.
- Set: `agent.orchestration: "loop"`
- Set: `agent.micro_tools_phase2: true`

Run:
```bash
python main.py --pdf invoices/
```

### Comparing them against each other
To compare fairly, run the same PDF set with `--learn` **off** (so learnings don’t change mid-experiment), then compare:
- `output/<invoice_stem>/summary.csv` (status/turns/fields/rule counts)
- `output/<invoice_stem>/logs/agent_log_*.jsonl` (tool sequence + reasoning)

---

## Configuration

Everything is driven by three CSV files in `config/csv/` and one YAML file. No code changes are needed to add invoice types, fields, or rules.

Tool catalog and mode/group exposure details are documented in [`docs/tools.md`](docs/tools.md).
Agent tool-selection, per-phase tool availability, and orchestration diagrams are documented in [`docs/architecture.md`](docs/architecture.md).

### `config/config.yaml`

```yaml
llm:
  provider: ollama   # or gemini (GOOGLE_API_KEY) or openai (OPENAI_API_KEY)
  # remote_guard: per-run caps for remote providers (see config/config.yaml for defaults)

ollama:
  base_url: "http://localhost:11434"
  vision_model: "qwen2.5vl:32b"    # used for extraction, inventory, visual checks
  reasoning_model: "qwen3:1.7b"    # used for the agent reasoning loop only

# gemini / openai: blocks live in config/config.yaml; set llm.provider accordingly.

agent:
  # Orchestration mode:
  # - "pipeline": deterministic fixed sequence (recommended baseline)
  # - "loop":     existing LLM tool-calling agent loop
  orchestration: "loop"

  max_turns: 25                    # loop mode only: hard stop on runaway agents
  max_field_retries: 3             # loop mode only: attempts per field before review
  confidence_threshold: 0.65       # minimum confidence to accept an extracted value

  # Tool exposure for loop mode (LLM tool enum + tool descriptions in prompts).
  # Defaults: pipeline tools enabled, learnings tools hidden, granular tools off.
  tool_groups_enabled: ["pipeline"]
  learnings_tools_enabled: false

  # Prompt sizing: auto → smaller caps for Ollama, larger for Gemini. Use null to take profile defaults.
  prompt_profile: auto
  learnings_inject_enabled: true
  learnings_max_chars: null
  planning_learnings_max_chars: null

  # Human-facing log preview sizing.
  log_line_max_chars: 120          # 0 = unlimited (console logs)
  history_preview_chars: null      # tool-output preview injected into the LLM
  visual_max_evidence_pages: 6     # max pages included in one visual compliance call
  hybrid_extraction: true          # medium-res first, auto full-res on weak/null/error (vision extract + visual compliance)

  # Batch field auto-expansion: include all un-attempted fields in every
  # extract_fields_vision call as a free-ride alongside the requested subset.
  # Disable only if you want to test targeted single-field extraction.
  batch_auto_expand: true

  # CSV ground truth fallback for --learn (used only if _truth.json is missing).
  ground_truth_csv_path: null
  ground_truth_source_column: "Source file"
  ground_truth_column_map: {}

ocr:
  langs: ["es", "en", "fr"]       # surya OCR languages for the pre-pass
```

### Customizing tool descriptions and phase mappings

Both can be edited without touching Python:

- **`config/tool_descriptions.yaml`** — maps tool names to replacement description strings shown in the system prompt. Only list tools you want to override; everything else uses the default text from `src/agent/prompts.py`. The path can be changed via `agent.tool_descriptions_path`.
- **`config/phase_tools.yaml`** — lists which tools are available in each phase (`SCAN`, `EXTRACT`, `VALIDATE`). Edit to add, remove, or reassign tools across phases. If the file is absent, the hardcoded fallback in `src/agent/phases.py` is used.

### `config/csv/invoice_types.csv`

Defines which document types the agent knows about.

| Column | Description |
|--------|-------------|
| `invoice_type_id` | Short identifier used everywhere (e.g. `VIAJES`) |
| `display_name` | Human-readable name |
| `description` | One-line description for the classification prompt |
| `agent_context` | Free-text guidance injected into the system prompt for this type |
| `enabled` | `true` / `false` |

### `config/csv/extraction_fields.csv`

Defines what the agent should extract from each invoice type.

| Column | Description |
|--------|-------------|
| `field_id` | Stable identifier used in compliance rules |
| `invoice_type_id` | Which type this field belongs to |
| `field_name` | Key name in extracted output |
| `field_label` | Human label shown to the vision model |
| `data_type` | `string` / `decimal` / `date` / `boolean` |
| `required` | Whether `check_compliance` expects it |
| `extraction_hint` | Plain-language hint injected into the vision model prompt |
| `page_region` | `header` / `footer` / `body` / `totals` / `address_block` / `line_items` |
| `aliases` | Comma-separated label variants to look for (e.g. `Factura Nº,Nº Factura,Ref`) |

**Standard fields extracted per type** (in addition to type-specific fields like `vendor_name`, `invoice_date`, `total_amount`, `net_amount`, `vat_amount`):

| Field | Types | Description |
|-------|-------|-------------|
| `expense_category` | All | Expense classification. Values depend on type: VIAJES → `flight / hotel / taxi / per_diem / train / bus / ferry / other_travel`; EQUIPOS → `equipment / furniture / IT_equipment / vehicle / supplies / audit / evaluation / consulting / works / other`; CONSUMIBLES → `office_supplies / printing / IT_consumables / cleaning_supplies / other_consumables`; PERS_LOCAL / PERS_SEDE → always `personnel` |
| `payment_method` | All | How the expense was paid: `bank_transfer / card / cash / cheque` |
| `ticket_class` | VIAJES only | Flight cabin class: `Economy / Business / First Class`. Null if not a flight ticket. |

### `config/csv/compliance_rules.csv`

Defines what constitutes a compliant invoice for each type.

| Column | Description |
|--------|-------------|
| `rule_id` | Stable identifier (e.g. `VAT_RATE_VALID`) |
| `invoice_type_id` | Which type this rule applies to |
| `check_type` | `required` / `regex` / `range` / `enum` / `cross_field` / `conditional_check` / `required_one_of` / `visual_check` |
| `check_value` | Type-specific payload: pattern, bounds, expression, etc. |
| `severity` | `error` (blocks pass) / `warning` (noted but doesn't block) |
| `agent_hint` | Guidance injected into the system prompt |
| `error_message` | Written to the compliance CSV on failure |
| `enabled` | `true` / `false` |

---

## Tools

The agent has access to these tools. It decides which to call at each turn.

| Tool | Description |
|------|-------------|
| `inspect_file()` | Read file metadata: size, page count, format. Tells the agent whether compression is advisable before rendering. |
| `compress_pages(dpi, quality, max_width)` | Render all pages at low resolution into `tmp/pages/`. Saves paths to `compressed_page_paths` in state — these are kept even after `convert_pdf_to_images` later overwrites `page_image_paths`. Call before `inventory_pages` for every document, regardless of file size. |
| `inventory_pages()` | Classify each page using the vision model. Automatically uses `compressed_page_paths` (low-res thumbnails) if available, falling back to `page_image_paths`. Returns a fixed category (`INVOICE_HEADER`, `LINE_ITEMS`, `TOTALS`, `SIGNATURE_STAMP`, `SUPPORTING_DOC`, `COVER_PAGE`, `BLANK`) plus a short description of what is literally visible. Stored in state and shown in every subsequent turn's state summary. |
| `classify_document_type()` | Send the first page to the vision model with all known type descriptions. Sets `state.invoice_type_id`. |
| `convert_pdf_to_images(dpi)` | Render all pages at full quality into `output/pages/`. Used in phase 2 for accurate extraction. |
| `crop_region(image_path, region, page_num)` | Crop a named region (`header`, `footer`, `totals`, `line_items`, `address_block`, `body`) or a custom bounding box from a page image. Useful when full-page extraction misses a specific area. |
| `extract_fields_vision(image_path, page_num, region, hints, field_subset)` | Send a page image to the vision model with the field schema. Returns values + per-field confidence scores. Automatically runs OCR pre-pass (surya) and expands `field_subset` to include all un-attempted fields. Merges results into state (only updates if new confidence is higher). Accepts any common alias for the image path (`page_path`, `page_image_path`, `image`, `path`, `file_path`) and for the page number (`page_index`, `page`, `page_number`). If only a page number is given and no path, the path is derived from the rendered page list automatically. |
| `check_compliance()` | Evaluate all field-based rules against current extracted values. Returns pass/fail per rule, failed error/warning lists, skipped checks (cross-field rules that couldn't evaluate due to missing fields), and any `visual_checks_pending`. |
| `check_compliance_visual(image_path, page_num)` | Evaluate all `visual_check` rules against a page image in a single vision model call. Returns per-rule verdicts with observations. Should be called after `check_compliance()` if `visual_checks_pending` is non-empty. |
| `flag_for_human_review(field_name, reason)` | Mark a field as needing human review. Called when max retries are exhausted or extraction is fundamentally ambiguous. |
| `note(text)` | Write a private observation into session memory (visible in state summary, not saved to disk). Used to record file-specific facts across turns without polluting learnings. |
| `read_learnings()` | Load past insights for the current invoice type from `learnings/learnings.md`. |
| `write_learning(category, content, invoice_type_id)` | Append a learning to `learnings/learnings.md`. Categories: `approaches`, `extraction_patterns`, `common_failures`, `compliance_edge_cases`, `tool_suggestions`. |
| `install_package(package)` | Pip-install a package into the active environment. Used for self-healing when a tool fails with an `ImportError`. |
| `finish(reason, all_errors_resolved)` | End the run. Reasons: `compliance_passed`, `max_retries`, `human_review_needed`, `unrecoverable_error`. |

---

## Output

Results are written to `output/<invoice_stem>/` after each run:

```
output/my_invoice/
  results_20240315_143022.csv       ← extracted field values + confidence + source page
  compliance_20240315_143022.csv    ← pass/fail per rule + messages
  pages/
    page_001.jpg                    ← full-quality rendered pages
    page_002.jpg
  crops/
    page1_header.jpg                ← region crops saved for debugging
  logs/
    agent_log_20240315_143022.jsonl ← full turn-by-turn JSONL log

output/summary.csv                  ← rolling summary across all runs
```

For folder and Google Drive batch runs, a batch-level canonical workbook snapshot is also written by default:

```
output/canonical_workbook/
  invoice_summary.csv
  compliance_results.csv
  compliance_matrix.csv
  review_queue.csv
  dashboard_status_counts.csv
  dashboard_rule_counts.csv
  dashboard_severity_counts.csv
```

The raw `invoice_summary.csv` and `compliance_results.csv` files are the stable normalized contract. The other CSVs are generated workbook views derived from those raw tables for review workflows. Canonical rows include source provenance and run identity for local and Google Drive documents. Failed invoices remain visible through the existing console and batch summary paths; they are not invented as canonical rows unless invoice processing produced a completed `AgentState`.

Single-file runs remain legacy-only: they write the existing per-invoice result, compliance, page, crop, and log artifacts, but do not create `output/canonical_workbook/`.

The JSONL log records every turn: tool called, params, result, reasoning, elapsed time. Useful for debugging runs post-hoc without re-running.

### Deterministic output tests

Run the output and integration tests without live Google APIs, OCR, LLM providers, Drive materialization, full invoice extraction, or package installs:

```bash
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/test_canonical_output.py tests/test_workbook_views.py tests/test_cli_sources.py -q
PYTHONDONTWRITEBYTECODE=1 pytest -p no:cacheprovider tests/test_cli_sources.py tests/test_canonical_output.py tests/test_workbook_views.py tests/test_google_sheets_output.py -q
```

The tests use synthetic `AgentState` fixtures plus fake CLI, workbook, and Sheets writer seams.

### Exit statuses

| Status | Meaning |
|--------|---------|
| `passed` | All error-level rules satisfied |
| `needs_review` | Passed rules but some fields were flagged for human review |
| `failed` | Some error-level rules still failing after all retries |
| `error` | Unrecoverable tool failure or LLM error |
| `interrupted` | User pressed Ctrl+C |

---

## Learning mode

Learning mode compares the agent output to ground truth and then writes targeted learnings.

Place a ground truth JSON file alongside each invoice:
```
invoices/my_invoice.pdf
invoices/my_invoice_truth.json
```

Alternatively, if `agent.ground_truth_csv_path` is set in `config/config.yaml`, `--learn`
will fall back to loading the matching row from the CSV when `<stem>_truth.json` is missing
(matching is driven by `agent.ground_truth_source_column` and `agent.ground_truth_column_map`).

Run with `--learn`:
```bash
python main.py --pdf invoices/my_invoice.pdf --learn
```

After the normal processing run, a second **reflection loop** starts. It receives the diff between the agent's extracted values and the ground truth, then writes targeted learnings for every discrepancy. These learnings are picked up on the next run for the same invoice type.

Truth file format:
```json
{
  "fields": {
    "invoice_number": "2024-1234",
    "invoice_date": "2024-03-15",
    "total_amount": "1250.00"
  },
  "compliance": {
    "VAT_RATE_VALID": "passed"
  }
}
```

---

## Project structure

```
invoice-agent/
├── main.py                     CLI entrypoint + batch runner
├── config/
│   ├── config.yaml             Runtime config (models, thresholds, OCR langs)
│   ├── phase_tools.yaml        Phase-to-tool mappings (edit without touching Python)
│   ├── tool_descriptions.yaml  Per-tool description overrides for the system prompt
│   └── csv/
│       ├── invoice_types.csv
│       ├── extraction_fields.csv
│       └── compliance_rules.csv
├── src/
│   ├── agent/
│   │   ├── agent.py            Orchestrator: run() sets up state and calls _run_agent_loop()
│   │   ├── state.py            AgentState dataclass — single mutable object for a run
│   │   ├── turn.py             Single LLM turn: prompt assembly, JSON schema, parse, retry
│   │   ├── registry.py         Thin assembler: wires tool factories → runtime tool dict
│   │   ├── phases.py           Phase detection; loads phase-to-tool map from phase_tools.yaml
│   │   ├── prompts.py          System prompt builder; tool descriptions loaded from YAML
│   │   ├── tool_policy.py      TOOL_GROUPS access control + allow/deny override merge
│   │   ├── loop_guards.py      DuplicateActionGuard + ConsecutiveFailureGuard
│   │   ├── param_resolver.py   PARAM_ALIASES + resolve_param() for LLM alias normalisation
│   │   ├── llm_payload.py      Shared build_payload() used by turn.py + reflection.py
│   │   ├── response_schema.py  build_response_schema() — dynamic JSON schema for LLM output
│   │   └── reflection.py       Post-run reflection loop for learning mode
│   ├── tools/
│   │   ├── tool_wrappers.py    All tool closure factories (make_inspect, make_extract, …)
│   │   └── tools.py            Low-level tool implementations + surya OCR helpers
│   ├── config/
│   │   └── loader.py           CSV config loader + ConfigStore + schema builder
│   ├── output/
│   │   └── writer.py           CSV result writer
│   └── learning/
│       └── evaluator.py        Ground truth diff + reflection loop support
├── learnings/
│   └── learnings.md            Persistent per-type learnings (auto-updated)
├── invoices/                   Drop PDFs here
└── output/                     Results written here
```

---

## Roadmap

### ✅ Completed

- **`expense_category`, `payment_method`, `ticket_class` fields** — added to `extraction_fields.csv` for all invoice types. `expense_category` drives all eligibility and cost-limit rules; `ticket_class` is flight-only.
- **29 XUNTA DE GALICIA compliance rules** — all four tiers now configured in `compliance_rules.csv` across all five invoice types. Covers stamps, grant references, expediente numbers, flight class, ineligible expense categories, cost limits, quotes requirements, execution dates, and more.
- **Skipped check surfacing** — `cross_field` and `conditional_check` rules that can't evaluate due to missing fields are now reported explicitly in logs, state summary, and output CSV rather than silently passing.

### Near-term: project metadata config

A `project_config.yaml` alongside `config.yaml` would allow grant-specific values to be configured without touching `compliance_rules.csv`, and unlock dynamic reference/deadline checks:

```yaml
# project_config.yaml (future)
grant_call_reference: "2023/001-XYZ"
expediente_number: "PR811A-2023-00042"
procedure_code: "PR811A"
execution_year: 2023
justification_deadline: "2024-03-31"
total_project_budget: 250000.00
```

The agent would load this at startup and inject relevant values into the system prompt and compliance rule `check_value` expressions — so the same rule file works for multiple grant projects without editing.

### Future: batch-level budget cap rules

Some rules cannot be checked per invoice — they require aggregating totals across all invoices in a batch and comparing against the total project budget:

| Rule | What it needs |
|------|--------------|
| `personnel_costs_cap` (≤ 70% of budget) | Sum of all personnel invoices across the batch |
| `indirect_costs_cap` (≤ 5% of budget) | Sum of all indirect-cost invoices |
| `field_operations_costs_cap` (≤ 5% of budget) | Sum of all field-operations invoices |

These would be implemented as a post-processing step that runs after `main.py` processes a full batch folder — reading the per-invoice `results.csv` files, summing by `expense_category`, and comparing against `total_project_budget` from `project_config.yaml`.

---

## Tests

```bash
pytest tests/
```

Tests cover config loading, all compliance check types, output CSV format, and learnings read/write. Ollama is not required to run tests.

**CI:** `.github/workflows/ci.yml` runs `pytest tests/` on every pull request and push to `main`. No API keys required.
