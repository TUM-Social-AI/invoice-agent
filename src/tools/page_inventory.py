"""Moved implementations for page_inventory.py."""

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
from src.models.tool_io_models import InventoryItemModel
from src.prompts.llm_prompts import page_inventory_prompt, page_inventory_batch_prompt

_CATEGORY_ENUM = [
    "INVOICE_HEADER", "LINE_ITEMS", "TOTALS", "SIGNATURE_STAMP",
    "SUPPORTING_DOC", "COVER_PAGE", "BLANK", "UNKNOWN",
]

_INVENTORY_ITEM_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": _CATEGORY_ENUM},
        "description": {"type": "string"},
    },
    "required": ["category", "description"],
    "additionalProperties": False,
}

# OpenAI structured output requires the top-level to be an object, not an array.
_INVENTORY_BATCH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "pages": {
            "type": "array",
            "items": _INVENTORY_ITEM_SCHEMA,
        }
    },
    "required": ["pages"],
    "additionalProperties": False,
}
from src.tools.pdf_pages import _image_to_base64
from src.tools.compliance_eval import _normalize_numeric

logger = logging.getLogger(__name__)


def _extract_entities_from_text(text: str) -> dict:
    """Lightweight entity extraction for evidence grounding."""
    t = text or ""
    amounts = []
    dates = []
    references = []
    payment_markers = []

    for m in re.findall(r"\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{2})\b|\b\d+[.,]\d{2}\b", t):
        n = _normalize_numeric(m)
        if n is not None:
            amounts.append(round(float(n), 2))

    for m in re.findall(r"\b\d{1,2}[\/\.\-]\d{1,2}[\/\.\-]\d{2,4}\b|\b\d{4}[\/\.\-]\d{2}[\/\.\-]\d{2}\b", t):
        dates.append(m)

    for m in re.findall(r"\b[A-Z]{1,5}[-_/]?\d{2,}\b", t):
        references.append(m)

    payment_terms = ("transfer", "bank", "iban", "swift", "paid", "payment", "tarjeta", "card", "receipt", "justificante")
    low = t.lower()
    for term in payment_terms:
        if term in low:
            payment_markers.append(term)

    return {
        "amounts": sorted(set(amounts)),
        "dates": sorted(set(dates)),
        "references": sorted(set(references)),
        "payment_markers": sorted(set(payment_markers)),
    }

def _batch_inventory_with_provider(
    inventory_paths: list,
    model: str,
    provider: "LLMProvider",
    timeout_s: int,
    batch_size: int,
) -> list[dict]:
    """
    Call provider.generate_json once per batch of pages, returning a flat list of
    {category, description} dicts in page order.  batch_size=0 means all pages in
    one call; batch_size=N means at most N pages per call.
    """
    results: list[dict] = []
    effective_batch = len(inventory_paths) if batch_size <= 0 else batch_size

    for chunk_start in range(0, len(inventory_paths), effective_batch):
        chunk_paths = inventory_paths[chunk_start:chunk_start + effective_batch]
        images_b64 = [_image_to_base64(p) for p in chunk_paths]
        prompt = page_inventory_batch_prompt(len(chunk_paths))
        try:
            llm_result = provider.generate_json(
                model=model,
                prompt=prompt,
                images_b64=images_b64,
                temperature=0.1,
                timeout_s=timeout_s,
                response_format=_INVENTORY_BATCH_SCHEMA,
            )
            parsed = llm_result.content_json
            if parsed is None:
                parsed = json.loads(llm_result.content_text.strip())
            # Schema enforces {"pages": [...]}, but unwrap defensively
            if isinstance(parsed, dict) and isinstance(parsed.get("pages"), list):
                parsed = parsed["pages"]
            if not isinstance(parsed, list):
                raise ValueError(f"Expected array under 'pages', got: {type(parsed).__name__}")
            # Pad or trim to match chunk length
            while len(parsed) < len(chunk_paths):
                parsed.append({"category": "UNKNOWN", "description": "(no response)"})
            results.extend(parsed[:len(chunk_paths)])
        except Exception as e:
            raw_preview = ""
            try:
                raw_preview = f" | response: {llm_result.content_text[:200]!r}"
            except Exception:
                pass
            logger.warning("inventory_pages batch failed for pages %d-%d: %s%s", chunk_start + 1, chunk_start + len(chunk_paths), e, raw_preview)
            results.extend([{"category": "UNKNOWN", "description": f"(error: {e})"}] * len(chunk_paths))

    return results


def inventory_pages(
    state: AgentState,
    ollama_url: str,
    model: str,
    provider: "LLMProvider | None" = None,
    timeout_s: int = 180,
    batch_size: int = 1,
) -> dict:
    """
    batch_size controls how many page images are sent in a single vision call:
      1  (default) — one call per page (Ollama-safe, any model)
      0            — all pages in one call (best for remote APIs like OpenAI/Gemini)
      N            — at most N pages per call
    """
    """
    Quick first-pass scan of all rendered pages.
    Sends each page to the vision model with a cheap one-sentence prompt to
    describe its content. Stores the result in state.page_inventory so the
    agent can jump directly to the right pages during extraction.

    Call this after compress_pages (use dpi=48, quality=30 for maximum speed).
    A separate convert_pdf_to_images pass at normal quality is still needed
    for accurate field extraction.
    """
    if not state.page_image_paths and not state.compressed_page_paths:
        return {"success": False, "error": "No pages rendered yet. Call compress_pages or convert_pdf_to_images first."}

    # Idempotency guard: if inventory is already built for the same number of pages,
    # return the cached result immediately — no vision model calls needed.
    inventory_paths = state.compressed_page_paths or state.page_image_paths
    if state.page_inventory and len(state.page_inventory) == len(inventory_paths):
        return {
            "success": True,
            "page_count": len(state.page_inventory),
            "inventory": state.page_inventory,
            "note": (
                "Inventory already built and unchanged — returning cached result. "
                "Do NOT call inventory_pages again unless pages have changed."
            ),
        }

    # Prefer low-res compressed pages for inventory — they're faster to encode and
    # the vision model only needs to classify the page type, not read fine detail.
    # compressed_page_paths is set by compress_pages and survives a subsequent
    # convert_pdf_to_images call (which overwrites page_image_paths).
    inventory_paths = state.compressed_page_paths or state.page_image_paths
    if state.compressed_page_paths:
        logger.debug("inventory_pages: using compressed pages for faster classification")
    else:
        logger.info(
            "inventory_pages: no compressed pages found — using full-res pages "
            "(consider calling compress_pages before inventory_pages for speed)"
        )

    inventory = []

    use_batch = provider is not None and batch_size != 1
    if use_batch:
        logger.debug("inventory_pages: batch mode (batch_size=%d, %d pages)", batch_size, len(inventory_paths))
        raw_items = _batch_inventory_with_provider(inventory_paths, model, provider, timeout_s, batch_size)
        for i, (path, item) in enumerate(zip(inventory_paths, raw_items)):
            page_num = i + 1
            try:
                inv_item = InventoryItemModel.model_validate(item)
                category = inv_item.category
                description = inv_item.description.strip()
            except Exception:
                category = str(item.get("category", "UNKNOWN"))
                description = str(item.get("description", ""))
            inventory.append({"page": page_num, "path": path, "category": category, "description": description})
            logger.debug("  inventory p%d [%s]: %s", page_num, category, description)
    else:
        # Per-page mode: one vision call per page (safe for Ollama and small models)
        prompt = page_inventory_prompt()
        for i, path in enumerate(inventory_paths):
            page_num = i + 1
            category = "UNKNOWN"
            description = ""
            try:
                img_b64 = _image_to_base64(path)
                if provider is not None:
                    llm_result = provider.generate_json(
                        model=model,
                        prompt=prompt,
                        images_b64=[img_b64],
                        temperature=0.1,
                        timeout_s=timeout_s,
                        response_format=_INVENTORY_ITEM_SCHEMA,
                    )
                    raw = llm_result.content_text.strip()
                    parsed = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
                else:
                    payload = {
                        "model": model,
                        "prompt": prompt,
                        "images": [img_b64],
                        "stream": False,
                        "options": {"temperature": 0.1},
                        "format": "json",
                    }
                    resp = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout_s)
                    resp.raise_for_status()
                    raw = resp.json().get("response", "").strip()
                    raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
                    parsed = json.loads(raw)
                inv_item = InventoryItemModel.model_validate(parsed)
                category = inv_item.category
                description = inv_item.description.strip()
            except json.JSONDecodeError:
                description = "(json parse error)"
            except Exception as e:
                description = f"(error: {e})"
            inventory.append({"page": page_num, "path": path, "category": category, "description": description})
            logger.debug("  inventory p%d [%s]: %s", page_num, category, description)

    state.page_inventory = inventory
    # Build normalized page facts for evidence-grounded rule evaluation.
    page_facts = {}
    for entry in inventory:
        page_num = int(entry.get("page", 0))
        description = entry.get("description", "")
        category = entry.get("category", "UNKNOWN")
        doc_subtype = "unknown"
        desc_low = description.lower()
        if "payroll" in desc_low or "nomina" in desc_low or "nómina" in desc_low:
            doc_subtype = "payroll"
        elif "invoice" in desc_low or "factura" in desc_low:
            doc_subtype = "invoice"
        elif "receipt" in desc_low or "justificante" in desc_low:
            doc_subtype = "receipt"
        elif "stamp" in desc_low or "seal" in desc_low:
            doc_subtype = "stamp_page"
        page_facts[page_num] = {
            "category": category,
            "doc_subtype": doc_subtype,
            "entities": _extract_entities_from_text(description),
            "confidence": 0.6,
        }
    state.page_facts = page_facts
    return {
        "success": True,
        "page_count": len(inventory),
        "inventory": inventory,
        "page_facts": page_facts,
    }
