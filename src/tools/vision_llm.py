"""Moved implementations for vision_llm.py."""

import base64
import io
import ast
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from src.agent.state import AgentState, FieldResult, RuleResult
from src.compliance.evidence import required_slots_for_rule, link_pages
from src.config.loader import ConfigStore, ComplianceRule
from src.llm.base import LLMProvider
from src.llm.response_format import provider_json_mode
from src.models.tool_io_models import ClassificationResultModel, ExtractionPayloadModel, inventory_label
from src.trace.evidence import Evidence, coerce_line_ids
from src.prompts.llm_prompts import (
    build_extract_fields_vision_prompt,
    classify_document_type_prompt,
    classify_from_inventory_prompt,
    format_extraction_accuracy_block,
    ocr_transcript_section,
)
from src.tools.pdf_pages import _image_to_base64
from src.tools.value_parsing import parse_amount, parse_full_date

logger = logging.getLogger(__name__)

# Lower than chat; reduces invented tokens on dense forms.
EXTRACTION_TEMPERATURE = 0.05


def _sanitize_extracted_string_value(value: Any, ftype: str) -> Any:
    """Strip HTML/XML noise from string/date fields before merge (OCR/layout sometimes embeds tags)."""
    if value is None:
        return None
    ftl = (ftype or "").strip().lower()
    if ftl not in ("string", "date", ""):
        return value
    if not isinstance(value, str):
        return value
    s = re.sub(r"<[^>]+>", "", value)
    s = re.sub(r"&#\d+;", " ", s)
    s = s.replace("&nbsp;", " ").replace("\xa0", " ").strip()
    if not s or s.lower() in ("null", "none"):
        return None
    return s


# Page 1 resolution for image-based classification (fallback when there is no page inventory).
CLASSIFY_PAGE_DPI = 150


def _inventory_block(state: AgentState) -> str:
    """One line per inventoried page; empty when there is no usable inventory."""
    lines = []
    for e in sorted(state.page_inventory or [], key=lambda x: int(x.get("page", 0) or 0)):
        desc = str(e.get("description", "") or "").strip()
        if not desc or desc.startswith("(no response") or desc.startswith("(error"):
            continue
        lines.append(f"- page {e.get('page')}: {inventory_label(e)} — {desc}")
    return "\n".join(lines)


def _name_slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def budget_line_hint(state: AgentState, store: "ConfigStore") -> str:
    """
    Classification prior from the file name: filers often prefix the file with its budget line
    ("A.7. Serv técn y prof-U0256-25.pdf"), matched against invoice_types.budget_line.
    Names are compared as slugs so "a-7-serv-t-cn-..." (a Drive download) matches too, and the
    longest configured code wins ("A.5.d" over "A.5"). Empty when nothing matches.
    """
    name = ""
    if state.source_provenance is not None:
        name = state.source_provenance.display_name or ""
    name = name or Path(state.pdf_path).name
    stem = _name_slug(Path(name).stem)
    best = None
    best_len = 0
    for t in store.invoice_types.values():
        code = _name_slug(t.budget_line)
        if code and (stem == code or stem.startswith(code + "-")) and len(code) > best_len:
            best, best_len = t, len(code)
    if best is None:
        return ""
    return (
        f'The file name "{name}" starts with budget line {best.budget_line}, '
        f'which maps to "{best.invoice_type_id}". Use it as a prior, not a rule: '
        "files are sometimes filed under the wrong budget line, so choose another type "
        "when the pages clearly show one."
    )


def _first_page_b64(state: AgentState) -> str:
    """Page 1 rendered at CLASSIFY_PAGE_DPI from the PDF; the first page image when rendering fails."""
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(state.pdf_path)
        try:
            img = pdf[0].render(scale=CLASSIFY_PAGE_DPI / 72.0).to_pil().convert("RGB")
        finally:
            pdf.close()
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception as e:
        logger.warning("classify_document_type: could not render page 1 (%s); using %s", e, state.page_image_paths[0])
        return _image_to_base64(state.page_image_paths[0])


def classify_document_type(
    state: AgentState,
    store: "ConfigStore",
    ollama_url: str,
    vision_model: str,
    provider: "LLMProvider | None" = None,
    timeout_s: int = 240,
) -> dict:
    """
    Determine which invoice type this document is. Sets state.invoice_type_id.

    With a page inventory (inventory_pages ran), classify from the descriptions of all
    pages, text only: page 1 is often a generic fund-request form that looks the same for
    every expense type, and what is being paid for shows on the later pages. Without an
    inventory, classify from page 1 rendered at CLASSIFY_PAGE_DPI; the 48 DPI thumbnails
    from compress_pages are too small to read and led to guessed types.
    Must be called after convert_pdf_to_images or compress_pages.
    """
    if not state.page_image_paths:
        return {"success": False, "error": "No pages rendered yet. Call convert_pdf_to_images or compress_pages first."}

    type_descriptions = "\n".join(
        f'- "{t.invoice_type_id}": {t.display_name} — {t.description}'
        for t in store.invoice_types.values()
    )

    filename_hint = budget_line_hint(state, store)
    inventory_block = _inventory_block(state)
    if inventory_block:
        basis = "page_inventory"
        prompt = classify_from_inventory_prompt(type_descriptions, inventory_block, filename_hint)
        images: list[str] = []
    else:
        basis = "first_page_image"
        prompt = classify_document_type_prompt(type_descriptions, filename_hint)
        images = [_first_page_b64(state)]

    payload = {
        "model": vision_model,
        "prompt": prompt,
        "images": images,
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        if provider is not None:
            pname = getattr(provider, "provider_name", "")
            llm_result = provider.generate_json(
                model=vision_model,
                prompt=prompt,
                images_b64=images or None,
                temperature=0.1,
                timeout_s=timeout_s,
                response_format=provider_json_mode(pname),
            )
            raw = llm_result.content_text
            parsed = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
        else:
            resp = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout_s)
            resp.raise_for_status()
            raw = resp.json().get("response", "")
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            parsed = json.loads(raw)
        parsed_model = ClassificationResultModel.model_validate(parsed)
        detected = parsed_model.invoice_type_id.strip()
        confidence = parsed_model.confidence
        reasoning = parsed_model.reasoning

        if detected not in store.invoice_types:
            return {
                "success": False,
                "error": f"Model returned unknown type '{detected}'",
                "available_types": list(store.invoice_types.keys()),
            }

        state.invoice_type_id = detected
        logger.info(f"Document classified as: {detected} (confidence={confidence}, from {basis}) — {reasoning}")
        return {
            "success": True,
            "invoice_type_id": detected,
            "confidence": confidence,
            "reasoning": reasoning,
            "basis": basis,
        }

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {e}", "raw_response": raw}
    except requests.RequestException as e:
        return {"success": False, "error": f"Ollama request failed: {e}"}

def _build_extraction_response_schema(
    schema: dict, provider_name: str, cite_lines: bool = False
) -> dict | None:
    """Build a provider-specific JSON Schema to constrain the vision model's output.

    Reads ``enum`` from each field's schema meta (populated from allowed_values.csv)
    so enum constraints flow from config into the LLM's structured output without
    any hardcoding here.  Returns None for unknown providers (falls back to json mode).
    """
    if provider_name in ("ollama", "openai"):
        props: dict = {}
        for field_name, meta in schema.items():
            ftype = (meta.get("type") or "string").strip().lower()
            allowed = meta.get("enum")
            if ftype == "decimal":
                # Amounts come back as the printed text ("425 000", "50.000 R") and are parsed
                # by parse_amount in merge_extracted_fields: a JSON number makes the model
                # resolve thousands separators itself, and it reads "50.000" as 50.
                prop: dict = {"anyOf": [{"type": "string"}, {"type": "null"}]}
            elif ftype == "boolean":
                prop = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
            elif allowed:
                prop = {"anyOf": [{"type": "string", "enum": allowed}, {"type": "null"}]}
            else:
                prop = {"anyOf": [{"type": "string"}, {"type": "null"}]}
            props[field_name] = prop
            props[f"{field_name}_confidence"] = {"type": "number", "minimum": 0.0, "maximum": 1.0}
            if cite_lines:
                props[f"{field_name}_evidence"] = {"type": "array", "items": {"type": "string"}}
        return {"type": "object", "properties": props}

    if provider_name == "gemini":
        props = {}
        for field_name, meta in schema.items():
            ftype = (meta.get("type") or "string").strip().lower()
            allowed = meta.get("enum")
            if ftype == "decimal":
                prop = {"type": "STRING", "nullable": True}
            elif ftype == "boolean":
                prop = {"type": "BOOLEAN", "nullable": True}
            elif allowed:
                prop = {"type": "STRING", "enum": allowed, "nullable": True}
            else:
                prop = {"type": "STRING", "nullable": True}
            props[field_name] = prop
            props[f"{field_name}_confidence"] = {"type": "NUMBER", "nullable": False}
            if cite_lines:
                props[f"{field_name}_evidence"] = {"type": "ARRAY", "items": {"type": "STRING"}}
        return {"type": "OBJECT", "properties": props}

    return None


def extract_fields_vision(
    state: AgentState,
    image_path: str,
    schema: dict,
    hints: str,
    ollama_url: str,
    model: str,
    text_context: str = "",
    provider: "LLMProvider | None" = None,
    timeout_s: int = 240,
    cite_lines: bool = False,
) -> dict:
    """
    Send image + schema to Qwen2-VL via Ollama.
    Returns structured JSON matching the schema fields.
    If text_context is provided (e.g. OCR text), it is injected so the vision model can
    cross-check pixel reading against the transcript.
    """
    field_descriptions = []
    for field_name, meta in schema.items():
        req = "REQUIRED" if meta.get("required") else "optional"
        aliases = ", ".join(meta.get("aliases", []))
        allowed = meta.get("enum")
        field_descriptions.append(
            f'- "{field_name}" ({meta["label"]}, {req}, type={meta["type"]}): '
            f'{meta["hint"]}'
            + (f" | Also look for: {aliases}" if aliases else "")
            + (f" | Must be one of: {', '.join(allowed)}" if allowed else "")
        )

    fields_text = "\n".join(field_descriptions)

    text_section = ocr_transcript_section(text_context)
    acc = format_extraction_accuracy_block(state)
    prompt = build_extract_fields_vision_prompt(
        text_section=text_section,
        hints=hints,
        accuracy_block=acc,
        fields_text=fields_text,
        cite_lines=cite_lines,
    )

    img_b64 = _image_to_base64(image_path)

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {"temperature": EXTRACTION_TEMPERATURE},
        "format": "json",  # Ollama /api/generate: constrain output to JSON when using raw HTTP path
    }

    try:
        if provider is not None:
            pname = getattr(provider, "provider_name", "")
            # Use a full JSON Schema (with enum constraints) when the provider supports it;
            # fall back to plain JSON mode so _build_extraction_response_schema returning
            # None still produces valid output.
            extraction_response_format: Any = (
                _build_extraction_response_schema(schema, pname, cite_lines=cite_lines)
                or (
                    "json"
                    if pname == "ollama"
                    else "application/json"
                    if pname == "gemini"
                    else {"type": "json_object"}
                    if pname == "openai"
                    else None
                )
            )
            llm_result = provider.generate_json(
                model=model,
                prompt=prompt,
                images_b64=[img_b64],
                temperature=EXTRACTION_TEMPERATURE,
                timeout_s=timeout_s,
                response_format=extraction_response_format,
            )
            raw = llm_result.content_text
            parsed = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
        else:
            resp = requests.post(
                f"{ollama_url}/api/generate",
                json=payload,
                timeout=timeout_s,
            )
            resp.raise_for_status()
            raw = resp.json().get("response", "")

            # Strip markdown fences if present
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("```").strip()

            parsed = json.loads(raw)
        payload_model = ExtractionPayloadModel.model_validate({"payload": parsed})
        return {"success": True, "extracted": payload_model.payload, "raw_response": raw}

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {e}", "raw_response": raw}
    except requests.Timeout:
        return {
            "success": False,
            "error": (
                f"Vision model timed out (>{timeout_s}s). "
                "This is a MODEL SPEED issue — the image is fine, re-rendering will NOT help. "
                "Do NOT call convert_pdf_to_images again. "
                "Instead: try a smaller field_subset (≤5 fields), "
                "use crop_region to send a smaller image region, "
                "or flag missing fields for human review and call check_compliance."
            ),
        }
    except requests.RequestException as e:
        return {"success": False, "error": f"Ollama request failed: {e}"}

def normalize_extracted_value(value: Any, meta: dict, date_order: str = "DMY") -> tuple[Any, str | None]:
    """
    Clean one extracted value for its field type. Returns (value, reject_reason):
    amounts are parsed from the printed text, dates must be complete and come back as
    YYYY-MM-DD, enum fields snap to an allowed value. A rejected value is (None, reason).
    """
    if value is None:
        return None, None
    ftype = (meta.get("type") or meta.get("data_type") or "").strip().lower()
    if ftype == "decimal":
        n = parse_amount(value)
        if n is None:
            return None, f"not a readable amount: {str(value)[:40]!r}"
        return n, None
    value = _sanitize_extracted_string_value(value, ftype)
    if value is None:
        return None, None
    if ftype == "date":
        iso = parse_full_date(value, order=date_order)
        if iso is None:
            return None, f"not a complete date (day, month and year): {str(value)[:40]!r}"
        return iso, None
    allowed_enum = meta.get("enum")
    if allowed_enum and isinstance(value, str):
        _norm = lambda s: re.sub(r"[\s\-]+", "_", s.strip().lower())
        snapped = next((v for v in allowed_enum if _norm(v) == _norm(value)), None)
        if snapped is None:
            return None, f"not an allowed value: {value[:40]!r}"
        return snapped, None
    return value, None


def _role_rank(role_priority: dict | None, field_name: str, role: str) -> int | None:
    """Position of a page's document role in the field's priority list (0 = best); None when the
    field has no list. Roles missing from the list rank after every listed role."""
    order = (role_priority or {}).get(field_name)
    if not order:
        return None
    return order.index(role) if role in order else len(order)


def merge_extracted_fields(
    state: AgentState,
    new_extraction: dict,
    schema: dict,
    source_page: int,
    source_region: str,
    *,
    role_priority: dict | None = None,
    date_order: str = "DMY",
) -> dict:
    """
    Merge new extraction results into state.extracted_fields.

    A field is updated when the new confidence is higher than the stored one, except for fields
    listed in role_priority (field -> document roles, best first): there a value read on a page
    whose inventory document_role ranks higher replaces a lower-ranked one, and a lower-ranked one
    never replaces a higher-ranked one, as long as the better-ranked value clears
    state.confidence_threshold. Amounts are parsed from their printed text and partial dates
    are rejected (see normalize_extracted_value); rejections count as null attempts.
    """
    from src.tools.page_inventory import page_document_role

    updated = []
    skipped = []
    null_fields = []
    rejected: dict[str, str] = {}
    new_role = page_document_role(state, source_page)

    for field_name, meta in schema.items():
        raw_value = new_extraction.get(field_name)
        confidence = float(new_extraction.get(f"{field_name}_confidence", 0.5))
        value, reject_reason = normalize_extracted_value(raw_value, meta, date_order)
        if reject_reason:
            rejected[field_name] = reject_reason
            logger.info("  merge: rejected %s from page %s (%s)", field_name, source_page, reject_reason)

        if value is None:
            # The model explicitly returned null for this field.
            # Count it as an attempted extraction so we can bound retries and
            # eventually route the field to human review.
            state.increment_field_retry(field_name)
            null_fields.append(field_name)

            existing = state.extracted_fields.get(field_name)
            # Don't overwrite a previously extracted non-null value with a null attempt.
            if existing is None or existing.extracted_value is None:
                field_id = meta.get("field_id", field_name)
                state.extracted_fields[field_name] = FieldResult(
                    field_id=field_id,
                    field_name=field_name,
                    extracted_value=None,
                    confidence=confidence,
                    source_page=source_page,
                    source_region=source_region,
                    extraction_attempts=state.get_field_retry_count(field_name),
                    flagged_for_review=False,
                    review_reason=f"Rejected on page {source_page}: {reject_reason}" if reject_reason else None,
                )
            continue

        existing = state.extracted_fields.get(field_name)
        keep_existing = bool(existing and existing.confidence >= confidence)
        new_rank = _role_rank(role_priority, field_name, new_role)
        if existing is not None and existing.extracted_value is not None and new_rank is not None:
            old_rank = _role_rank(role_priority, field_name, page_document_role(state, existing.source_page))
            threshold = state.confidence_threshold
            if new_rank < old_rank and confidence >= threshold:
                keep_existing = False
            elif new_rank > old_rank and existing.confidence >= threshold:
                keep_existing = True
        if keep_existing:
            # Still count as an attempt even though confidence didn't improve,
            # so the agent knows when to stop retrying and flag for review
            state.increment_field_retry(field_name)
            skipped.append(field_name)
            continue

        # Find field_id from schema meta
        field_id = meta.get("field_id", field_name)

        # Increment retry count before writing so extraction_attempts reflects
        # the number of successful updates (i.e. times the value improved)
        state.increment_field_retry(field_name)

        is_batch_review = (
            state.confidence_threshold <= confidence < state.batch_review_threshold
        )
        cited = coerce_line_ids(new_extraction.get(f"{field_name}_evidence"))
        evidence = (
            [Evidence(
                page=source_page,
                line_ids=cited,
                source="ocr_direct" if source_region == "ocr_direct" else "model_cited",
            )]
            if cited
            else []
        )
        state.extracted_fields[field_name] = FieldResult(
            field_id=field_id,
            field_name=field_name,
            extracted_value=value,
            confidence=confidence,
            source_page=source_page,
            source_region=source_region,
            extraction_attempts=state.get_field_retry_count(field_name),
            batch_review=is_batch_review,
            evidence=evidence,
        )
        updated.append(field_name)

    # "already_have_better" = fields where we already had a higher-confidence value stored;
    # the new extraction was NOT an improvement — existing values are fine, do NOT retry.
    # "null_fields" = fields the model returned null for (could not extract from this image).
    # "rejected" = values dropped as unreadable amounts, partial dates or values outside the enum.
    return {
        "updated": updated,
        "already_have_better": skipped,
        "null_fields": null_fields,
        "rejected": rejected,
    }
