<!-- refreshed: 2026-06-25 -->
# Architecture

**Analysis Date:** 2026-06-25

## System Overview

```text
┌─────────────────────────────────────────────────────────────┐
│                         CLI Entrypoint                       │
│                         `main.py`                            │
└───────────────┬──────────────────────┬──────────────────────┘
                │                      │
                ▼                      ▼
┌──────────────────────────┐  ┌───────────────────────────────┐
│ Source materialization    │  │ Config loading                 │
│ `src/sources/*`           │  │ `src/config/loader.py`         │
└───────────────┬──────────┘  └───────────────┬───────────────┘
                │                             │
                ▼                             ▼
┌─────────────────────────────────────────────────────────────┐
│                     Agent Orchestration                      │
│ `src/agent/agent.py`, `src/agent/pipeline.py`, `src/agent/*` │
└───────────────┬──────────────────────┬──────────────────────┘
                │                      │
                ▼                      ▼
┌──────────────────────────┐  ┌───────────────────────────────┐
│ Tool registry + tools     │  │ LLM providers                  │
│ `src/tools/*`             │  │ `src/llm/*`                    │
└───────────────┬──────────┘  └───────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│ Outputs, logs, learnings, run identity                       │
│ `src/output/*`, `src/learning/*`, `src/learnings/*`          │
└─────────────────────────────────────────────────────────────┘
```

## Component Responsibilities

| Component | Responsibility | File |
|-----------|----------------|------|
| CLI | Parse arguments, load env/config, choose local vs Drive source, run batch/single invoice processing | `main.py` |
| Config loader | Load CSV business configuration into typed stores and build LLM context/schema | `src/config/loader.py` |
| Source layer | Materialize local PDFs or Google Drive PDFs/config files with provenance | `src/sources/local.py`, `src/sources/google_drive.py`, `src/sources/models.py` |
| Agent orchestrator | Build provider/tool registry, run loop or fixed pipeline, enforce loop guards | `src/agent/agent.py` |
| Fixed pipeline | Deterministic scan/inventory/extract/compliance/finish sequence | `src/agent/pipeline.py` |
| Tool registry | Assemble callable tool surface for the agent | `src/agent/registry.py` |
| Tools | PDF rendering, OCR, vision extraction, compliance checks, learnings, finish/review actions | `src/tools/*.py` |
| LLM providers | Normalize Ollama, Gemini, and OpenAI behind `LLMProvider` | `src/llm/*.py` |
| Output writer | Write per-run field CSV, compliance CSV, and rolling summary CSV | `src/output/writer.py` |

## Pattern Overview

**Overall:** CLI-driven pipeline/agent-loop with typed state, provider abstraction, and tool dispatch.

**Key Characteristics:**
- Keep business rules in CSV config, not hardcoded control flow.
- Route all model access through `LLMProvider` implementations.
- Mutate `AgentState` through tools and record every action in the state history.
- Support both agentic loop mode and deterministic pipeline mode with the same tool primitives.

## Layers

**Entrypoint Layer:**
- Purpose: User-facing CLI, env loading, high-level batch orchestration.
- Location: `main.py`.
- Contains: argument parsing, config/source selection, process loop, learning-mode evaluation.
- Depends on: config, sources, agent, output, learning modules.
- Used by: direct CLI, Docker command, CI smoke/import tests.

**Configuration Layer:**
- Purpose: Turn YAML and CSV files into typed runtime configuration and LLM prompt context.
- Location: `config/config.yaml`, `config/csv/`, `src/config/loader.py`.
- Contains: invoice types, fields, rules, allowed values, denylist, rule group filtering.
- Depends on: Pydantic models in `src/models/config_models.py`.
- Used by: agent initialization and compliance/evaluation tools.

**Source Layer:**
- Purpose: Normalize local and Google Drive inputs into materialized documents with provenance.
- Location: `src/sources/`.
- Contains: local file handling, Drive discovery/download/config-folder materialization, run identity helpers.
- Depends on: Google API libraries for Drive; filesystem for local inputs.
- Used by: `main.py` before invoking `InvoiceAgent.run()`.

**Agent Layer:**
- Purpose: Own runtime state progression and model/tool decisions.
- Location: `src/agent/`.
- Contains: state, loop guards, action contract validation, phases, prompt building, fallback handling, param resolution.
- Depends on: LLM provider, tool registry, config store.
- Used by: `main.py` and tests in `tests/test_agent_*.py`, `tests/test_turn_action_contract.py`, `tests/test_finish.py`.

**Tool Layer:**
- Purpose: Implement deterministic side effects and model-backed extraction/compliance functions.
- Location: `src/tools/`.
- Contains: PDF page rendering, OCR layout, page inventory, vision extraction, compliance evaluation, visual compliance, learnings store, wrappers.
- Depends on: `AgentState`, image/PDF libraries, LLM providers.
- Used by: agent loop and fixed pipeline.

**Output Layer:**
- Purpose: Persist results and present progress.
- Location: `src/output/`.
- Contains: CSV writing and Rich/Null presenters.
- Depends on: `AgentState` and rule verdict helpers.
- Used by: `main.py`, `src/agent/agent.py`, `src/agent/pipeline.py`.

## Data Flow

### Primary Invoice Run

1. CLI parses flags and loads `.env` plus YAML config (`main.py`).
2. Config CSVs are loaded locally or materialized from Drive (`load_config_store()` in `main.py`, `src/config/loader.py`, `src/sources/google_drive.py`).
3. Input PDFs are materialized from local paths or Google Drive (`src/sources/local.py`, `src/sources/google_drive.py`).
4. `InvoiceAgent` builds an LLM provider via `src/llm/factory.py` and tool registry via `src/agent/registry.py`.
5. `InvoiceAgent.run()` initializes `AgentState`, then runs loop mode or `run_fixed_pipeline()`.
6. Tools render pages, inventory pages, classify invoice type, extract fields, check rules, and finish (`src/tools/*.py`).
7. `write_results()` writes fields/compliance/summary CSVs under the selected output directory (`src/output/writer.py`).
8. Optional learning-mode evaluation compares state to ground truth and writes learnings (`src/learning/evaluator.py`, `src/tools/learnings_store.py`).

### Google Drive Source Flow

1. Resolve folder ID from CLI override or `config/config.yaml` (`resolve_google_drive_folder_id()` in `src/sources/google_drive.py`).
2. Resolve OAuth credentials from token or desktop client JSON (`resolve_google_drive_credentials()`).
3. Discover PDF document refs from Drive (`discover_google_drive_documents()`).
4. Download each PDF to `output/materialized` (`materialize_google_drive_document()`).
5. Preserve provenance and run identity in `AgentState` and output CSV summary.
6. Clean up downloaded PDF after processing when `cleanup_downloads` is enabled.

**State Management:**
- Runtime state lives in `AgentState` in `src/agent/state.py`.
- Per-run persistence is CSV/log output, not a database.
- Learning state is Markdown in `learnings/learnings.md`.

## Key Abstractions

**`AgentState`:**
- Purpose: Central mutable run state for PDF path, invoice type, pages, extracted fields, rule results, action history, provenance, and status.
- Examples: `src/agent/state.py`, `tests/test_finish.py`, `tests/test_output.py`.
- Pattern: Pydantic-like domain model with helper methods for prompt summaries and action recording.

**`LLMProvider`:**
- Purpose: Common interface for JSON chat and image generation/extraction calls.
- Examples: `src/llm/base.py`, `src/llm/ollama_provider.py`, `src/llm/gemini_provider.py`, `src/llm/openai_provider.py`.
- Pattern: Provider adapter selected by config.

**Tool Registry:**
- Purpose: Named callables exposed to loop and pipeline modes.
- Examples: `src/agent/registry.py`, `src/tools/tools.py`, `src/tools/tool_wrappers.py`.
- Pattern: Function registry plus exposed-tool policy.

**Config Store:**
- Purpose: Typed business rules and extraction schema builder.
- Examples: `src/config/loader.py`, `src/models/config_models.py`.
- Pattern: CSV-backed repository object.

## Entry Points

**CLI:**
- Location: `main.py`.
- Triggers: `python main.py`, Docker `CMD`, direct user commands.
- Responsibilities: configure, materialize sources, instantiate agent, process invoices, write outputs.

**Tests:**
- Location: `tests/`.
- Triggers: `pytest tests/ -q` in `.github/workflows/ci.yml`.
- Responsibilities: verify Drive source behavior, provider formatting, compliance, output, presenter, CLI source decisions, and loop contracts.

## Architectural Constraints

- **Threading:** Single-process synchronous CLI. LLM/API calls and PDF operations run inline; no async worker queue detected.
- **Global state:** Logging setup and URL-key redaction are module-level in `main.py`; Surya models are loaded once in `InvoiceAgent.__init__()` and shared by tool registry for that process.
- **Circular imports:** No explicit circular import chain detected in the sampled modules; keep new modules layered from config/sources/models upward into agent/tools/output.
- **Secrets:** Secret-bearing files should remain in `.env` or `.secrets/`; generated docs and logs must avoid printing token contents.

## Anti-Patterns

### Hardcoding Business Rules in Tools

**What happens:** Compliance behavior bypasses CSV config and lands directly in tool functions.
**Why it's wrong:** The system is designed around `config/csv/compliance_rules.csv`, `config/csv/extraction_fields.csv`, and `ConfigStore` so rules can change without code edits.
**Do this instead:** Add or change CSV rows and consume them through `src/config/loader.py` and `src/tools/compliance_eval.py`.

### Provider-Specific Logic Outside Providers

**What happens:** Call sites branch directly on OpenAI/Gemini/Ollama request formats.
**Why it's wrong:** It defeats the `LLMProvider` adapter boundary and makes remote guardrails inconsistent.
**Do this instead:** Add provider behavior in `src/llm/*_provider.py` or `src/llm/config_resolve.py`, then call through `src/llm/base.py` protocol methods.

## Error Handling

**Strategy:** Raise source/config errors early at the boundary, use tool result dictionaries for recoverable run issues, and use loop guards for repeated failures.

**Patterns:**
- Source failures raise `SourceError` or `GoogleDriveSourceError` from `src/sources/*` and are handled by `main.py`.
- Provider setup failures raise clear `RuntimeError`/`ValueError` messages from `src/llm/*`.
- Pipeline failures set `AgentState.status` and `finish_reason` in `src/agent/pipeline.py`.
- Loop repetition and consecutive failures are handled by `src/agent/loop_guards.py`.

## Cross-Cutting Concerns

**Logging:** Python logging, JSONL run logs, and Rich presenter paths.
**Validation:** Pydantic config/action/state models plus response-format parsing and action-contract validation.
**Authentication:** Environment API keys for LLM providers and OAuth token/client files for Drive.

---

*Architecture analysis: 2026-06-25*
