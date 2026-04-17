# Tool Catalog

This file lists the agent tools, their intent, key params, and how exposure is configured.

## LLM backend

- `llm.provider` in `config/config.yaml` selects the HTTP backend for all agent reasoning and vision LLM calls (`ollama` or `gemini`).
- **Ollama**: uses `ollama.base_url`, `ollama.reasoning_model`, `ollama.vision_model`.
- **Gemini**: uses the Generative Language API; set `gemini.api_key_env` (default `GOOGLE_API_KEY`) and `gemini.reasoning_model` / `gemini.vision_model` (defaults in repo: **Flash** for both — cost/latency). Set `llm.provider: gemini` to use it. Implementation: `src/llm/` (`build_llm_provider`, `GeminiProvider`).
- **Timeouts** (`timeout_cfg` / `llm_timeouts`): for Gemini, precedence is `gemini.timeout_*` → `llm.timeout_*` → `ollama.timeout_*` → defaults. For Ollama: `ollama.timeout_*` → `llm.timeout_*` → defaults.
- **Remote guardrails** (`llm.remote_guard` and optional `gemini.remote_guard` merge, Gemini wins): `max_llm_requests_per_run`, `max_chat_requests_per_run`, `max_generate_requests_per_run`, `max_total_token_count_per_run`, `warn_token_threshold`. The repo ships **default numeric limits** under `llm.remote_guard` so switching to Gemini is not uncapped by mistake. When any limit is set **and** `llm.provider` is `gemini`, calls go through `MeteredLLMProvider` (counters reset at each `InvoiceAgent.run()`). With `ollama`, these keys are ignored.
- **Prompt caps** (`agent.prompt_profile`: `auto` | `local` | `remote`): `auto` uses larger history/learnings caps for non-Ollama providers. Set `learnings_max_chars`, `planning_learnings_max_chars`, or `history_preview_chars` to an integer to override; use YAML `null` to use the profile default for that field. Defaults in `config/config.yaml` set `history_preview_chars` and `learnings_max_chars` explicitly for cost control with remote APIs.
- **State summary caps** (`agent.state_summary_max_page_lines`, `state_summary_max_inventory_lines`, `state_summary_inventory_desc_chars`): truncate long page path lists and page-inventory rows in `AgentState.summary_for_prompt` so multi-page PDFs do not explode text tokens each turn.
- **OCR injection cap** (`agent.ocr_prompt_max_chars`): truncates Surya OCR text passed into `extract_fields_vision` prompts (full-page and crop paths).
- **Batch auto-expansion** (`agent.batch_auto_expand`, default `true`): when the agent calls `extract_fields_vision` for a `field_subset`, the wrapper automatically adds every other field that has never been attempted. All un-tried fields get a free ride in the same vision model call, cutting total turns by 60–70%. Set to `false` to disable and test targeted single-field extraction.
- **Tool description overrides** (`agent.tool_descriptions_path`, default `config/tool_descriptions.yaml`): maps tool names to replacement description strings shown in the system prompt. Only list tools you want to override; everything else falls back to the hardcoded text in `src/agent/prompts.py`.
- **Phase-to-tool mappings** (`config/phase_tools.yaml`): YAML listing which tools are available in each phase (`SCAN`, `EXTRACT`, `VALIDATE`). Edit without touching Python. Absent file → hardcoded fallback in `src/agent/phases.py`.

### Vision / `generate_json` cost (Gemini or any remote VL)

Each **`chat_json`** call is one reasoning/planning/reflection step. Each **`generate_json`** call sends at least one image + prompt. Main sites:

| Location | Calls | Notes |
|----------|--------|--------|
| [`src/tools/page_inventory.py`](../src/tools/page_inventory.py) `inventory_pages` | **One per PDF page** | Dominates for long documents; `compress_pages` first uses smaller thumbnails but still one request per page. |
| [`src/tools/vision_llm.py`](../src/tools/vision_llm.py) `classify_document_type` | 1 | First page classification. |
| [`src/tools/vision_llm.py`](../src/tools/vision_llm.py) `extract_fields_vision` | Per extraction (often per page / crop group) | Full-res or medium image bytes; OCR text capped by `ocr_prompt_max_chars`. |
| [`src/tools/compliance_visual.py`](../src/tools/compliance_visual.py) `check_compliance_visual` | Batched | Multiple pages in one call, capped by `visual_max_evidence_pages`, images resized before send. |

Use `llm.remote_guard.max_generate_requests_per_run` to bound vision calls per run; raise it if you routinely process PDFs with more pages than the default budget allows.

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

## Customization Without Code Changes

### Tool description overrides (`config/tool_descriptions.yaml`)

Map any tool name to a replacement description string.  Only listed tools are overridden; everything else falls back to the hardcoded text in `src/agent/prompts.py`.  Path is controlled by `agent.tool_descriptions_path` in `config/config.yaml`.

```yaml
# config/tool_descriptions.yaml
extract_fields_vision: |
  extract_fields_vision(page_num, region="", hints="", field_subset=null)
    Custom override: your project-specific extraction guidance here.
note: |
  note(text)
    Record a private observation (not saved to disk).
```

### Phase-to-tool mappings (`config/phase_tools.yaml`)

Lists which tools are callable in each phase.  `note` and `install_package` are always merged in automatically.  Edit to add, remove, or reassign tools across phases without touching Python.  If the file is absent, the hardcoded fallback in `src/agent/phases.py` is used.

```yaml
# config/phase_tools.yaml
SCAN:
  - inspect_file
  - compress_pages
  - inventory_pages
  - classify_document_type
  - read_learnings

EXTRACT:
  - convert_pdf_to_images
  - extract_fields_vision
  - crop_region
  - flag_for_human_review
  - flag_fields_for_review
  - read_learnings
  - write_learning
  - edit_learning
  - delete_learning
  - check_compliance

VALIDATE:
  - check_compliance
  - check_compliance_visual
  - extract_fields_vision
  - crop_region
  - flag_for_human_review
  - flag_fields_for_review
  - write_learning
  - edit_learning
  - delete_learning
  - finish
```

### Adding a new tool

Three edits required:
1. Register it in `src/agent/registry.py` (`build_tool_registry`) with a factory from `src/tools/tool_wrappers.py`.
2. List it in the relevant phase(s) in `config/phase_tools.yaml`.
3. Add it to the relevant group(s) in `src/agent/tool_policy.py` (`TOOL_GROUPS`) if it should be accessible via group-based access control.

