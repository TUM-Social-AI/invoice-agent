# Invoice Compliance Agent: Deployment, Configuration and Implementation Report

Report date: 2026-10-03
Repository: `TUM-Social-AI/invoice-agent`
Working tree inspected: local `main` at `ad2b52b`, plus `origin/main` (`16f7006`) and the unmerged branches, read through git.

---

## 1. Executive summary

The Invoice Compliance Agent takes scanned PDF invoices, extracts structured fields, and checks them against a configurable rulebook. The rulebook is built for XUNTA DE GALICIA-style grant auditing. A reasoning model chooses tools one step at a time, and a vision model reads the pages. The output is per-invoice CSVs, a batch "reviewer workbook" and an optional Google Sheets sync.

**Where it stands today**

| Area | Status |
|---|---|
| Core agent (loop and deterministic pipeline), tools, compliance engine | Implemented and tested. 247 tests pass and 15 are skipped (conda env `invoice-agent`, Python 3.12). |
| LLM backends | Ollama, Gemini and OpenAI are implemented. The checked-in default is **OpenAI**. |
| Inputs | Local PDFs and Google Drive folders. Drive auth supports OAuth on local `main` and OAuth plus service account on `origin/main`. |
| Outputs | Per-invoice CSVs, a canonical workbook CSV set, and a Google Sheets writer. |
| Container | A `Dockerfile` and a `docker-compose.yml` with `gemini` and `ollama` profiles. |
| CI | GitHub Actions runs `pytest` only. It does not build the image. |
| Scheduled AWS worker | The code is on `origin/main` (poll loop, DynamoDB dedup). **It cannot process Drive documents yet**, because the real Drive adapter raises `NotImplementedError`. |
| AWS infrastructure (CDK) | Written and synth-tested, but **reverted from `main`**. It survives on the `aws-cdk-infra` branch only. |

**The three most important findings**

1. **There is no working production deployment path yet.** The worker, the CDK stack and service-account auth were built on separate branches and only partly joined. The missing piece is a `GoogleDriveAdapter` that connects the worker to the Drive functions (section 6.5).
2. **Local `main` is two commits behind `origin/main`.** Those commits are the worker (PR #29) and service-account auth (PR #30). Pull before building anything on top.
3. **Documentation has drifted from the code.** The README and `docs/tools.md` describe the project as Ollama-only or Gemini-default. The actual `config/config.yaml` defaults to OpenAI with Drive and Google Sheets output switched on (section 9).

---

## 2. How this report was produced

- I read the files on local `main`: `README.md`, `Dockerfile`, `docker-compose.yml`, the CI workflow, `config/`, `docs/`, `.planning/`, `main.py` and the main modules in `src/`.
- I read `origin/main`, `origin/aws-cdk-infra` and the local `mvp-testing-prep` branch with `git show` and `git diff`. They hold the worker, service-account auth, the CDK stack and a few dev tweaks.
- I ran the test suite in the project's conda env.
- I did **not** open `.env`, which holds secrets. I did not run the agent against live LLM or Google APIs, so no end-to-end claims are made.
- I did not run `docker build` or `cdk synth`. The CDK validation claim comes from the commit message.

**Branch map**

| Branch | What it adds over local `main` | Merged to `origin/main`? |
|---|---|---|
| `origin/main` | Orchestration worker (#29) and Drive service-account auth (#30) | n/a (it is the head) |
| `origin/aws-cdk-infra` | CDK stack (`infra/`) with 3 follow-up fixes | **No.** The original commit was reverted on `main` (`6fe1326`). |
| `mvp-testing-prep` (local only) | Worker, service-account auth, a crash-log writer in `main.py`, `.gitignore` additions, and `boto3`/`moto` requirements | No (not pushed) |
| `openai-abstraction`, `source-provenance-inputs`, `presentation-logs`, `google-drive-source` | Earlier feature work, mostly absorbed into `main` | Mostly yes |

---

## 3. System architecture

### 3.1 End-to-end flow

```
            ┌──────────────────────── main.py (CLI) ─────────────────────────┐
 Sources    │ parse flags → load .env + config.yaml → load CSV rulebook       │
 local dir ─┤   (local config/csv OR a Google Drive config folder)            │
 Drive     ─┤ → materialize PDFs (with provenance + run identity)             │
            └───────────────┬─────────────────────────────────────────────────┘
                            ▼
                    InvoiceAgent.run()
        ┌── orchestration = "loop"  : LLM picks the next tool each turn
        └── orchestration = "pipeline": fixed sequence
                            │
   SCAN ───────► EXTRACT ───────► VALIDATE ───────► finish
 inspect_file   convert_pdf_to_images  check_compliance          (status)
 compress_pages extract_fields_vision  check_compliance_visual
 inventory_pages crop_region           flag_for_human_review
 classify_document_type
                            │
                            ▼
        write_results() ─► per-invoice CSVs + JSONL log
        batch only ─────► canonical workbook CSVs ─► optional Google Sheets sync
```

### 3.2 Layers (from `src/`)

| Layer | Location | Responsibility |
|---|---|---|
| Entry point | `main.py` (865 lines) | CLI, batch loop, learning mode, workbook and Sheets hand-off |
| Config | `src/config/loader.py`, `src/models/config_models.py` | Loads the CSV rulebook into a typed `ConfigStore` and builds the extraction schema |
| Sources | `src/sources/{local,google_drive,models,run_identity}.py` | Discovers and downloads documents. Provenance and run identity are attached to every document. |
| Agent | `src/agent/*` | State, turn loop, phases, prompt builder, action contract, loop guards, fallbacks, reflection |
| Tools | `src/tools/*` | PDF rendering, OCR, page inventory, vision extraction, compliance checks, learnings store |
| LLM | `src/llm/*` | `LLMProvider` protocol, three providers, and a metering wrapper |
| Output | `src/output/*` | Per-run CSVs, canonical schema, workbook views, Google Sheets writer, Rich presenter |
| Learning | `src/learning/evaluator.py`, `src/learnings/` | Ground-truth scoring and the learnings markdown store |
| Orchestration (`origin/main`) | `src/orchestration/*` | Scheduled poll worker with durable dedup state |

Design rules visible in the code and `.planning/codebase/ARCHITECTURE.md`:

- Business rules live in CSV, not in Python.
- All model access goes through `LLMProvider`.
- A single mutable `AgentState` carries one run.
- The loop and the pipeline share the same tool primitives.

---

## 4. Implementation details

### 4.1 The agent

**Two orchestration modes** (`agent.orchestration`):

- `loop` is the agentic mode and the checked-in default.
  - Each turn builds a system prompt, a state summary and the recent action history.
  - The model must return `{tool, params, reasoning}` against a dynamic JSON schema.
  - `action_contract.py` validates the action and allows one repair retry.
  - `registry.py` dispatches the call to the matching tool wrapper.
- `pipeline` is a deterministic debug and baseline mode. The sequence is `inspect_file → compress_pages → inventory_pages → classify_document_type → convert_pdf_to_images → extract_fields_vision → check_compliance → check_compliance_visual → finish`.

**Phase gating.** `config/phase_tools.yaml` lists the tools allowed in `SCAN`, `EXTRACT` and `VALIDATE`. Before classification the allowed set is tightened further to `compress_pages → inventory_pages → classify_document_type`. `src/agent/tool_policy.py` merges tool groups (`pipeline`, `granular`, `learnings`) with the allow and deny lists. `finish` and `note` are always exposed.

**Planning.** When `planning_enabled` is true, a plan is generated after classification. It is advisory only and is not enforced.

**Loop guards** (`loop_guards.py`):

- `DuplicateActionGuard` warns after 2 identical consecutive calls and stops after 5.
- `ConsecutiveFailureGuard` stops a tool after 3 back-to-back failures.
- `max_turns` is 30 and `max_field_retries` is 5.
- When the reasoning backend fails, `fallbacks.py` applies a deterministic fallback action if one exists. Otherwise the run is marked `ERROR`.

**Statuses:** `passed`, `needs_review`, `failed`, `error`, `interrupted`.

### 4.2 Tools

The demo deck cites 18 tools. In `config/config.yaml`, `install_package` is denied by `tools_extra_deny`.

| Group | Tools |
|---|---|
| Document prep | `inspect_file`, `compress_pages`, `convert_pdf_to_images`, `crop_region` |
| AI extraction | `inventory_pages`, `classify_document_type`, `extract_fields_vision`, `check_compliance_visual` |
| Deterministic | `check_compliance` |
| Memory and control | `read_learnings`, `write_learning`, `edit_learning`, `delete_learning`, `note`, `flag_for_human_review`, `flag_fields_for_review`, `install_package` (denied), `finish` |

Key behaviours:

- **Two-phase scan.** Thumbnails (low DPI) are inventoried first. Full-quality rendering (`page_dpi: 200`) comes only in the extract phase.
- **OCR pre-pass.** Surya OCR text (`ocr.langs: es, en, fr`) is injected into extraction prompts, capped at `ocr_prompt_max_chars: 24000`.
- **Batch auto-expansion.** Every un-attempted field rides along in each `extract_fields_vision` call.
- **Confidence-aware merge.** A new value only replaces an old one if its confidence is higher. Values below `confidence_threshold` (0.65) get a blocking review flag. Values between 0.65 and `batch_review_threshold` (0.85) get a non-blocking flag.
- **Inventory batching.** `inventory_batch_size: 5` pages per vision call.
- **Visual compliance.** Multi-page evidence, capped at `visual_max_evidence_pages: 8`.

### 4.3 Compliance engine

The rulebook in `config/csv/` currently holds:

| Item | Count |
|---|---|
| Invoice types | 5 (VIAJES, PERS_LOCAL, PERS_SEDE, EQUIPOS, CONSUMIBLES) |
| Extraction fields | 62 (15 / 11 / 11 / 14 / 11 by type) |
| Compliance rules | 103 (VIAJES 26, EQUIPOS 25, PERS_LOCAL 18, PERS_SEDE 17, CONSUMIBLES 17) |
| Rule check types | 73 `visual_check`, 22 `required`, 4 `cross_field`, 3 `range`, 1 `enum` |
| Rule severity | 69 error, 34 warning |
| Rule groups | `general` 60, `xunta_galicia` 43 |
| Allowed-value rows | 36 |
| Ground-truth rows | 23 |

- **Rule groups.** Only `general` is active (`agent.active_rule_groups: [general]`), so the Galicia-specific stamp, 2023-window and PR811A rules are off. The comment in `config.yaml` says this is for a Chad field-office corpus.
- **Tiers.** Deterministic field rules run in pure Python (`compliance_eval.py`). Visual rules go in one batched vision call (`compliance_visual.py`). Cross-page rules use the page inventory. Threshold rules key off `expense_category`.
- **Skipped checks.** Cross-field rules that cannot run because of missing fields are recorded as skipped and block a clean pass.

### 4.4 LLM providers

`src/llm/factory.py` selects the provider from `llm.provider`.

| Provider | Reasoning model | Vision model | Key env var |
|---|---|---|---|
| `openai` (**default**) | `o3-mini` | `gpt-4.1-mini` | `OPENAI_API_KEY` |
| `gemini` | `gemini-2.5-flash` | `gemini-2.5-pro` | `GOOGLE_API_KEY` |
| `ollama` | `qwen3:1.7b` (docs) | `qwen2.5vl:7b` in `config.yaml`, `32b` in `config.docker-ollama.yaml` | none |

- **Cost guard.** `MeteredLLMProvider` wraps the remote providers when any `llm.remote_guard` limit is set. The checked-in limits are: 45 chat requests, 150 generate requests, 220 total requests, and 400,000 tokens per run, with a warning at 120,000.
- **Timeouts.** Remote providers use a 600 s generate timeout. Ollama uses 240 s chat and 420 s generate.
- **OpenAI.** Retries 429s with exponential backoff, up to 5 attempts and a 60 s maximum wait (comment in `config.yaml`).
- **Prompt sizing.** `prompt_profile: auto` selects larger caps for remote providers.

### 4.5 Sources, provenance and cleanup

- **Local.** `src/sources/local.py` accepts a file or a folder.
- **Google Drive** (`src/sources/google_drive.py`):
  - It lists direct PDF children of a folder and supports Shared Drives. Revision ID is taken as `headRevisionId`, then `md5Checksum`, then `modifiedTime`.
  - It downloads each PDF through a `.part` file to `output/materialized/google_drive/<run_id>/` and builds provenance and a SHA-256.
  - It deletes the downloaded PDF afterwards (`cleanup_downloads: true`). Outputs, logs and pages are kept.
  - Output directories are collision-safe: `output/<stem>-<source_hash>`.
- **Drive-hosted config.** `sources.google_drive.config_folder.enabled: true` loads the rulebook CSVs from a Drive folder, even when processing local PDFs. `--local-config` forces local CSVs.
- **Default-source behaviour.** If `--pdf` is omitted and a Drive folder is configured, **Drive is the default source**.

### 4.6 Outputs

**Per invoice** (`src/output/writer.py`):

- `results_<ts>.csv`
- `compliance_<ts>.csv`
- `pages/`
- `crops/`
- `logs/agent_log_<ts>.jsonl`
- a rolling `summary.csv`

**Batch** (folder or Drive runs only):

- `output/canonical_workbook/` holds the CSV tables listed below. Single-file runs write none of these.
- `canonical.py` defines the stable raw schema (`schema_version: "1"`). Compliance results are long-format, one row per invoice and rule.
- `workbook.py` generates the reviewer-facing views.

| Table | Visibility in Sheets |
|---|---|
| Dashboard, Review, Review Issues, Invoice Summary, Compliance Matrix, Rule Guide | Visible |
| Compliance Results, Technical Run Data (the raw tables) | Hidden by default |

**Google Sheets** (`src/output/google_sheets.py`, 919 lines):

- Target is an existing `spreadsheet_id` or a `create_title`.
- Modes are `append` or `replace`. The checked-in config uses **`append`**.
- It reuses the Drive credentials, with retry on 429/5xx and transient network errors.
- `--upload-workbook-csv-dir` uploads fixture CSVs without running any invoices.

### 4.7 Learning system

- `learnings/learnings.md` (about 11 KB) is hydrated into the prompt at the start of each run, capped by `learnings_max_chars: 16000`.
- Categories are `approaches`, `extraction_patterns`, `vision_model_extraction`, `common_failures`, `compliance_edge_cases` and `tool_suggestions`.
- `--learn` compares the result to `_truth.json` or a row of `ground_truth.csv`. It then runs a reflection loop that writes new learnings.
- `--reset-learnings` backs up to `.bak` first.
- The ground-truth column mapping is in `config.yaml` under `ground_truth_column_map`.

### 4.8 Observability

- Python logging at the level set in `logging.level`, with a Rich "presentation" mode (`logging.presentation: true` in the checked-in config).
- A JSONL turn log per run records tool, params, reasoning, result and elapsed time.
- `main.py` redacts `?key=` query strings from urllib3 logs.
- The local `mvp-testing-prep` branch adds a crash-log writer to `output/_crash_logs/`.
- No external error tracker, metrics or tracing exists.

### 4.9 Scheduled worker (`origin/main`, `src/orchestration/`)

```
EventBridge ─► Fargate task: python -m src.orchestration.worker  (one poll, then exit)
   list_input_documents ─► try_claim(key = sourceId#revisionId) ─► materialize
   ─► agent.run ─► write_results ─► upload_results ─► mark_done / mark_failed ─► cleanup
```

- **State machine** (`dedup.py`): `processing → done`, `processing → failed` (retry), and `failed → needs_attention` once `max_attempts` (3) is exhausted. A `processing` claim older than `stale_claim_seconds` (1800) is reclaimed. Attempts are counted at claim time, so a crashed task still burns an attempt.
- **Stores.**
  - `DynamoDbDedupStore` uses partition key `pk` and an optimistic version guard (conditional put). A losing racer is reported as "raced".
  - `InMemoryDedupStore` is for local runs and tests.
- **Idempotence.** A replaced file (new revision) is reprocessed. An unchanged one is skipped.
- **Cap.** `max_files_per_poll: 50` bounds token spend per run.
- **Outcome.** `agent_runner.py` maps results to "ok" or "needs_review". An agent `ERROR` raises so the file can be retried. A needs-review result is uploaded normally with a status marker.
- **Local rehearsal.** `DRIVE_CLIENT=local LOCAL_INPUT_DIR=invoices LOCAL_OUTPUT_DIR=output/drive python -m src.orchestration.worker`.
- **Not done.** `build_drive_client()` raises `NotImplementedError` for the real Drive source. See section 9.

---

## 5. Configuration reference

### 5.1 `config/config.yaml` (checked-in defaults)

| Block | Key settings |
|---|---|
| `llm` | `provider: openai`, `remote_guard` (45 / 150 / 220 requests, 400k tokens) |
| `agent` | `orchestration: loop`, `loop_profile: patterned`, `max_turns: 30`, `max_field_retries: 5`, `planning_enabled: true`, `tool_groups_enabled: [pipeline]`, `tools_extra_deny: [install_package]`, `active_rule_groups: [general]`, `inventory_batch_size: 5`, `page_dpi: 200`, `hybrid_extraction: false`, `batch_auto_expand: true` |
| `ocr` | `langs: [es, en, fr]` |
| `output` | `base_dir: output`, `include_page_crops: true`, `google_sheets.enabled: true` (mode `append`, existing spreadsheet ID) |
| `sources.google_drive` | `auth_mode: oauth`, a default input folder URL, scope `https://www.googleapis.com/auth/drive`, `cleanup_downloads: true`, `include_shared_drives: true`, `page_size: 100`, `config_folder.enabled: true` |
| `logging` | `level: INFO`, `presentation: true` |

`config/config.docker-ollama.yaml` is a trimmed copy for the compose `ollama` profile. It points at `http://ollama:11434` and uses the 32b vision model. It has no Drive or Sheets blocks.

**Added on `origin/main` (not in the local working tree):**

- `sources.google_drive.auth_mode_env: GOOGLE_DRIVE_AUTH_MODE`
- `service_account_file` and `service_account_file_env`
- `service_account_json_env`
- an `orchestration:` block with `max_files_per_poll: 50`, `max_attempts: 3`, `stale_claim_seconds: 1800`, `dedup_backend: dynamodb`, `dynamodb_table: invoice-agent-dedup`, `aws_region: null` and `output_root: output`

### 5.2 Environment variables

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Key when `llm.provider: openai` |
| `GOOGLE_API_KEY` | Key when `llm.provider: gemini` |
| `CONFIG_PATH` | Alternative config file (default `config/config.yaml`) |
| `GOOGLE_DRIVE_OAUTH_CLIENT_SECRET` | Overrides the OAuth client JSON path |
| `GOOGLE_DRIVE_AUTH_MODE` | `oauth` or `service_account` (origin/main) |
| `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE` | Path to the service-account key (origin/main) |
| `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON` | Full key JSON text. It wins over the file. (origin/main) |
| `OLLAMA_HOST` | Set by the compose `agent-ollama` service (origin/main) |
| `DRIVE_CLIENT`, `LOCAL_INPUT_DIR`, `LOCAL_OUTPUT_DIR` | Worker local-rehearsal switches |

`main.py` loads `.env` from the project root and then from the current directory. `.env.example` lists only the two API keys, plus the Drive variables on `origin/main`.

### 5.3 Secrets and ignored paths

- `.gitignore` excludes `.env`, `.secrets/`, `client_secret*.json`, `google-drive-oauth*.json`, `google-drive-token*.json`, `/output/`, and PDFs under `invoices/`.
- Expected local files: `.secrets/google-drive-oauth-client.json` and `.secrets/google-drive-token.json`, or `.secrets/google-drive-service-account.json`.
- `.secrets/` does not currently exist in this checkout, so Drive auth has not been set up here.

### 5.4 CLI reference (`main.py`)

| Flag | Purpose |
|---|---|
| `--pdf PATH` | A file or a folder of PDFs. Cannot be combined with `--google-drive-folder-id`. |
| `--google-drive-folder-id ID` | Process a Drive folder |
| `--drive-auth` | Authenticate Drive and exit. Opens a browser in OAuth mode. |
| `--drive-oauth-client-secret PATH` | Override the OAuth client JSON |
| `--type TYPE` | Force an invoice type |
| `--output DIR` | Output root (default `output`) |
| `--config PATH` | Config file (default from `CONFIG_PATH`) |
| `--local-config` / `--no-drive-config` | Use local CSVs even if a Drive config folder is enabled |
| `--list-types` | List the types with field and rule counts |
| `--learn` | Learning mode against ground truth |
| `--reset-learnings [TYPE [CATEGORY]]`, `--yes` | Reset learnings (with a backup) |
| `--batch-summary-csv PATH` | Batch accuracy summary |
| `--presentation` | Rich live-demo output |
| `--upload-workbook-csv-dir`, `--sheets-spreadsheet-id`, `--sheets-create-title` | Upload fixture CSVs to Sheets without processing |

---

## 6. Deployment

### 6.1 Local (Conda)

```bash
conda activate invoice-agent
pip install -r requirements.txt
python main.py --pdf invoices/ --local-config
```

- `.vscode/launch.json` has two launch configurations that use the conda interpreter. They list invoice types and process a sample PDF, both with `--local-config --presentation`.
- Ollama needs `ollama pull qwen3:1.7b` and the vision model. OpenAI and Gemini only need an API key.
- Surya OCR weights (about 300 MB) download on first use.
- The conda env here runs Python 3.12. CI and the Docker image use 3.11.

### 6.2 Docker image (`Dockerfile`)

- Base `python:3.11-slim-bookworm`.
- System libraries: `libgomp1`, `libglib2.0-0`, `libsm6`, `libxext6`, `libxrender1`.
- Copies `main.py`, `src/`, `config/` and `learnings/`. It does **not** copy `.secrets/`, `.env` or `output/`.
- Creates non-root `appuser` (uid 1000).
- **Preloads Surya models at build time**, so the image is about 3–5 GB and the build needs network access.
- Default `CMD` is `python main.py --help`. The AWS task overrides the command.
- `.dockerignore` excludes `.git`, `.env*`, `output/`, `invoices/`, `tests/`, `docs/`, `presentation.html` and `*.md` (except the learnings file).

### 6.3 Docker Compose (`docker-compose.yml`)

| Service | Profile | Notes |
|---|---|---|
| `agent` | `gemini` | Uses `config/config.yaml`. Loads `.env`. |
| `agent-ollama` | `ollama` | Uses `config/config.docker-ollama.yaml`. Depends on the `ollama` service. |
| `ollama` | `ollama` | `ollama/ollama:latest`, port 11434, NVIDIA GPU reservation, volume `ollama_data` |

- Volume mounts are `./invoices`, `./output`, `./learnings` and `./config:ro`.
- `origin/main` adds the secrets mount `./.secrets:/app/.secrets:ro` and the `GOOGLE_DRIVE_SERVICE_ACCOUNT_FILE` variable.
- The profile is named `gemini`, but the checked-in `config.yaml` selects **OpenAI**. Under that profile, only `OPENAI_API_KEY` is needed.
- The compose header comment says Docker is "not used for AWS deployment". The Dockerfile comment says it is also the AWS worker image.

### 6.4 CI (`.github/workflows/ci.yml`)

- Triggers: every pull request and every push to `main`.
- Steps: checkout, Python 3.11 with pip caching, `pip install -r requirements.txt`, `pytest tests/ -q`.
- It needs no secrets.
- It does not run lint, type checks, coverage, a Docker build or any live integration test.
- There is no CD pipeline. Nothing builds or pushes the image automatically.

### 6.5 AWS target architecture

**Worker (on `origin/main`)** is described in section 4.9. **Infrastructure (CDK, `origin/aws-cdk-infra`, `infra/`):**

| Resource | Configuration |
|---|---|
| Region | `eu-central-1` (Frankfurt) by default. Override with `-c region=…`. |
| VPC | Minimal and NAT-free: 2 AZs, public subnets only. An existing VPC can be imported with `-c existingVpcId=…`. |
| Security group | No inbound rules. All outbound allowed. |
| Compute | ECS Fargate task, 2048 CPU units, 8192 MiB, 30 GiB ephemeral storage. The container runs `python -m src.orchestration.worker`. |
| Trigger | EventBridge rule, default `rate(10 minutes)`, with `assign_public_ip=True` so the task can reach the internet without a NAT |
| Dedup state | DynamoDB `invoice-agent-dedup`: partition key `pk`, on-demand billing, point-in-time recovery, `RETAIN` removal policy |
| Secrets | Secrets Manager `invoice-agent/llm-api-key` and `invoice-agent/drive-service-account`. Both are created empty. |
| Registry | ECR repository `invoice-agent`, scan on push, keeps the last 10 images |
| Logs | CloudWatch group `/invoice-agent/worker`, 1-month retention |
| Task environment | `CONFIG_PATH=config/config.yaml`, `GOOGLE_DRIVE_AUTH_MODE=service_account`, `LLM_PROVIDER` (informational) |
| Secret injection | The LLM secret goes in under `OPENAI_API_KEY` by default. The Drive secret goes in as `GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON`. |

Context keys: `region`, `existingVpcId`, `scheduleRate`, `llmProvider`, `llmKeyEnvName`, `imageTag`, `dedupTableName`, `cpu`, `memoryMib`.

**Deploy runbook (from `infra/README.md`)**

1. One-time per account and region: `cdk bootstrap`.
2. `cd infra && pip install -r requirements.txt && cdk deploy`. This creates the ECR repository and everything else.
3. Log in to ECR, then `docker build -t <ecr-uri>:latest ..` and `docker push`.
4. Fill the secrets with `aws secretsmanager put-secret-value`, one command for the LLM key and one for the Drive service-account JSON.
5. The next schedule tick launches the worker. Watch `/invoice-agent/worker`, or start a run from the ECS console.

**Cost profile** (from `infra/README.md`): the fixed cost is close to zero (no NAT, DynamoDB on-demand, scale-to-zero compute). LLM tokens dominate.

---

## 7. Testing and quality

**Result:** `247 passed, 15 skipped in 7.23 s` on local `main` in the conda env. The system Python 3.14 has no `rich`, so the suite cannot be collected there.

| Area | Test files |
|---|---|
| Sources and CLI | `test_sources_google_drive`, `test_sources_local`, `test_cli_sources`, `test_run_identity` |
| Providers | `test_openai_provider`, `test_gemini_provider`, `test_response_format`, `test_timeouts_prompt_guard` |
| Agent | `test_turn_action_contract`, `test_action_contract_page_num`, `test_agent_fallback`, `test_agent_provenance`, `test_finish` |
| Compliance | `test_compliance`, `test_compliance_loop`, `test_compliance_visual_backfill`, `test_visual_multipage`, `test_vision_merge_sanitize`, `test_vision_learnings_hints`, `test_grounding` |
| Evaluation | `test_ground_truth_csv`, `test_config` |
| Output | `test_output`, `test_canonical_output`, `test_workbook_views`, `test_google_sheets_output`, `test_presenter` |

- `origin/main` adds `test_orchestration_worker` and `test_dedup_dynamodb` (moto-backed).
- Fixture CSVs are in `tests/fixtures/output/`.
- Tests use fakes and mocks. No live Drive, LLM, OCR or Sheets access is needed.
- The earlier architecture review (2026-05-12) reported a broken test suite. That is **resolved**, since the suite now collects and passes.

---

## 8. Documentation inventory

| File | Content | Accuracy note |
|---|---|---|
| `README.md` | Full user guide: loop, tiers, setup, run modes, config, tools, output, Drive, Sheets, Docker | **Stale in places.** It claims "fully local, no cloud APIs", defaults to Ollama, and references `config/tool_descriptions.yaml`, which does not exist. |
| `docs/architecture.md` | Mermaid diagrams of tool selection, phases and orchestration | Matches the code |
| `docs/tools.md` | Tool catalog, provider and guard settings | Mentions only Ollama and Gemini |
| `docs/google_drive_oauth_ingestion_plan.md` | The original design plan for Drive OAuth ingestion | A historical plan. The code has moved past it. |
| `docs/project_architecture_review_plan.md` | Prioritised review (P0–P3) from 2026-05-12 | Several items are still open (section 9) |
| `docs/demo_day_presentation.md`, `docs/build_pptx.py`, `docs/invoice_agent_demo.pptx`, `presentation.html` | Demo-day deck sources | Marketing material |
| `.planning/PROJECT.md` and `.planning/codebase/*` | A planning workspace with stack, integration, architecture, concerns, conventions and testing snapshots (dated 2026-06-25) | Dated snapshots. `.planning/` is in `.gitignore`, yet its files are tracked. |
| `.cursor/plans/rules_taxonomy_and_adaptations.plan.md` | A Cursor plan for rule taxonomy | Not reviewed in depth |
| `infra/README.md` (branch only) | CDK deploy guide | Says the Drive client is "on a separate branch". It has since landed. |
| `learnings/learnings.md` | Agent knowledge, edited by the agent and by hand | Data, not documentation |

**Untracked tooling in this checkout:** `tools/spreadsheet_filter/` and `outputs/019f4cb3-filter-rules/`. These hold Node and Python scripts, plus generated artifacts, for filtering and converting a source rules spreadsheet into `compliance_rules.csv` form. The filenames refer to an "aexcid" rule set. I did not review these in depth. `mvp-testing-prep` adds them to `.gitignore`.

---

## 9. Gaps, risks and documentation drift

Ordered by how much they block or endanger a real deployment.

### 9.1 Blockers for scheduled production use

1. **No Drive adapter for the worker.** `build_drive_client()` raises `NotImplementedError` unless `DRIVE_CLIENT=local`. The code comment says the adapter is to be written once Drive auth merged, and that has now happened. The worker also needs `upload_results` (write outputs back to a Drive output folder), which does not exist in `google_drive.py`.
2. **CDK is not on `main`.** It was reverted (`6fe1326`) and lives on `aws-cdk-infra`. Its README still says the Drive client is missing.
3. **The worker skips the batch workbook.** It calls `write_results` per document and never builds the canonical workbook or the Sheets sync. A scheduled run would not update the reviewer spreadsheet.
4. **Worker config mismatches.** `worker.main()` loads rules only from the local `config_dir`. It ignores `sources.google_drive.config_folder`, which is `enabled: true`.
5. **Ephemeral container storage.** The `learnings/` folder is baked into the image, so any learnings written in a task are lost. The default config hides learnings-write tools, so impact is limited today.
6. **Service-account access.** A service account sees only folders shared with its `client_email`. The input folder, the output folder, the config folder and the target spreadsheet each need that share.

### 9.2 Safety and privacy

- **Real defaults in the committed config.** `config.yaml` contains a specific spreadsheet ID and Drive folder URLs. `output.google_sheets.enabled: true` with mode `append` means any batch run by anyone with valid Drive credentials writes to that spreadsheet.
- **Default source is Drive.** A bare `python main.py` goes to Drive, not to `invoices/`.
- **Broad OAuth scope.** The configured scope is the full `drive` scope. The comment says this is for future write-back. Read-only ingestion would need less.
- **A real-looking invoice is tracked.** `backup invoices/A.5.d.- Personnel volontaire-U0279-25.pdf` is committed, because `.gitignore` covers only `invoices/*.pdf` and root `/*.pdf`. Check whether it is real data.
- **Data leaves the machine.** With OpenAI or Gemini selected, invoice images and OCR text go to those APIs. The README's "fully local" claim applies only to Ollama.
- **Output retention.** Logs and CSVs under `output/` can contain sensitive invoice data, and there is no retention policy.
- **Runtime installs.** `load_surya_models()` tries `pip install surya-ocr` if the import fails. The agent-callable `install_package` tool exists but is denied by config. The 2026-05-12 review recommended removing both.

### 9.3 Reliability and maintainability

- **Unpinned dependencies.** `requirements.txt` has lower bounds only, and `surya-ocr` has none. `transformers` is pinned below 5 because surya is incompatible with 5.x. There is no lock file.
- **Heavy image.** Surya is preloaded at build time, which makes builds slow and network-dependent.
- **CI covers tests only.** No Docker build, lint, type check or coverage.
- **Large modules.** `google_sheets.py` (919 lines), `tool_wrappers.py` (893), `workbook.py` (1,108), `evaluator.py` (699), `compliance_eval.py` (609) and `google_drive.py` (586).
- **Plans are advisory.** The generated execution plan is not enforced by the loop.
- **Python version split.** CI and Docker use 3.11, while the conda env is 3.12.
- **Minor.** The DynamoDB terminal write stores `updated_at=0.0`.

### 9.4 Documentation drift

- The README says "Runs fully locally via Ollama" and lists the older defaults. `config.yaml` selects OpenAI (`o3-mini` and `gpt-4.1-mini`).
- The README's `config.yaml` excerpt differs from the real file (`max_turns` 25 vs 30, `hybrid_extraction` true vs false).
- `docs/tools.md` lists "Flash for both" Gemini models. The config uses `gemini-2.5-pro` for vision.
- `config/tool_descriptions.yaml` is referenced but absent. The code falls back to defaults.
- The compose profile is called `gemini` but runs the OpenAI default.
- The Sheets example in the README uses `mode: replace`. The checked-in config uses `append`.

---

## 10. Recommended path to a working deployment

1. **Sync.** Fast-forward local `main` to `origin/main`.
2. **Write `GoogleDriveAdapter`.** Implement the `DriveClient` protocol in `src/orchestration/drive_source.py`.
   - Wrap `discover_google_drive_documents`, `materialize_google_drive_document` and `cleanup_materialized_google_drive_document`.
   - Add an `upload_results` function that writes to a separate Drive output folder. The input and output folders must differ, or the poller will re-ingest its own results.
   - Add tests alongside `test_orchestration_worker.py`.
3. **Decide the worker's reporting path.** Either build the batch workbook and Sheets sync at the end of each poll, or accept per-document Drive uploads only.
4. **Re-land the CDK stack.** Merge `aws-cdk-infra` (or a fresh PR) and update its README. Confirm `dedupTableName` matches `orchestration.dynamodb_table`, and that `llmKeyEnvName` matches `<provider>.api_key_env`.
5. **Add image build and push to CI.** Use a GitHub Actions job on `main` that builds and pushes to ECR, ideally with OIDC and no stored keys. Add a build-time flag for the Surya preload.
6. **Harden the defaults.** Move the real spreadsheet and folder IDs out of the committed `config.yaml` into an example file or environment variables. Set `output.google_sheets.enabled` to `false` by default. Narrow the Drive scope if no write-back is needed.
7. **Fix the docs.** Update the README provider and privacy statements and the config excerpt. Remove or create `tool_descriptions.yaml`. Rename the compose profile.
8. **Pin dependencies.** Add a constraints file for the image build.
9. **Dry-run.** Run the worker with `DRIVE_CLIENT=local` and the in-memory store, then once against a test Drive folder with a service account, before turning the schedule on.

---

## Appendix A: Repository map

```
invoice-agent/
├── main.py                      CLI and batch runner
├── Dockerfile, docker-compose.yml, .dockerignore
├── .github/workflows/ci.yml     pytest on PR and push to main
├── config/
│   ├── config.yaml              Runtime config (OpenAI default, Drive + Sheets on)
│   ├── config.docker-ollama.yaml
│   ├── phase_tools.yaml         Tools allowed per phase
│   └── csv/                     invoice_types, extraction_fields, compliance_rules,
│                                allowed_values, ground_truth, employee_name_role_denylist
├── src/
│   ├── agent/                   Loop, turn, state, phases, guards, prompts, pipeline, reflection
│   ├── tools/                   PDF, OCR, inventory, vision, compliance, learnings
│   ├── llm/                     Provider protocol, Ollama, Gemini, OpenAI, metering
│   ├── sources/                 local, google_drive, models, run_identity
│   ├── output/                  writer, canonical, workbook, google_sheets, presenter
│   ├── config/, models/, prompts/, compliance/, learning/, learnings/
│   └── orchestration/           (origin/main) worker, dedup, drive_source, agent_runner, config
├── infra/                       (aws-cdk-infra branch only) CDK Python stack
├── tests/                       30+ test modules and fixture CSVs
├── docs/, .planning/, learnings/
├── tools/spreadsheet_filter/    Untracked rule-spreadsheet utilities
└── outputs/                     Untracked generated artifacts
```

## Appendix B: Command cheat sheet

```bash
# Local run
python main.py --pdf invoices/ --local-config
python main.py --pdf invoices/x.pdf --type VIAJES --presentation
python main.py --list-types

# Drive
python main.py --drive-auth
python main.py --google-drive-folder-id <id>

# Learning
python main.py --pdf invoices/x.pdf --learn
python main.py --reset-learnings VIAJES --yes

# Sheets without processing
python main.py --upload-workbook-csv-dir tests/fixtures/output --sheets-spreadsheet-id <id>

# Docker
docker build -t invoice-agent .
docker compose --profile gemini run --rm agent python main.py --pdf invoices/x.pdf
docker compose --profile ollama up -d ollama

# Tests
pytest tests/ -q

# Worker, local rehearsal (origin/main)
DRIVE_CLIENT=local LOCAL_INPUT_DIR=invoices LOCAL_OUTPUT_DIR=output/drive \
  CONFIG_PATH=config/config.yaml python -m src.orchestration.worker

# CDK (aws-cdk-infra branch)
cd infra && pip install -r requirements.txt && cdk bootstrap && cdk deploy
```
