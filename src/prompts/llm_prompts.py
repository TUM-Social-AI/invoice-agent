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


def _filename_hint_section(filename_hint: str) -> str:
    return f"\nFile name hint:\n{filename_hint}\n" if filename_hint else ""


def classify_document_type_prompt(type_descriptions_block: str, filename_hint: str = "") -> str:
    """
    type_descriptions_block: newline-separated "- \"TYPE_ID\": ..." lines from config.
    filename_hint: optional budget-line prior derived from the file name.
    """
    return f"""You are an invoice classification expert. Examine the first page image and choose the single best matching type from the list below.

Use layout, language, headings, logos, and line-item structure. A file name hint, if given, is only a prior; the page decides.

Available types:
{type_descriptions_block}
{_filename_hint_section(filename_hint)}
Respond with ONLY valid JSON, no markdown fences:
{{"invoice_type_id": "<one id from the list>", "confidence": 0.0-1.0, "reasoning": "one concise sentence citing visible cues"}}

Rules:
- Pick exactly one invoice_type_id from the list; never invent new ids.
- Lower confidence when the page is ambiguous, blank, or unlike any listed type."""


def classify_from_inventory_prompt(
    type_descriptions_block: str, inventory_block: str, filename_hint: str = ""
) -> str:
    """
    Text-only classification from the page inventory (category + description per page).
    type_descriptions_block: newline-separated "- \"TYPE_ID\": ..." lines from config.
    inventory_block: one "- page N: CATEGORY — description" line per page.
    filename_hint: optional budget-line prior derived from the file name.
    """
    return f"""You are an invoice classification expert. Below is an inventory of every page of one expense document
(page category + a short description of what is visible). The first page is often a generic internal fund-request
form; decide from what is actually being paid for across all pages (the supplier invoice, line items, receipts).

Available types:
{type_descriptions_block}

Page inventory:
{inventory_block}
{_filename_hint_section(filename_hint)}
Respond with ONLY valid JSON, no markdown fences:
{{"invoice_type_id": "<one id from the list>", "confidence": 0.0-1.0, "reasoning": "one concise sentence citing the pages that decided it"}}

Rules:
- Pick exactly one invoice_type_id from the list; never invent new ids.
- Lower confidence when the pages describe nothing that clearly fits a listed type."""


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
- Amounts: copy the amount exactly as printed, with its thousands separators, decimal mark and currency mark
  (e.g. "425 000", "50.000 F", "$3,758.00"); do not convert or reformat it.
- If a field is not present on this page, use null — do not copy values from unrelated lines (e.g. a date into payment_method)."""


def build_extract_fields_vision_prompt(
    *,
    text_section: str,
    hints: str,
    accuracy_block: str,
    fields_text: str,
    cite_lines: bool = False,
) -> str:
    """
    text_section: OCR block including labels, or empty string.
    hints: optional agent hints for this call.
    accuracy_block: output of format_extraction_accuracy_block(state).
    fields_text: bullet list of fields from schema.
    cite_lines: OCR lines carry [L<n>] IDs; ask for a <field>_evidence list per field.
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
        ]
    )
    if cite_lines:
        parts.extend(
            [
                "Return ONLY a valid JSON object whose keys are exactly the field names above plus each field's *_confidence and *_evidence keys.",
                "Confidence keys use the pattern <field_name>_confidence with values from 0.0 to 1.0.",
                "Evidence keys use the pattern <field_name>_evidence: the IDs of the OCR transcript lines (e.g. [\"L12\"]) "
                "on which you read that value. Cite only lines that contain the value itself (not just its label); "
                "cite several IDs when the value spans lines. Use [] when the value is null or not in the OCR transcript "
                "(e.g. handwriting the OCR missed). Never invent IDs.",
                "Example:",
                "{{",
                '  "vendor_name": "Acme GmbH",',
                '  "vendor_name_confidence": 0.95,',
                '  "vendor_name_evidence": ["L3"],',
                '  "invoice_number": null,',
                '  "invoice_number_confidence": 0.0,',
                '  "invoice_number_evidence": []',
                "}}",
            ]
        )
    else:
        parts.extend(
            [
                "Return ONLY a valid JSON object whose keys are exactly the field names above plus each field's *_confidence key.",
                "Confidence keys use the pattern <field_name>_confidence with values from 0.0 to 1.0.",
                "Example:",
                "{{",
                '  "vendor_name": "Acme GmbH",',
                '  "vendor_name_confidence": 0.95,',
                '  "invoice_number": null,',
                '  "invoice_number_confidence": 0.0',
                "}}",
            ]
        )
    parts.extend(["", "Do not include any text outside the JSON object."])
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
    "- Date fields need a complete date (day, month and year). If the page shows only a day, or only a month and year, use null.",
    "- Total amounts: take the grand total of the document (TOTAL GENERAL, Total TTC, Net à payer, Montant total, "
    "or the amount in words after 'Arrêté la présente facture à la somme de'), never a single line total.",
    "- Funding or project stamps (a box naming the project, 'Financé par', 'Pourcentage d'imputation', a project code) "
    "are approval marks, not document content: never use their text as a description, purpose, item, vendor or name.",
    "- payment_method: use the payment evidence shown (a cheque, a transfer order or slip, a receipt stating cash). "
    "A 'Payé par' line on an internal request names the payer, not how the payment was made; use it only when no such evidence is on the page.",
)

# Notes added to an extraction call according to the page's inventory document_role.
_PAGE_ROLE_NOTES: dict[str, str] = {
    "funds_request": (
        "This page is an internal funds or payment request issued by the paying organisation itself, not the "
        "supplier's invoice. The organisation in its letterhead is the payer (the client), never the vendor. "
        "'Bénéficiaire' is the party being paid (the supplier or the person receiving the money). People next to "
        "'Autorisé par', 'Payé par', 'Demandeur', 'Visa' or 'Approuvé par' are the payer's own staff: never use them "
        "as the vendor or as the employee or volunteer being paid. The form's own N° and date are the request's "
        "number and date."
    ),
    "internal_other": (
        "This page is an internal form of the paying organisation (requisition, goods received note, timesheet, "
        "memo), not the supplier's invoice. Its letterhead organisation is the payer, never the vendor; its own "
        "numbers and dates are internal references, not an invoice number or invoice date. Staff names on it "
        "(requester, logistics, approvers) are not the vendor."
    ),
    "supplier_other": (
        "This page is a supplier document that is not the final invoice (pro forma, quote or delivery note): its "
        "number and date are not the invoice's, and a pro forma total may differ from the amount invoiced."
    ),
    "payment_proof": (
        "This page is proof of payment (cheque, transfer, bank statement). Use it for how the payment was made and "
        "the amount paid; the issuing bank is not the vendor."
    ),
}


def page_role_note(document_role: str) -> str:
    """Extraction hint for a page's inventory document_role; empty for supplier invoices and unknown roles."""
    return _PAGE_ROLE_NOTES.get((document_role or "").strip().lower(), "")


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


def build_compliance_visual_prompt(
    evidence_lines_block: str,
    rule_lines: str,
    ocr_block: str = "",
    cite_lines: bool = False,
) -> str:
    """
    evidence_lines_block: joined lines describing each image index and page_num.
    rule_lines: newline-separated rule descriptions from config.
    ocr_block: OCR lines of the pages sent, each prefixed with a [p<page>_L<n>] ID.
    cite_lines: ask for evidence_line_ids / evidence_kind per rule.
    """
    base = _compliance_visual_prompt_base(evidence_lines_block, rule_lines)
    if not cite_lines:
        return base
    return (
        base
        + f"""

OCR transcript of the pages above (may contain OCR errors; each line starts with its ID):
{ocr_block}

Also add to every rule object:
  "evidence_line_ids": IDs of the OCR lines that show what you based the verdict on, e.g. ["p2_L39", "p2_L40"]
     (the stamp text, the date, the signature label next to the signature). [] if none apply.
  "evidence_kind": one of
     "text"   — the evidence is printed or stamped text in the cited lines,
     "anchor" — the evidence is not text (signature, seal graphic); cite the nearest label or stamp text lines,
     "absent" — you looked and the required item is not in the document (cite nothing),
     "none"   — you could not judge.
Only cite IDs listed in the OCR transcript. Never invent IDs."""
    )


def _compliance_visual_prompt_base(evidence_lines_block: str, rule_lines: str) -> str:
    return f"""You are a document compliance inspector. You receive one or more page images in order.

Evidence (image order → document page):
{evidence_lines_block}

For each requirement below, decide pass or fail using evidence across all images. Reference page_num=N when you cite where something appears.
"passes": true means the requirement is satisfied; false means it is not. Write the observation first, then set
"passes" so it agrees with it: an observation saying the requirement is met must have "passes": true.
Judge only what is visible. Do not assume or infer that a required document or mark exists.

Requirements:
{rule_lines}

Respond with ONLY valid JSON — one top-level key per rule_id, no markdown fences. Shape per rule:
{{
  "RULE_ID": {{
    "observation": "one or two short sentences: what you saw, where (page_num), and why pass/fail",
    "passes": true,
    "confidence": 0.0-1.0,
    "field_updates": {{}}
  }}
}}

Optional "field_updates": only when this rule directly supports a value — a flat map of extraction field names (same names as the invoice schema, e.g. employee_name, payment_method) to short strings. Use {{}} or omit if nothing to add. Never invent values."""


# --- Page inventory (per page, category + description) ---


_INVENTORY_CATEGORIES = (
    "   INVOICE_HEADER   — vendor/client block, invoice number, dates, references, letterhead\n"
    "   LINE_ITEMS       — tables of services/products, quantities, unit prices, mileage lines\n"
    "   TOTALS           — subtotals, taxes, grand total, bank/IBAN, payment summary\n"
    "   SIGNATURE_STAMP  — signatures, stamps, seals, approvals, discharge blocks\n"
    "   SUPPORTING_DOC   — receipts, quotes, contracts, tickets, boarding passes, photos, timesheets\n"
    "   COVER_PAGE       — title page, transmittal, cover letter, project summary without invoice body\n"
    "   BLANK            — empty or nearly empty\n"
)

# Who issued the page. Expense bundles mix the paying organisation's own forms with the
# supplier's documents; extraction ranks pages by this role (see agent.document_role_priority).
_INVENTORY_DOCUMENT_ROLES = (
    "   funds_request    — internal funds or payment request issued by the paying organisation itself, on its\n"
    "                      own letterhead (\"Demande de fonds\", \"Demande de paiement\", \"Solicitud de fondos\",\n"
    "                      payment voucher, \"Bon de caisse\", \"Ordre de paiement\"); usually has \"Bénéficiaire\",\n"
    "                      \"Autorisé par\", \"Payé par\" lines\n"
    "   internal_other   — any other form issued by the paying organisation: requisition (\"Fiche de réquisition\"),\n"
    "                      goods received note (\"Bon de réception\"), timesheet (\"Fiche de pointage\"), mission order,\n"
    "                      internal memo, letter or list\n"
    "   supplier_invoice — the final invoice, receipt or ticket issued by the supplier or service provider\n"
    "                      (\"Facture\", \"Facture définitive\", \"Reçu\", airline or hotel invoice), or a payment\n"
    "                      acknowledgement signed by the person being paid (\"Décharge\")\n"
    "   supplier_other   — other supplier documents that are not the final invoice: pro forma or quote\n"
    "                      (\"Facture proforma\", \"Devis\"), delivery note (\"Bon/Bordereau de livraison\")\n"
    "   payment_proof    — proof the payment was made: cheque copy, bank transfer order or advice, bank statement\n"
    "   other            — anything else: photos, identity documents, a page with only stamps, cover or blank page\n"
)


def page_inventory_prompt() -> str:
    return (
        "Examine this single page image. Do three things:\n\n"
        "1. Choose EXACTLY one category from the fixed list:\n"
        + _INVENTORY_CATEGORIES
        + "\n2. Choose EXACTLY one document_role (who issued this page and what it is):\n"
        + _INVENTORY_DOCUMENT_ROLES
        + "\n3. Write a specific description (max 15 words) of what is literally visible on THIS page.\n"
        "   Name the document title, the issuing organisation, amounts, languages, or notable stamps — avoid generic filler.\n\n"
        'Respond with ONLY valid JSON: {"category": "<one of the categories>", '
        '"document_role": "<one of the roles>", "description": "..."}'
    )


def page_inventory_batch_prompt(page_count: int) -> str:
    return (
        f"You are given {page_count} page images in order (page 1 first). "
        "For EACH page choose a category and a document_role, and write a short description.\n\n"
        "Categories (pick exactly one per page):\n"
        + _INVENTORY_CATEGORIES
        + "\nDocument roles (pick exactly one per page: who issued it and what it is):\n"
        + _INVENTORY_DOCUMENT_ROLES
        + "\nDescription: max 15 words, name the document title, issuing organisation, amounts and stamps "
        "visible on that specific page.\n\n"
        'Respond with ONLY valid JSON: {"pages": [{"category": "...", "document_role": "...", "description": "..."}, ...]}'
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
