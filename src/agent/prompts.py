"""Prompt builders and tool docs."""

import json
import logging
import re
import requests
from typing import Any

from src.agent.state import AgentState
from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.llm.config_resolve import active_rule_groups_from_config
from src.agent.phases import current_phase as _current_phase, next_required_step as _next_required_step
from src.agent.tool_policy import (
    parse_tool_description_blocks,
    load_tool_description_overrides,
    render_tool_documentation,
)
from src.prompts.llm_prompts import build_reflection_learning_prompt

logger = logging.getLogger(__name__)

TOOL_DESCRIPTIONS = """
Available tools — respond with JSON: {"tool": "name", "params": {}, "reasoning": "why"}

inspect_file()
  → filename, size_mb, page_count, format, suggest_compression
compress_pages(dpi=48, quality=30, max_width=1400)
  → render pages at low resolution into tmp/; ALWAYS call this before inventory_pages,
    even for small files — inventory is much faster on thumbnails than full-res images.
    Low-res copies are remembered separately so convert_pdf_to_images can still render
    full quality for extraction without losing the thumbnails.
classify_document_type()
  → vision model identifies invoice type from first page; call after pages are rendered
convert_pdf_to_images(dpi=150)
  → render all pages at full quality into output/pages/
crop_region(image_path, region, page_num, custom_bbox=null)
  regions: header | footer | address_block | totals | line_items | body
  image_path aliases accepted: image_path, page_path, page_image_path, image, path, file_path
  page_num  aliases accepted: page_num, page_index, page, page_number
extract_fields_vision(page_num, region="", hints="", field_subset=null)
  REQUIRED: page_num=N (integer). Paths are resolved from rendered pages — never pass image_path.
  page_num aliases: page_num, page_index, page, page_number
  field_subset aliases: field_subset, fields, field_names, regions
  Retries must change at least one of: page_num, region, hints, or field_subset.
  NEVER pass category or field_name to extract_fields_vision (those belong to learning/review tools).
  After convert_pdf_to_images, hybrid mode may run medium-res first internally, then promote to full-res.
  Returns: {"extracted": {...}, "merge_result": {"updated": [...], "already_have_better": [...], "null_fields": [...]}}
    updated          = fields where the new value replaced the old (improvement)
    already_have_better = fields we ALREADY have with higher confidence — DO NOT RETRY THESE.
      If already_have_better is non-empty and updated is empty → extraction found nothing new.
      STOP retrying this page/field combination. The existing stored values are the best available.
      Move on to check_compliance or finish instead.
    null_fields      = fields the model returned null for (could not extract from this image).
      If updated=[] AND already_have_better=[] AND null_fields is non-empty → model returned null
      for all requested fields. Do NOT retry the same page+fields. Instead: use crop_region to
      zoom in on the relevant region, add a more specific hint, try check_compliance_visual, or
      flag the fields for human review.
flag_for_human_review(field_name, reason)   — also accepts: fields_to_flag=[...], fields=[...]
check_compliance()
  → validates extracted field values against all non-visual rules
  → always check the "visual_checks_pending" list in the result — if it is non-empty,
    you MUST call check_compliance_visual before finish (finish will be rejected otherwise)
check_compliance_visual(page_num)
  → REQUIRED when visual_checks_pending is non-empty after check_compliance
  → evaluates all visual_check rules (stamps, signatures, letterhead, seals, etc.)
  → target the SIGNATURE_STAMP or INVOICE_HEADER page from the page inventory
  → REQUIRED: page_num=N (integer) only — paths come from rendered pages
note(text)
  → write a private observation about this file into session memory (visible in state summary, not saved to disk)
read_learnings()
  → returns all stored learnings for this invoice type + GENERAL; each entry has an ID like [L042]
  → call at the START of every session to load prior knowledge before extracting any fields
write_learning(category, content, invoice_type_id=null)
  → appends a new learning; returns {"learning_id": "L042", ...} — save the ID if you may edit it later
  → skips silently if identical content already exists (exact dedup)
  categories: approaches | extraction_patterns | vision_model_extraction | common_failures | compliance_edge_cases | tool_suggestions
  set invoice_type_id="GENERAL" for cross-type insights and tool suggestions
edit_learning(learning_id, new_content)
  → replace the content of learning [L042] in-place (original date preserved, [edited] suffix added)
  → use when a previous learning was wrong, incomplete, or misleading
delete_learning(learning_id)
  → permanently remove learning [L042]; the ID is retired and never reused
  → use when a learning is completely wrong or no longer applicable at all
inventory_pages()
  → classifies each page into a fixed category + short description
  → categories: INVOICE_HEADER | LINE_ITEMS | TOTALS | SIGNATURE_STAMP | SUPPORTING_DOC | COVER_PAGE | BLANK
  → automatically uses low-res compressed thumbnails if compress_pages was called (much faster)
  → result visible in state summary; use categories to decide which pages to target
  → MUST call compress_pages(dpi=48, quality=30) before this, then convert_pdf_to_images for extraction
install_package(package)
  → pip-install a package into the active conda env; call when a tool fails with "not installed" or ImportError, then retry the tool
finish(reason, all_errors_resolved)
  reason: "compliance_passed" | "max_retries" | "human_review_needed" | "unrecoverable_error"
  Note: "compliance_passed" means no blocking ERROR-severity rule failures (and no incomplete
  error evidence). WARNING-severity rules may still be non-pass — check warning_failures in the result.
  The returned status_explanation summarizes this.
"""

def build_system_prompt(
    state: AgentState,
    store: ConfigStore,
    config: dict,
    allowed_tool_names: "set[str] | None" = None,
    max_field_retries: int = 3,
    confidence_threshold: float = 0.65,
) -> str:
    agent_cfg = config.get("agent", {})
    _page_dpi = int(agent_cfg.get("page_dpi", 150))
    learnings_inject_enabled = bool(agent_cfg.get("learnings_inject_enabled", True))
    if not state.invoice_type_id:
        type_context = (
            "The invoice type is not yet known.\n\n"
            "Suggested starting approach (adapt as needed):\n"
            "- read_learnings() to check past approaches for this kind of document\n"
            "- inspect_file() to understand the file before doing heavy work\n"
            "- render pages (compress_pages if large, otherwise convert_pdf_to_images)\n"
            "- classify_document_type() once you have a page image to look at\n"
            "- then extract fields and run compliance checks\n\n"
            "Available invoice types:\n" +
            "\n".join(
                f'  - {tid}: {t.display_name} — {t.description}'
                for tid, t in store.invoice_types.items()
            )
        )
    else:
        type_context = store.build_agent_context(
            state.invoice_type_id,
            active_rule_groups_from_config(config),
        )

    improvement_guidance = f"""
Working style:
- Recommended two-phase approach for ALL multi-page documents (even small ones):
  Phase 1 (structure): compress_pages(dpi=48, quality=30) → inventory_pages() → classify_document_type()
    ALWAYS call compress_pages before inventory_pages — even for small files. The
    inventory vision model runs much faster on low-res thumbnails than on full-quality
    images. compress_pages(dpi=48) saves the thumbnails and inventory_pages uses them
    automatically, even after convert_pdf_to_images is called later.
    inventory_pages() returns a fixed category (INVOICE_HEADER, LINE_ITEMS, TOTALS,
    SIGNATURE_STAMP, SUPPORTING_DOC, COVER_PAGE, BLANK) for every page plus a short
    description. Use this map to target extraction — don't guess which page has what.
  Phase 2 (extraction): convert_pdf_to_images(dpi={_page_dpi}) → targeted extraction on the right pages
    Extract all fields expected on the same page in a SINGLE extract_fields_vision call.
    Do NOT call extract_fields_vision once per field — pass the full field_subset for
    that page in one call. This saves turns and reduces redundant vision model load.
    Example: fields invoice_number, invoice_date, vendor_name all live on the INVOICE_HEADER
    page → one call with field_subset=["invoice_number","invoice_date","vendor_name"].
- OCR pre-pass: extract_fields_vision automatically runs OCR on the page image before
  calling the vision model. The OCR text is injected as context — you don't need to
  do anything extra. Confidence tends to be higher when OCR is clean.
- inspect_file() will tell you the file size. If suggest_compression is true, run compress_pages() first before convert_pdf_to_images() — this avoids slow rendering of large files.
- Use note(text) to record observations about this specific file as you go — e.g. "document is a scanned hotel receipt", "IVA not shown, likely factura simplificada". These notes are visible in your state summary each turn and help you stay consistent across turns without repeating work.
- Extraction first, compliance second: extract as many fields as possible before running check_compliance so you have a complete picture before deciding what to retry.
- Adapt when something fails: if extraction is low confidence on a page, try crop_region to zoom in, or add a more specific hint. NEVER repeat the exact same call — always change something.
- Before finishing: write_learning(category="approaches") with what worked and what didn't.
- read_learnings() at the start of a new document gives you context from past runs.
- Evidence-gap priority: when state summary shows missing evidence slots for ERROR rules,
  prioritize actions that fill those gaps before warning-level cleanup.
"""

    phase = _current_phase(state)
    next_step = _next_required_step(state)
    next_step_line = f"\nYour immediate next tool call MUST be: {next_step}" if next_step else ""
    phase_guidance = {
        "SCAN": (
            f"CURRENT PHASE: SCAN — only scanning/classification tools are available.{next_step_line}\n"
            "Complete order: compress_pages → inventory_pages → classify_document_type.\n"
            "Do NOT attempt to extract fields or run compliance in this phase."
        ),
        "EXTRACT": (
            f"CURRENT PHASE: EXTRACT — only extraction tools are available.{next_step_line}\n"
            "Complete order: convert_pdf_to_images → extract_fields_vision (per page) → check_compliance.\n"
            "Do NOT call finish or check_compliance_visual before running check_compliance."
        ),
        "VALIDATE": (
            f"CURRENT PHASE: VALIDATE — only validation/finish tools are available.{next_step_line}\n"
            "Resolve failed rules, call check_compliance_visual if visual checks are pending, then finish."
        ),
    }[phase]

    learnings_block = ""
    if learnings_inject_enabled and state.learnings_context:
        learnings_block = f"\nLoaded learnings for this run (capped):\n{state.learnings_context}\n"

    # Tool descriptions should match what the LLM can actually call.
    tool_doc = TOOL_DESCRIPTIONS
    tool_doc = tool_doc.replace(
        "convert_pdf_to_images(dpi=150)",
        f"convert_pdf_to_images(dpi={_page_dpi})",
    )
    if not bool(agent_cfg.get("hybrid_extraction", True)):
        tool_doc = tool_doc.replace(
            "After convert_pdf_to_images, hybrid mode may run medium-res first internally, then promote to full-res.",
            "Hybrid extraction is disabled: vision uses full-resolution page images only (no medium-res pass).",
        )
    if allowed_tool_names is not None:
        # Cache parsed blocks on the function for speed.
        cache = getattr(build_system_prompt, "_tool_desc_cache", None)
        if cache is None:
            header, blocks = parse_tool_description_blocks(TOOL_DESCRIPTIONS)
            setattr(build_system_prompt, "_tool_desc_cache", (header, blocks))
            cache = getattr(build_system_prompt, "_tool_desc_cache")
        header, blocks = cache
        tool_overrides_path = agent_cfg.get("tool_descriptions_path", "config/tool_descriptions.yaml")
        overrides = load_tool_description_overrides(tool_overrides_path)
        tool_doc = render_tool_documentation(
            header=header,
            blocks=blocks,
            overrides=overrides,
            allowed_tool_names=set(allowed_tool_names),
        )
        tool_doc = tool_doc.replace(
            "convert_pdf_to_images(dpi=150)",
            f"convert_pdf_to_images(dpi={_page_dpi})",
        )
        if not bool(agent_cfg.get("hybrid_extraction", True)):
            tool_doc = tool_doc.replace(
                "After convert_pdf_to_images, hybrid mode may run medium-res first internally, then promote to full-res.",
                "Hybrid extraction is disabled: vision uses full-resolution page images only (no medium-res pass).",
            )

    return f"""You are an invoice compliance agent that learns and improves with each document it processes.

{phase_guidance}

{type_context}

{tool_doc}

{learnings_block}

{improvement_guidance}
Hard rules:
- Do not retry a field more than {max_field_retries} times — flag for human review instead
- A field is considered reliably extracted when confidence ≥ {confidence_threshold}
- Call check_compliance after extraction updates
- If check_compliance returns visual_checks_pending (non-empty list), you MUST call
  check_compliance_visual(page_num=N) on the SIGNATURE_STAMP or INVOICE_HEADER page
  before calling finish — finish() will be rejected with an error if visual checks are pending
- When all error-level rules pass AND visual_checks_pending is empty, finish with all_errors_resolved=true
  (warning-level rule failures alone do not block PASSED — read error_failures vs warning_failures on finish)
- Always use page_num=N (an integer like 1, 2, 3) — NEVER construct or guess image file paths
- When retrying extract_fields_vision or check_compliance_visual, change parameters (page_num, region, hints, or field_subset) — do not repeat identical params
- Respond ONLY with valid JSON."""

def build_reflection_prompt(state: AgentState, diff_text: str, store: ConfigStore) -> str:
    return build_reflection_learning_prompt(diff_text, list(state.session_notes))
