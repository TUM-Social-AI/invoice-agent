"""
Centralized LLM prompts for vision tools, compliance visuals, planning, and repair hints.

Keep content domain-generic: invoice-type specifics belong in config CSV and learnings
(`### vision_model_extraction`), not here.
"""

from __future__ import annotations

from typing import Optional

from src.agent.state import AgentState
from src.learnings.vision_hints import vision_model_extraction_bullets

# --- Document classification (first page) ---


def classify_document_type_prompt(type_descriptions_block: str) -> str:
    """
    type_descriptions_block: newline-separated "- \"TYPE_ID\": ..." lines from config.
    """
    return f"""You are an invoice classification expert. Examine the first page image and choose the single best matching type from the list below.

Use layout, language, headings, logos, and line-item structure — not filename or assumptions.

Available types:
{type_descriptions_block}

Respond with ONLY valid JSON, no markdown fences:
{{"invoice_type_id": "<one id from the list>", "confidence": 0.0-1.0, "reasoning": "one concise sentence citing visible cues"}}

Rules:
- Pick exactly one invoice_type_id from the list; never invent new ids.
- Lower confidence when the page is ambiguous, blank, or unlike any listed type."""


# --- Field extraction (per page / crop) ---

_EXTRACTION_LANG_NOTE = (
    "The document may be in French, Spanish, English, or another language, or a mix. "
    "Map what you read to the JSON keys below (keys stay in English snake_case). "
    "Recognize synonymous labels across languages (e.g. French 'Nom', Spanish 'Nombre', English 'Name' for a person field).\n\n"
)

_EXTRACTION_INTRO = """You are a structured document extraction specialist. Your task is to read the page image (and optional OCR transcript) and fill the schema below.

Priorities:
- Use only information visible on this image (and OCR text if provided). Do not guess from world knowledge.
- When printed text and handwriting disagree for the same field, prefer the clearest authoritative source (often printed totals or table cells).
- Amounts: preserve decimal separators as in the document; strip thousand separators only when unambiguous.
- If a field is not present on this page, use null — do not copy values from unrelated lines (e.g. a date into payment_method)."""


def build_extract_fields_vision_prompt(
    *,
    text_section: str,
    hints: str,
    accuracy_block: str,
    fields_text: str,
) -> str:
    """
    text_section: OCR block including labels, or empty string.
    hints: optional agent hints for this call.
    accuracy_block: output of format_extraction_accuracy_block(state).
    fields_text: bullet list of fields from schema.
    """
    parts = [
        _EXTRACTION_INTRO,
        "",
        _EXTRACTION_LANG_NOTE.rstrip(),
    ]
    if text_section:
        parts.extend(["", text_section.rstrip()])
    if hints.strip():
        parts.extend(["", "Additional hints for this call:", hints.strip()])
    parts.extend(["", accuracy_block.strip(), ""])
    parts.extend(
        [
            "Extract the fields listed below. Use null only when the value is truly absent from what you can see.",
            "If a value is visible with reasonable confidence (including handwritten or stamped text), extract it and set the matching *_confidence between 0 and 1.",
            "",
            fields_text,
            "",
            "Return ONLY a valid JSON object whose keys are exactly the field names above plus each field's *_confidence key.",
            "Confidence keys use the pattern <field_name>_confidence with values from 0.0 to 1.0.",
            "Example:",
            "{{",
            '  "vendor_name": "Acme GmbH",',
            '  "vendor_name_confidence": 0.95,',
            '  "invoice_number": null,',
            '  "invoice_number_confidence": 0.0',
            "}}",
            "",
            "Do not include any text outside the JSON object.",
        ]
    )
    return "\n".join(parts)


# --- Generic extraction rules + learnings-driven vision hints ---

_EXTRACTION_ACCURACY_BASE: tuple[str, ...] = (
    "Extraction accuracy (follow strictly):",
    "- Copy values verbatim from the page; do not invent digits or letters.",
    "- When OCR text and the image disagree on a name or amount, prefer the clearest readable pixels.",
    "- Do not use a job title or employer/org line as employee_name; use the person's given + family name.",
    "- Never use a bare date token (e.g. DD/MM) or a tiny numeric fragment as payment_method; use payer/channel wording or null.",
    "- Do not use only a budget code (NN.NN) for expense_category if a descriptive label appears nearby; prefer the label.",
    "- For pay_period use MM/YYYY or month name + year as text—not a lone month number 1–12 as the whole field.",
)


def format_extraction_accuracy_block(state: Optional["AgentState"]) -> str:
    """Generic rules plus optional bullets from `### vision_model_extraction` in learnings."""
    lines = list(_EXTRACTION_ACCURACY_BASE)
    ctx = ""
    if state is not None:
        ctx = getattr(state, "learnings_context", "") or ""
    extra = vision_model_extraction_bullets(ctx)
    if extra:
        lines.append("Document-specific vision hints (from learnings):")
        lines.extend(f"- {b}" for b in extra)
    return "\n".join(lines)


def ocr_transcript_section(text_context: str) -> str:
    """Wrap OCR text for injection into extraction prompts."""
    if not (text_context or "").strip():
        return ""
    return (
        "[OCR transcript — may contain line breaks, column bleed, or spelling errors; "
        "cross-check every value against the image pixels]\n"
        f"{text_context}\n\n"
    )


# --- Visual compliance (multi-page) ---


def build_compliance_visual_prompt(evidence_lines_block: str, rule_lines: str) -> str:
    """
    evidence_lines_block: joined lines describing each image index and page_num.
    rule_lines: newline-separated rule descriptions from config.
    """
    return f"""You are a document compliance inspector. You receive one or more page images in order.

Evidence (image order → document page):
{evidence_lines_block}

For each requirement below, decide pass or fail using evidence across all images. Reference page_num=N when you cite where something appears.

Requirements:
{rule_lines}

Respond with ONLY valid JSON — one top-level key per rule_id, no markdown fences. Shape per rule:
{{
  "RULE_ID": {{
    "passes": true,
    "confidence": 0.0-1.0,
    "observation": "one or two short sentences: what you saw, where (page_num), and why pass/fail",
    "field_updates": {{}}
  }}
}}

Optional "field_updates": only when this rule directly supports a value — a flat map of extraction field names (same names as the invoice schema, e.g. employee_name, payment_method) to short strings. Use {{}} or omit if nothing to add. Never invent values."""


# --- Page inventory (per page, category + description) ---


def page_inventory_prompt() -> str:
    return (
        "Examine this single page image. Do two things:\n\n"
        "1. Choose EXACTLY one category from the fixed list:\n"
        "   INVOICE_HEADER   — vendor/client block, invoice number, dates, references, letterhead\n"
        "   LINE_ITEMS       — tables of services/products, quantities, unit prices, mileage lines\n"
        "   TOTALS           — subtotals, taxes, grand total, bank/IBAN, payment summary\n"
        "   SIGNATURE_STAMP  — signatures, stamps, seals, approvals, discharge blocks\n"
        "   SUPPORTING_DOC   — receipts, quotes, contracts, tickets, boarding passes, photos, timesheets\n"
        "   COVER_PAGE       — title page, transmittal, cover letter, project summary without invoice body\n"
        "   BLANK            — empty or nearly empty\n\n"
        "2. Write a specific description (max 15 words) of what is literally visible on THIS page.\n"
        "   Name organisations, document titles, amounts, languages, or notable stamps — avoid generic filler.\n\n"
        'Respond with ONLY valid JSON: {"category": "<one of the above>", "description": "..."}'
    )


def page_inventory_batch_prompt(page_count: int) -> str:
    return (
        f"You are given {page_count} page images in order (page 1 first). "
        "For EACH page classify it and write a short description.\n\n"
        "Categories (pick exactly one per page):\n"
        "   INVOICE_HEADER   — vendor/client block, invoice number, dates, references, letterhead\n"
        "   LINE_ITEMS       — tables of services/products, quantities, unit prices, mileage lines\n"
        "   TOTALS           — subtotals, taxes, grand total, bank/IBAN, payment summary\n"
        "   SIGNATURE_STAMP  — signatures, stamps, seals, approvals, discharge blocks\n"
        "   SUPPORTING_DOC   — receipts, quotes, contracts, tickets, boarding passes, photos, timesheets\n"
        "   COVER_PAGE       — title page, transmittal, cover letter, project summary without invoice body\n"
        "   BLANK            — empty or nearly empty\n\n"
        "Description: max 15 words, name organisations/amounts/stamps visible on that specific page.\n\n"
        'Respond with ONLY valid JSON: {"pages": [{"category": "...", "description": "..."}, ...]}'
    )


# --- Planning (one-shot JSON plan after classify) ---

PLANNING_SYSTEM_MESSAGE = (
    "You are a planning assistant for invoice processing pipelines. "
    "Output only valid JSON matching the requested schema. No markdown, no commentary."
)


def build_planning_user_prompt(
    *,
    file_name: str,
    invoice_type_id: str,
    type_display: str,
    inventory_hint: str,
    learnings_hint: str,
) -> str:
    return f"""You are planning the EXTRACT and VALIDATE phases for a classified invoice-like document.

File: {file_name}
Document type: {invoice_type_id} ({type_display})

{inventory_hint}

{learnings_hint}

SCAN is already done (compress_pages, inventory_pages, classify_document_type). Plan only extraction and validation.

Return a JSON object with a top-level "plan" key whose value is an array of steps. Each step must include:
- "step": integer (1-based order)
- "tool": exact tool name to call
- "rationale": one sentence tying the step to page roles or document structure

Include at least six steps covering:
1. convert_pdf_to_images — full-quality render for extraction (if not already implied complete)
2. extract_fields_vision — header / primary data page (name the page_num from inventory)
3. extract_fields_vision — additional pages if inventory shows LINE_ITEMS, TOTALS, SUPPORTING_DOC, etc.
4. check_compliance — field-based rules
5. check_compliance_visual — when stamps/signatures/payment proof need pixels (use inventory page_num)
6. finish — record outcome

Merge pages with the same role into one extract_fields_vision call when sensible. Adapt to the real inventory and learnings. Return ONLY valid JSON."""


# --- Reasoning repair (invalid tool JSON) ---


def action_json_repair_user_content(validation_err: str) -> str:
    return (
        "Your previous JSON action was invalid.\n"
        f"Validation error: {validation_err}\n"
        "Return ONLY corrected JSON with a valid tool name and params.\n"
        "extract_fields_vision and check_compliance_visual require page_num (integer).\n"
        "Do not invent image_path strings. Retries must change at least one of: "
        "page_num, region, hints, or field_subset."
    )


# --- Reflection / learning mode ---


def build_reflection_learning_prompt(diff_text: str, session_notes: list[str]) -> str:
    notes_block = "\n".join(f"  - {n}" for n in session_notes) if session_notes else "  (none)"
    return f"""You have just processed an invoice in LEARNING MODE.
Your normal run is complete. You will receive verified ground truth and should reflect on performance.

{diff_text}

Your session notes from this run:
{notes_block}

Task: call write_learning() with specific, actionable insights for future runs — focus on WHY something failed and HOW to avoid it.

Categories (content string only; invoice_type_id separate):
- "approaches" — overall strategy for this document type
- "extraction_patterns" — where/how to find fields reliably
- "common_failures" — misses and recovery
- "compliance_edge_cases" — unexpected rule behaviour
- "tool_suggestions" — missing capabilities (often invoice_type_id="GENERAL")

When finished, call finish(reason="reflection_complete", all_errors_resolved=false).
Respond ONLY with valid JSON: {{"tool": "...", "params": {{}}, "reasoning": "..."}}"""
