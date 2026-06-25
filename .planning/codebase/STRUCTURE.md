# Codebase Structure

**Analysis Date:** 2026-06-25

## Directory Layout

```text
invoice-agent/
├── main.py                 # CLI entrypoint and batch orchestration
├── requirements.txt        # Python runtime and test dependencies
├── Dockerfile              # Python 3.11 container image
├── docker-compose.yml      # Local Gemini/Ollama run profiles
├── config/                 # YAML and CSV business configuration
├── src/                    # Application packages
├── tests/                  # pytest suite
├── docs/                   # Design notes, plans, presentation assets
├── invoices/               # Local input PDF folder
├── learnings/              # Persisted learning notes
└── .github/workflows/      # CI workflow
```

## Directory Purposes

**`src/agent/`:**
- Purpose: Agent orchestration, loop/pipeline mechanics, prompts, action contracts, state, loop guards, registry, and policies.
- Contains: `agent.py`, `pipeline.py`, `state.py`, `turn.py`, `registry.py`, `loop_guards.py`, `tool_policy.py`.
- Key files: `src/agent/agent.py`, `src/agent/pipeline.py`, `src/agent/state.py`.

**`src/tools/`:**
- Purpose: Executable tools called by the agent and pipeline.
- Contains: PDF/image helpers, OCR, page inventory, vision extraction, compliance checks, learnings store, wrappers.
- Key files: `src/tools/pdf_pages.py`, `src/tools/vision_llm.py`, `src/tools/compliance_eval.py`, `src/tools/tool_wrappers.py`.

**`src/llm/`:**
- Purpose: Provider abstraction and backend-specific request formatting.
- Contains: `base.py`, `factory.py`, `ollama_provider.py`, `gemini_provider.py`, `openai_provider.py`, metering and response-format utilities.
- Key files: `src/llm/factory.py`, `src/llm/metered_provider.py`.

**`src/sources/`:**
- Purpose: Input source abstraction and provenance.
- Contains: local materialization, Google Drive discovery/download/config materialization, run identity helpers, source models.
- Key files: `src/sources/google_drive.py`, `src/sources/local.py`, `src/sources/run_identity.py`.

**`src/config/`:**
- Purpose: Load and expose CSV-backed business configuration.
- Contains: `loader.py`.
- Key files: `src/config/loader.py`.

**`src/models/`:**
- Purpose: Typed models shared across config, state, actions, and tool I/O.
- Contains: `config_models.py`, `state_models.py`, `action_models.py`, `tool_io_models.py`.

**`src/output/`:**
- Purpose: Persist run outputs and render terminal presentation output.
- Contains: `writer.py`, `presenter.py`.

**`src/learning/` and `src/learnings/`:**
- Purpose: Ground-truth evaluation and vision hint/learning utilities.
- Contains: `src/learning/evaluator.py`, `src/learnings/vision_hints.py`.

**`config/`:**
- Purpose: Runtime YAML plus CSV business rules.
- Contains: `config/config.yaml`, `config/config.docker-ollama.yaml`, and `config/csv/*.csv`.

**`tests/`:**
- Purpose: pytest coverage for sources, providers, compliance, output, presenter, config, and agent contracts.
- Contains: `tests/test_*.py` files.

## Key File Locations

**Entry Points:**
- `main.py`: CLI argument parsing, configuration, source selection, process loop.
- `Dockerfile`: container command defaults to `python main.py --help`.

**Configuration:**
- `config/config.yaml`: default provider/source/agent/output/OCR config.
- `config/config.docker-ollama.yaml`: Docker Ollama-specific config.
- `config/csv/invoice_types.csv`: invoice type definitions.
- `config/csv/extraction_fields.csv`: extraction schema source.
- `config/csv/compliance_rules.csv`: compliance rule source.
- `.env.example`: API-key environment template.

**Core Logic:**
- `src/agent/agent.py`: main orchestrator.
- `src/agent/pipeline.py`: deterministic processing sequence.
- `src/agent/registry.py`: tool registry composition.
- `src/tools/tool_wrappers.py`: composite/pipeline tool behavior.
- `src/tools/vision_llm.py`: model-backed classification and field extraction.
- `src/tools/compliance_eval.py`: field-based compliance evaluation.
- `src/sources/google_drive.py`: Drive source implementation.

**Testing:**
- `tests/test_sources_google_drive.py`: Drive discovery/materialization behavior.
- `tests/test_cli_sources.py`: CLI source selection behavior.
- `tests/test_openai_provider.py`, `tests/test_gemini_provider.py`: provider behavior.
- `tests/test_compliance.py`, `tests/test_compliance_loop.py`: compliance checks and loop behavior.
- `tests/test_output.py`: output writer and learnings store.

## Naming Conventions

**Files:**
- Python source files use lowercase snake_case: `src/agent/loop_guards.py`, `src/tools/page_inventory.py`.
- Tests use `test_<subject>.py`: `tests/test_sources_google_drive.py`.
- Config CSVs use descriptive snake_case: `config/csv/allowed_values.csv`.

**Directories:**
- Package directories are lowercase nouns grouped by responsibility: `agent`, `tools`, `sources`, `llm`, `output`, `models`.
- Keep new runtime code under `src/<domain>/`; keep tests under `tests/` with matching subject names.

## Where to Add New Code

**New Feature:**
- CLI/source-facing feature: start in `main.py`, then place reusable logic under the relevant `src/` package.
- Agent orchestration feature: add to `src/agent/` and test with `tests/test_agent_*.py` or `tests/test_turn_action_contract.py`.
- Tool feature: implement under `src/tools/`, export through `src/tools/tools.py`, register in `src/agent/registry.py`, and test in `tests/test_<tool_or_flow>.py`.
- Source feature: implement under `src/sources/` and add source tests near `tests/test_sources_*.py` and `tests/test_cli_sources.py`.

**New Component/Module:**
- LLM backend: add `src/llm/<provider>_provider.py`, update `src/llm/factory.py`, add tests in `tests/test_<provider>_provider.py`.
- Business config field/rule: prefer editing `config/csv/*.csv`; update models in `src/models/config_models.py` only when schema changes.
- Output format: update `src/output/writer.py` and tests in `tests/test_output.py`.

**Utilities:**
- Shared agent utilities: `src/agent/`.
- Shared tool helpers: `src/tools/`.
- Shared source/provenance helpers: `src/sources/`.
- Avoid dumping general helpers into unrelated modules; create a domain-specific module instead.

## Special Directories

**`output/`:**
- Purpose: Runtime results, logs, materialized Drive downloads, rendered pages.
- Generated: Yes.
- Committed: No.

**`.secrets/`:**
- Purpose: OAuth client and token files.
- Generated: Partly user-provided, partly runtime-created.
- Committed: No.

**`invoices/`:**
- Purpose: Local input PDFs.
- Generated: User-provided.
- Committed: Directory placeholder only.

**`learnings/`:**
- Purpose: Persistent learning notes used by future runs.
- Generated: Runtime may append.
- Committed: Yes, at least `learnings/learnings.md` is tracked in this checkout.

**`docs/`:**
- Purpose: Architecture notes, planning docs, demo presentation source/assets.
- Generated: Some artifacts such as `docs/invoice_agent_demo.pptx` may be generated by `docs/build_pptx.py`.
- Committed: Yes.

---

*Structure analysis: 2026-06-25*
