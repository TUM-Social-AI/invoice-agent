"""Moved implementations for vision_llm.py."""

import base64
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
from src.models.tool_io_models import ClassificationResultModel, ExtractionPayloadModel
from src.prompts.llm_prompts import (
    build_extract_fields_vision_prompt,
    classify_document_type_prompt,
    format_extraction_accuracy_block,
    ocr_transcript_section,
)
from src.tools.pdf_pages import _image_to_base64

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


def classify_document_type(
    state: AgentState,
    store: "ConfigStore",
    ollama_url: str,
    vision_model: str,
    provider: "LLMProvider | None" = None,
    timeout_s: int = 240,
) -> dict:
    """
    Look at the first rendered page and determine which invoice type this document is.
    Sets state.invoice_type_id. Must be called after convert_pdf_to_images or compress_pages.
    """
    if not state.page_image_paths:
        return {"success": False, "error": "No pages rendered yet. Call convert_pdf_to_images or compress_pages first."}

    first_page = state.page_image_paths[0]

    type_descriptions = "\n".join(
        f'- "{t.invoice_type_id}": {t.display_name} — {t.description}'
        for t in store.invoice_types.values()
    )

    prompt = classify_document_type_prompt(type_descriptions)

    img_b64 = _image_to_base64(first_page)
    payload = {
        "model": vision_model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {"temperature": 0.1},
    }

    try:
        if provider is not None:
            pname = getattr(provider, "provider_name", "")
            llm_result = provider.generate_json(
                model=vision_model,
                prompt=prompt,
                images_b64=[img_b64],
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
        logger.info(f"Document classified as: {detected} (confidence={confidence}) — {reasoning}")
        return {
            "success": True,
            "invoice_type_id": detected,
            "confidence": confidence,
            "reasoning": reasoning,
        }

    except json.JSONDecodeError as e:
        return {"success": False, "error": f"JSON parse error: {e}", "raw_response": raw}
    except requests.RequestException as e:
        return {"success": False, "error": f"Ollama request failed: {e}"}

def _build_extraction_response_schema(schema: dict, provider_name: str) -> dict | None:
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
                prop: dict = {"anyOf": [{"type": "number"}, {"type": "null"}]}
            elif ftype == "boolean":
                prop = {"anyOf": [{"type": "boolean"}, {"type": "null"}]}
            elif allowed:
                prop = {"anyOf": [{"type": "string", "enum": allowed}, {"type": "null"}]}
            else:
                prop = {"anyOf": [{"type": "string"}, {"type": "null"}]}
            props[field_name] = prop
            props[f"{field_name}_confidence"] = {"type": "number", "minimum": 0.0, "maximum": 1.0}
        return {"type": "object", "properties": props}

    if provider_name == "gemini":
        props = {}
        for field_name, meta in schema.items():
            ftype = (meta.get("type") or "string").strip().lower()
            allowed = meta.get("enum")
            if ftype == "decimal":
                prop = {"type": "NUMBER", "nullable": True}
            elif ftype == "boolean":
                prop = {"type": "BOOLEAN", "nullable": True}
            elif allowed:
                prop = {"type": "STRING", "enum": allowed, "nullable": True}
            else:
                prop = {"type": "STRING", "nullable": True}
            props[field_name] = prop
            props[f"{field_name}_confidence"] = {"type": "NUMBER", "nullable": False}
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
                _build_extraction_response_schema(schema, pname)
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

def merge_extracted_fields(
    state: AgentState,
    new_extraction: dict,
    schema: dict,
    source_page: int,
    source_region: str,
) -> dict:
    """
    Merge new extraction results into state.extracted_fields.
    Only updates a field if the new confidence is higher than existing.
    """
    from src.tools.compliance_eval import _normalize_numeric as _coerce_numeric

    updated = []
    skipped = []
    null_fields = []

    for field_name, meta in schema.items():
        value = new_extraction.get(field_name)
        confidence = float(new_extraction.get(f"{field_name}_confidence", 0.5))

        if value is not None:
            ftype = (meta.get("type") or meta.get("data_type") or "").strip().lower()
            if ftype == "decimal" and isinstance(value, str):
                n = _coerce_numeric(value)
                if n is not None:
                    value = n
            else:
                value = _sanitize_extracted_string_value(value, ftype)

            # Enforce enum constraint: snap to canonical allowed value or reject.
            allowed_enum = meta.get("enum")
            if allowed_enum and isinstance(value, str):
                _norm = lambda s: re.sub(r"[\s\-]+", "_", s.strip().lower())
                value = next((v for v in allowed_enum if _norm(v) == _norm(value)), None)

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
                    review_reason=None,
                )
            continue

        existing = state.extracted_fields.get(field_name)
        if existing and existing.confidence >= confidence:
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
        state.extracted_fields[field_name] = FieldResult(
            field_id=field_id,
            field_name=field_name,
            extracted_value=value,
            confidence=confidence,
            source_page=source_page,
            source_region=source_region,
            extraction_attempts=state.get_field_retry_count(field_name),
            batch_review=is_batch_review,
        )
        updated.append(field_name)

    # "already_have_better" = fields where we already had a higher-confidence value stored;
    # the new extraction was NOT an improvement — existing values are fine, do NOT retry.
    # "null_fields" = fields the model returned null for (could not extract from this image).
    return {"updated": updated, "already_have_better": skipped, "null_fields": null_fields}
