# Tool Catalog

This file lists the agent tools, their intent, key params, and how exposure is configured.

## LLM backend

- `llm.provider` in `config/config.yaml` selects the HTTP backend for all agent reasoning and vision LLM calls (`ollama` or `gemini`).
- **Ollama**: uses `ollama.base_url`, `ollama.reasoning_model`, `ollama.vision_model`.
- **Gemini**: uses the Generative Language API; set `gemini.api_key_env` (default `GOOGLE_API_KEY`) and `gemini.reasoning_model` / `gemini.vision_model` (Flash models are a good default for cost/latency). Implementation lives under `src/llm/` (`build_llm_provider`, `GeminiProvider`).
- **Timeouts** (`timeout_cfg` / `llm_timeouts`): for Gemini, precedence is `gemini.timeout_*` → `llm.timeout_*` → `ollama.timeout_*` → defaults. For Ollama: `ollama.timeout_*` → `llm.timeout_*` → defaults.
- **Remote guardrails** (`llm.remote_guard` and optional `gemini.remote_guard` merge, Gemini wins): optional `max_llm_requests_per_run`, `max_chat_requests_per_run`, `max_generate_requests_per_run`, `max_total_token_count_per_run`, `warn_token_threshold`. When any limit is set, Gemini calls go through `MeteredLLMProvider` (counters reset at each `InvoiceAgent.run()`).
- **Prompt caps** (`agent.prompt_profile`: `auto` | `local` | `remote`): `auto` uses larger history/learnings caps for non-Ollama providers. Set `learnings_max_chars`, `planning_learnings_max_chars`, or `history_preview_chars` to an integer to override; use YAML `null` to use the profile default for that field.

## Mode Matrix

- `agent.orchestration: loop`
  - Agentic mode (LLM chooses next tool call).
  - Tool visibility is controlled by `agent.tool_groups_enabled`, `agent.learnings_tools_enabled`, and allow/deny overrides.
- `agent.orchestration: pipeline`
  - Deterministic debug fallback.
  - Calls tools in a fixed order from `src/agent/pipeline.py`.

## Tool Groups (loop mode)

- `pipeline` (default enabled):
  - `inspect_file`, `compress_pages`, `inventory_pages`, `classify_document_type`
  - `convert_pdf_to_images`, `extract_fields_vision`, `crop_region`
  - `check_compliance`, `check_compliance_visual`
  - `flag_for_human_review`, `flag_fields_for_review`
  - `finish`, `note`
- `granular`:
  - Reserved for future detailed/micro tools.
- `learnings`:
  - `read_learnings`, `write_learning`, `edit_learning`, `delete_learning`

## Tool Reference

- `inspect_file()`
  - Reads basic file metadata (size/pages/format).

- `compress_pages(dpi, quality, max_width)`
  - Renders low-res thumbnails for fast inventory/classification.

- `inventory_pages()`
  - Classifies each page role (`INVOICE_HEADER`, `LINE_ITEMS`, `TOTALS`, `SIGNATURE_STAMP`, `SUPPORTING_DOC`, etc.).
  - Builds `state.page_facts` for downstream evidence linking.

- `classify_document_type()`
  - Vision-driven invoice type classification.

- `convert_pdf_to_images(dpi)`
  - Renders full-quality pages for extraction/compliance.

- `crop_region(page_num|image_path, region, custom_bbox?)`
  - Crops named regions or custom bboxes.

- `extract_fields_vision(page_num|image_path, region?, hints?, field_subset?)`
  - Main extraction tool.
  - Internally combines OCR layout localization, crop-based extraction, and full-page fallback.
  - Merges confidence-aware field updates into state.

- `check_compliance()`
  - Runs deterministic field-based rules.
  - Produces `visual_checks_pending` for visual rules.

- `check_compliance_visual(page_num|image_path)`
  - Runs visual rules using **multi-page evidence**.
  - Anchor page comes from caller (`page_num`), then tool expands evidence pages from `state.page_inventory`/`state.page_facts` and rule heuristics.
  - Sends one multi-image vision call (`agent.visual_max_evidence_pages` cap).
  - Updates `state.rule_evidence[rule_id]["refs"]` with pages used and observations.

- `flag_for_human_review(field_name, reason)`
  - Marks fields for manual review.

- `note(text)`
  - Adds session notes (ephemeral, per-run memory).

- `read_learnings()`, `write_learning(...)`, `edit_learning(...)`, `delete_learning(...)`
  - CRUD for persistent learnings markdown.

- `finish(reason, all_errors_resolved)`
  - Finalizes run status with evidence/visual pending guards.

