# Codebase Concerns

**Analysis Date:** 2026-06-25

## Highest Priority Concerns

### Dependencies Are Not Locked

**Evidence:** `requirements.txt` uses lower bounds and broad ranges for most dependencies.

**Impact:** Rebuilds can pull newer OCR, SDK, LLM, or PDF-processing versions that change behavior or break tests. This is especially risky for `surya-ocr`, `transformers`, `google-genai`, `openai`, and `pypdfium2`.

**Fix approach:** Add a lock/constraints file for repeatable runtime builds, or pin Docker builds with a generated constraints file while keeping development ranges in `requirements.txt`.

### Default Config Uses Remote Provider and Drive Folders

**Evidence:** `config/config.yaml` sets `llm.provider: openai` and includes configured Google Drive folder URLs under `sources.google_drive`.

**Impact:** A fresh run can prefer remote API behavior and Drive ingestion unless the operator understands the config. This can surprise local-only users and makes demos depend on external credentials.

**Fix approach:** Consider splitting checked-in sample config from local/private config, or make `config/config.yaml` conservative while documenting remote provider/Drive overrides.

### Docker Build Preloads Surya Models

**Evidence:** `Dockerfile` runs `python -c "from src.tools.ocr_layout import load_surya_models; assert load_surya_models() is not None"` during image build.

**Impact:** Docker builds can become slow, large, network-dependent, and fragile if model hosting is unavailable. It also makes CI Docker-build adoption expensive.

**Fix approach:** Move model preloading behind an optional build arg or runtime warmup command, and document the tradeoff.

## Technical Debt

### Large Multi-Responsibility Modules

**Evidence:** Largest files include `src/tools/tool_wrappers.py` (893 lines), `src/learning/evaluator.py` (699), `src/agent/agent.py` (627), `src/tools/compliance_eval.py` (609), and `src/sources/google_drive.py` (586).

**Impact:** These modules mix orchestration, parsing, error handling, and business logic, making targeted changes harder and increasing regression risk.

**Fix approach:** Extract cohesive units only when modifying nearby behavior: Drive auth vs discovery vs materialization, compliance expression parsing vs rule application, and wrapper orchestration vs result normalization.

### No Static Analysis Gate

**Evidence:** `.github/workflows/ci.yml` runs pytest only. No Ruff/Black/mypy config is present.

**Impact:** Import cycles, unused code, style drift, and typing mistakes may reach PR review manually. This matters because providers and tool results rely on precise dictionary/model contracts.

**Fix approach:** Add Ruff for fast lint/format checks first. Consider mypy or pyright later for core model/provider/tool surfaces.

### Broad Exception Swallowing in Non-Critical Paths

**Evidence:** `pass` appears in exception handlers in `main.py`, `src/agent/pipeline.py`, `src/sources/google_drive.py`, `src/output/presenter.py`, `src/tools/page_inventory.py`, and provider metering code.

**Impact:** Some operational failures may be hidden from logs or tests. This is acceptable for presenter hooks in places, but source cleanup/auth/materialization paths should be explicit about ignored failures.

**Fix approach:** Audit each broad `except Exception: pass`; keep only truly best-effort UI/logging paths, and log debug/warning context elsewhere.

## Security and Privacy Concerns

### Generated Logs May Contain Sensitive Invoice Data

**Evidence:** The app writes fields, compliance messages, summaries, and run logs under `output/` via `src/output/writer.py` and loop logging.

**Impact:** Invoice PII, vendor data, payment details, or compliance observations can persist locally. This is correct for processing, but the retention boundary should be explicit.

**Fix approach:** Keep `output/` ignored, document retention/deletion expectations, and avoid adding generated output samples with real invoice data.

### Drive OAuth Scope Is Broad

**Evidence:** `config/config.yaml` defaults Drive scopes to `https://www.googleapis.com/auth/drive`.

**Impact:** Full Drive scope is broader than read-only ingestion needs. The comment says it supports future uploads/moves, but current read/materialize paths may not require full access.

**Fix approach:** Use the narrowest possible scope for the active feature set, or make broader scope opt-in for write-back features.

### API-Key Redaction Is Specific

**Evidence:** `main.py` redacts `?key=` query parameters from urllib3 logs.

**Impact:** Other credential forms, headers, OAuth token payloads, or provider SDK debug logs may not be covered.

**Fix approach:** Keep provider logging low by default, avoid logging request headers/bodies, and expand redaction only for observed leak paths.

## Performance Concerns

### Vision/OCR Pipeline Is Expensive

**Evidence:** README and `config/config.yaml` describe page inventory batching, OCR injection, visual evidence page caps, DPI, and remote guardrails.

**Impact:** Large PDFs can trigger many vision requests and large OCR prompts. Remote providers can hit cost/rate/token limits; local models can be slow or memory-heavy.

**Fix approach:** Preserve `llm.remote_guard`, tune `inventory_batch_size`, `ocr_prompt_max_chars`, `visual_max_evidence_pages`, and `page_dpi` per deployment. Add performance tests around page batching if this becomes production-critical.

### Synchronous Single-Process Execution

**Evidence:** `main.py`, `src/agent/agent.py`, and `src/agent/pipeline.py` run processing inline.

**Impact:** Batch Drive or folder processing is simple but serial; one slow PDF/provider call blocks the process.

**Fix approach:** Keep CLI sync for now. If throughput becomes a goal, add a job/worker boundary around materialized documents rather than threading inside the agent state machine.

## Testing Gaps

### No Live Integration Coverage in CI

**Evidence:** CI runs unit-style pytest only and does not build Docker or call real Drive/LLM/OCR services.

**Impact:** SDK/API drift, OAuth consent/token behavior, model availability, and Docker model preload problems can escape CI.

**Fix approach:** Add optional/manual integration jobs with test credentials and tiny synthetic PDFs, or scheduled smoke tests in a controlled environment.

### No Coverage Thresholds

**Evidence:** No coverage tool or threshold is configured.

**Impact:** New branches in complex modules can land without direct tests.

**Fix approach:** Add coverage reporting for core packages and set a modest threshold after measuring the current baseline.

## Maintenance Notes

- Prefer adding tests before changing `src/tools/tool_wrappers.py`, `src/tools/compliance_eval.py`, `src/sources/google_drive.py`, or `src/agent/agent.py`.
- Keep config/data changes in `config/csv/` unless a schema change is genuinely needed.
- Keep provider-specific request logic inside `src/llm/*_provider.py`.
- Keep source-specific provenance logic inside `src/sources/*` so output summaries remain consistent.

---

*Concern analysis: 2026-06-25*
