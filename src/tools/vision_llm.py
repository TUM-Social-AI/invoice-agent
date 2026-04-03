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
from typing import Any, Optional

import requests
from PIL import Image

from src.agent.state import AgentState, FieldResult, RuleResult
from src.compliance.evidence import required_slots_for_rule, link_pages
from src.config.loader import ConfigStore, ComplianceRule
from src.llm.base import LLMProvider
from src.tools.pdf_pages import _image_to_base64

logger = logging.getLogger(__name__)


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

    prompt = f"""You are an invoice classification expert. Look at this document and identify which type it is.

Available types:
{type_descriptions}

Respond with ONLY a valid JSON object:
{{"invoice_type_id": "EU_VAT", "confidence": 0.92, "reasoning": "one sentence"}}

Pick exactly one type_id from the list. Do not invent new types."""

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
            llm_result = provider.generate_json(
                model=vision_model,
                prompt=prompt,
                images_b64=[img_b64],
                temperature=0.1,
                timeout_s=timeout_s,
            )
            raw = llm_result.content_text
            parsed = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
        else:
            resp = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout_s)
            resp.raise_for_status()
            raw = resp.json().get("response", "")
            raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
            parsed = json.loads(raw)
        detected = parsed.get("invoice_type_id", "").strip()
        confidence = parsed.get("confidence", 0)
        reasoning = parsed.get("reasoning", "")

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
    If text_context is provided (native PDF text layer), it is injected before the field
    list so the vision model can cross-check pixel reading against the text layer.
    """
    field_descriptions = []
    for field_name, meta in schema.items():
        req = "REQUIRED" if meta.get("required") else "optional"
        aliases = ", ".join(meta.get("aliases", []))
        field_descriptions.append(
            f'- "{field_name}" ({meta["label"]}, {req}, type={meta["type"]}): '
            f'{meta["hint"]}'
            + (f" | Also look for: {aliases}" if aliases else "")
        )

    fields_text = "\n".join(field_descriptions)

    text_section = (
        f"[Native PDF text layer — use as primary reference for exact values]\n{text_context}\n\n"
        if text_context else ""
    )

    prompt = f"""You are an invoice data extraction specialist. Extract the following fields from this invoice image.

{text_section}{hints}

Extract these fields (return null if not found, not if uncertain):
{fields_text}

Return ONLY a valid JSON object with exactly these keys. For each field also include a "_confidence" key (0.0-1.0).
Example format:
{{
  "vendor_name": "Acme GmbH",
  "vendor_name_confidence": 0.95,
  "invoice_number": null,
  "invoice_number_confidence": 0.0
}}

Do not include any text outside the JSON object."""

    img_b64 = _image_to_base64(image_path)

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
        "options": {"temperature": 0.1},  # low temp for extraction tasks
    }

    try:
        if provider is not None:
            llm_result = provider.generate_json(
                model=model,
                prompt=prompt,
                images_b64=[img_b64],
                temperature=0.1,
                timeout_s=timeout_s,
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
        return {"success": True, "extracted": parsed, "raw_response": raw}

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
    updated = []
    skipped = []
    null_fields = []

    for field_name, meta in schema.items():
        value = new_extraction.get(field_name)
        confidence = float(new_extraction.get(f"{field_name}_confidence", 0.5))

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
