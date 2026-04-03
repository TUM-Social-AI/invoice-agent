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

Several mechanisms prevent the agent from getting stuck:

| Guard | Trigger | Action |
|-------|---------|--------|
| **Duplicate action guard** | Same tool + same params called 2+ times in a row | Injects a `LOOP DETECTED` warning into session notes, visible in the next turn's state summary |
| **Consecutive failure guard** | Same tool fails 3 times in a row | Sets status to `ERROR`, stops the loop |
| **Max turns** | Turn count exceeds `agent.max_turns` | Sets status to `FAILED` |
| **Max field retries** | Field attempted more than `agent.max_field_retries` times | Agent is instructed to call `flag_for_human_review` instead |

---

## Setup

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

# Learning mode — compare results to ground truth and write learnings
python main.py --pdf invoices/my_invoice.pdf --learn

# List all configured invoice types
python main.py --list-types
```

Press `Ctrl+C` at any time to interrupt gracefully — partial results and the per-run log are saved.

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

### `config/config.yaml`

```yaml
ollama:
  vision_model: "qwen2.5vl:32b"    # used for extraction, inventory, visual checks
  reasoning_model: "qwen3:1.7b"    # used for the agent reasoning loop only

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

  # Learnings auto-load + prompt sizing.
  learnings_inject_enabled: true   # include loaded learnings in the system prompt
  learnings_max_chars: 8000        # cap learnings loaded into state
  planning_learnings_max_chars: 800

  # Human-facing log preview sizing.
  log_line_max_chars: 120          # 0 = unlimited (console logs)
  history_preview_chars: 300      # tool-output preview injected into the LLM
  visual_max_evidence_pages: 6     # max pages included in one visual compliance call
  hybrid_extraction: true          # medium-res first, auto full-res on weak/null/error (vision extract + visual compliance)

  # CSV ground truth fallback for --learn (used only if _truth.json is missing).
  ground_truth_csv_path: null
  ground_truth_source_column: "Source file"
  ground_truth_column_map: {}

ocr:
  langs: ["es", "en", "fr"]       # surya OCR languages for the pre-pass
```

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

The JSONL log records every turn: tool called, params, result, reasoning, elapsed time. Useful for debugging runs post-hoc without re-running.

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
│   └── csv/
│       ├── invoice_types.csv
│       ├── extraction_fields.csv
│       └── compliance_rules.csv
├── src/
│   ├── agent/
│   │   ├── agent.py            Agentic loop, tool registry, system prompt builder
│   │   └── state.py            AgentState dataclass — single mutable object for a run
│   ├── tools/
│   │   └── tools.py            All tool implementations + surya OCR helpers
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
