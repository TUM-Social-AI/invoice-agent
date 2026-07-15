# Coding Conventions

**Analysis Date:** 2026-06-25

## Naming Patterns

**Files:**
- Use snake_case Python modules: `src/agent/loop_utils.py`, `src/tools/compliance_visual.py`, `tests/test_ground_truth_csv.py`.
- Keep provider implementations named `<provider>_provider.py`: `src/llm/openai_provider.py`, `src/llm/gemini_provider.py`.

**Functions:**
- Use snake_case verbs or verb phrases: `load_config()`, `build_llm_provider()`, `write_results()`, `resolve_google_drive_credentials()`.
- Prefix private helpers with `_`: `_drive_config()`, `_timestamp()`, `_dispatch()`.

**Variables:**
- Use snake_case locals and explicit names: `source_provenance`, `run_identity`, `active_rule_groups`, `log_line_max_chars`.
- Constants use uppercase: `FIELD_COLUMNS`, `COMPLIANCE_COLUMNS`, `DEFAULT_DRIVE_SCOPE`.

**Types:**
- Classes use PascalCase: `InvoiceAgent`, `ConfigStore`, `GoogleDriveSourceError`, `OpenAIProvider`.
- Pydantic model subclasses are domain nouns: `InvoiceType`, `ComplianceRule`, `ObservationFallback`.

## Code Style

**Formatting:**
- No formatter config detected. Existing code follows conventional Black-like 4-space indentation and line wrapping.
- Preserve type hints and `from __future__ import annotations` in newer modules.

**Linting:**
- No Ruff, Flake8, Pylint, or mypy configuration detected.
- CI only runs `pytest tests/ -q`.

## Import Organization

**Order:**
1. `from __future__ import annotations` when used.
2. Standard library imports.
3. Third-party imports.
4. Local `src.*` imports.

**Path Aliases:**
- No configured aliases. Imports use package paths rooted at `src`, e.g. `from src.agent.state import AgentState`.

## Error Handling

**Patterns:**
- Define domain-specific exceptions at source boundaries, e.g. `GoogleDriveSourceError` in `src/sources/google_drive.py`.
- Raise clear setup/config errors for missing OAuth files, unsupported providers, and invalid provider dependencies.
- Return structured result dictionaries from tools for recoverable per-run failures; pipeline mode checks `success` and mutates `AgentState.status`.
- In broad best-effort logging paths, catch `Exception` sparingly and continue only when failure should not stop invoice processing, as in `src/agent/pipeline.py`.

## Logging

**Framework:**
- Python `logging` plus Rich presenter.

**Patterns:**
- Configure root logging in `main.py` and adjust log level after config load.
- Use module loggers via `logging.getLogger(__name__)` in source modules.
- Redact URL query API keys in `main.py` before noisy HTTP debug logs can leak them.
- Use `clip_for_log()` and `log_line_max_chars` to keep logs manageable.

## Comments

**When to Comment:**
- Use docstrings for modules/classes/functions that define important runtime contracts, such as `main.py`, `src/agent/agent.py`, and `src/output/writer.py`.
- Use comments to explain non-obvious operational constraints: model choice tradeoffs in `config/config.yaml`, Docker image size/model preload, and remote guardrails.

**JSDoc/TSDoc:**
- Not applicable; this is a Python project.
- Use Python docstrings for public functions and classes.

## Function Design

**Size:**
- Prefer small helpers around boundary parsing, credential resolution, output row building, and provider formatting.
- Larger orchestrators exist where sequence matters (`main.py`, `src/agent/agent.py`, `src/tools/tool_wrappers.py`); new logic should be extracted when it can be independently tested.

**Parameters:**
- Prefer keyword-only parameters for functions with multiple optional controls, as in `resolve_google_drive_credentials()`.
- Pass `AgentState` explicitly into tool functions.
- Pass config dictionaries into provider/source builders; resolve specific values in `src/llm/config_resolve.py` or local helper functions.

**Return Values:**
- Use typed domain objects for config/source/state structures.
- Use dictionaries for tool call results and output path summaries when serialized/tested shape matters.
- Return empty collections rather than `None` for no-results lists where callers iterate.

## Module Design

**Exports:**
- `src/tools/tools.py` is a facade re-export for tool implementations; update it when adding a tool expected in the registry.
- `src/__init__.py` and package `__init__.py` files are minimal.

**Barrel Files:**
- Only `src/tools/tools.py` acts as a notable barrel/facade.
- Avoid broad barrels elsewhere unless there is an existing import contract to preserve.

---

*Convention analysis: 2026-06-25*
