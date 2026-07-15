# Invoice Output Normalization and Google Sheets Delivery

## What This Is

This project extends the existing invoice compliance agent so its extraction and compliance results become easy to consume outside the CLI. The system will produce canonical local CSV outputs first, then sync the same normalized datasets to Google Drive/Sheets and generate user-friendly views such as an invoice summary, compliance results table, compliance matrix, and review/dashboard sheets.

The project is for people reviewing batches of invoices who need spreadsheet-ready results without rerunning OCR, LLM extraction, or the full agent loop just to test reporting and Google Sheets behavior.

## Core Value

Users can trust one canonical result schema that works locally as CSV and in Google Sheets, with generated views that make invoice compliance review faster and clearer.

## Requirements

### Validated

- ✓ The agent processes local and Google Drive PDF invoice sources through a CLI workflow — existing
- ✓ The agent extracts invoice fields into `AgentState.extracted_fields` and writes per-run field CSVs — existing
- ✓ The agent evaluates field-based and visual compliance rules and writes per-run compliance CSVs — existing
- ✓ The agent preserves source provenance and run identity for local and Google Drive documents — existing
- ✓ Google Drive ingestion and OAuth credentials already exist for reading PDFs and config files — existing
- ✓ The test suite covers Google Drive source behavior, output writing, providers, compliance, and CLI source selection — existing

### Active

- [ ] Define canonical normalized output schemas for invoice summaries and per-rule compliance results.
- [ ] Make local CSV output use the same canonical schemas that Google Sheets sync will consume.
- [ ] Support fixture/test-data driven exports so output and Sheets behavior can be tested without running OCR, LLM calls, or invoice extraction.
- [ ] Add a Google Sheets/Drive delivery target that can create or update sheets from canonical output rows.
- [ ] Generate analyst-friendly spreadsheet views from normalized data, starting with a compliance matrix and review/dashboard-style sheets.
- [ ] Keep detailed per-run artifacts available where useful, but make the canonical batch outputs the stable integration contract.

### Out of Scope

- Reworking invoice OCR, extraction prompts, or compliance rule logic — this project is about output normalization and delivery.
- Building a web application dashboard — Google Sheets is the first user-facing review surface.
- Requiring live Google API access for core validation — local fixture and fake-writer tests must cover the schema and view logic.
- Requiring a full invoice-processing run to test Sheets output — fixture CSV/JSON paths should exercise the delivery layer directly.
- Native Google Sheets pivot tables and charts in v1 — generated tabular views are simpler, more testable, and can support native pivots later.

## Context

The current codebase is a Python 3.11 CLI invoice compliance agent. `main.py` loads configuration, chooses a local or Google Drive source, constructs `InvoiceAgent`, processes invoices, and writes results through `src/output/writer.py`. The existing output writer creates timestamped per-run field CSVs, compliance CSVs, and a rolling `summary.csv`.

The existing architecture already has useful seams for this project:
- `AgentState` in `src/agent/state.py` centralizes extracted fields, rule results, provenance, status, and run identity.
- `src/output/writer.py` owns CSV serialization and is the natural place to introduce canonical row builders or delegate to them.
- `src/sources/google_drive.py` already handles Google Drive OAuth, folder discovery, file materialization, and config-folder ingestion.
- `tests/test_output.py`, `tests/test_sources_google_drive.py`, and `tests/test_cli_sources.py` provide patterns for filesystem, fake-service, and no-live-API testing.

The desired spreadsheet model is normalized raw data plus generated views:
- `Invoice Summary`: one row per processed invoice or run, with identity, provenance, extracted fields, and rolled-up compliance summary.
- `Compliance Results`: one row per invoice-rule result, with rule ID/name, severity, status, reasoning, evidence, and source page where available.
- `Compliance Matrix`: generated from compliance results, with invoices as rows, rules as columns, and compact PASS/FAIL/WARN/UNSURE values.
- Review/dashboard sheets: generated tables for failed invoices, warning counts, review queue, latest run status, or other analyst-friendly summaries.

The first implementation should prove this offline with deterministic fixture data before relying on Google Sheets API behavior. A fake or in-memory sheet writer should make tests fast and stable, while a real Google writer can be integration-tested separately or exercised manually with OAuth credentials.

## Constraints

- **Tech stack**: Stay in Python 3.11 and follow existing module boundaries — the project already uses Python, Pydantic, pytest, Google API libraries, and filesystem CSV outputs.
- **Testing**: Core schema, CSV, and generated-view behavior must be testable without OpenAI, Gemini, Ollama, Surya OCR, Google Drive, or a live invoice run.
- **Google credentials**: Real Sheets/Drive writes must use existing OAuth-style configuration patterns and must not log or commit credentials.
- **Compatibility**: Preserve existing output behavior where downstream users may rely on it, or migrate with clear compatibility wrappers/options.
- **Data shape**: Prefer normalized long-format compliance rows as the canonical data; generate matrix/pivot-style views from that source rather than making wide rule columns the primary contract.
- **Privacy**: Output artifacts can contain invoice and compliance-sensitive data; generated examples and tests should use synthetic fixtures.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Initialize as output normalization plus Sheets delivery, not Sheets-only upload | Local CSVs, fixtures, and Google Sheets should share one stable schema so the feature can be tested without live APIs and used consistently outside Drive | — Pending |
| Use two canonical raw datasets: invoice summary and compliance results | One row per invoice plus one row per invoice-rule keeps the data appendable, filterable, and resilient when rules change | — Pending |
| Generate spreadsheet-friendly views from normalized data | Reviewers still get matrix/dashboard-style sheets without making the wide matrix the canonical storage format | — Pending |
| Build an offline fixture path before live Google writes | It lets us validate schemas, transformations, and views without OCR/LLM cost or Google API dependence | — Pending |
| Defer native Google Sheets pivot tables/charts from v1 | Code-generated tables are easier to test and control; native pivots can be added after the raw/view contracts settle | — Pending |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition** (via `$gsd-transition`):
1. Requirements invalidated? -> Move to Out of Scope with reason
2. Requirements validated? -> Move to Validated with phase reference
3. New requirements emerged? -> Add to Active
4. Decisions to log? -> Add to Key Decisions
5. "What This Is" still accurate? -> Update if drifted

**After each milestone** (via `$gsd-complete-milestone`):
1. Full review of all sections
2. Core Value check -> still the right priority?
3. Audit Out of Scope -> reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-06-28 after initialization*
