# Testing Patterns

**Analysis Date:** 2026-06-25

## Test Framework

**Runner:**
- pytest 7+.
- Config: no dedicated pytest config detected; CI runs `pytest tests/ -q` from `.github/workflows/ci.yml`.

**Invocation:**
- Full suite: `pytest tests/ -q`.
- Focused file: `pytest tests/test_sources_google_drive.py -q`.

## Test Structure

**Location:**
- Tests live in `tests/` and mirror subject areas with `test_<area>.py` names.

**Organization:**
- Source ingestion: `tests/test_sources_google_drive.py`, `tests/test_sources_local.py`, `tests/test_cli_sources.py`, `tests/test_run_identity.py`.
- LLM providers and formatting: `tests/test_openai_provider.py`, `tests/test_gemini_provider.py`, `tests/test_response_format.py`.
- Agent loop/contracts: `tests/test_action_contract_page_num.py`, `tests/test_agent_fallback.py`, `tests/test_turn_action_contract.py`, `tests/test_finish.py`, `tests/test_timeouts_prompt_guard.py`.
- Compliance and extraction behavior: `tests/test_compliance.py`, `tests/test_compliance_loop.py`, `tests/test_compliance_visual_backfill.py`, `tests/test_grounding.py`, `tests/test_visual_multipage.py`.
- Output/presenter: `tests/test_output.py`, `tests/test_presenter.py`.

## Test Data

**Fixtures:**
- Tests mostly create temporary files and fake service objects inline.
- `tmp_path`, `tempfile.TemporaryDirectory`, and monkeypatching are common.
- Config CSV behavior is tested by writing minimal temporary CSVs or using existing `config/csv` expectations.

**External Services:**
- Google Drive tests use fake service/request classes such as `FakeService`, `FakeFiles`, and `FakeExecuteRequest` in `tests/test_sources_google_drive.py`.
- Provider tests should mock SDK clients/responses rather than making live API calls.

## Mocking Patterns

**Fake Objects:**
- Use small fake classes with `.execute()`, `.list()`, or `.get_media()` behavior for Google API shape.
- Use `monkeypatch` for replacing imports, environment variables, or external SDK modules.

**State Builders:**
- Build direct `AgentState` instances for output and finish tests, as in `tests/test_output.py`.
- Populate only fields needed for the assertion; avoid full end-to-end invoice runs in unit tests.

## Assertions

**Style:**
- Assert exact output fields, statuses, and path/provenance values.
- Assert failure messages for user-facing validation, for example Drive config errors in `tests/test_sources_google_drive.py`.
- Prefer behavior assertions over implementation details unless the contract is an SDK request shape.

**Coverage Expectations:**
- For new source behavior, test config resolution, discovery, materialization, cleanup, and CLI selection.
- For new provider behavior, test request formatting, JSON parsing, timeout handling, and auth/env resolution.
- For new output fields, test CSV headers, row values, and append behavior.
- For new tools, test state mutation and structured result dictionaries.

## CI

**Workflow:**
- `.github/workflows/ci.yml` runs on pull requests and pushes to `main`.
- It installs Python 3.11, caches pip dependencies by `requirements.txt`, installs requirements, and runs `pytest tests/ -q`.

**Limitations:**
- CI does not run linting, type checking, coverage thresholds, Docker build, or live LLM/Drive integration tests.
- Heavy OCR/model behavior should be isolated behind mocks or marked/kept out of the default suite.

## Adding Tests

**New Module:**
- Add `tests/test_<module_or_feature>.py`.
- Use local fakes and `tmp_path` for filesystem/network boundaries.

**Regression:**
- Add a focused test reproducing the exact state/config that failed.
- Keep business-rule regressions close to `tests/test_compliance*.py` or `tests/test_ground_truth_csv.py`.

**Integration:**
- Prefer narrow integration through `main.py` source-selection helpers or `InvoiceAgent` dependencies with fake providers.
- Do not require real `.env`, `.secrets`, Google Drive, OpenAI, Gemini, Ollama, or Surya model downloads in the default test run.

---

*Testing analysis: 2026-06-25*
