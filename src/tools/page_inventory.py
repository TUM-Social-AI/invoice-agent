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

def inventory_pages(
    state: AgentState,
    ollama_url: str,
    model: str,
    provider: "LLMProvider | None" = None,
    timeout_s: int = 180,
) -> dict:
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

    # Fixed taxonomy makes the inventory machine-readable and easier for the agent
    # to use when deciding which pages to target for extraction.
    prompt = (
        "Look carefully at this document page. Do two things:\n\n"
        "1. Choose EXACTLY one category:\n"
        "   INVOICE_HEADER   — vendor info, client info, invoice number, date, reference numbers\n"
        "   LINE_ITEMS       — table of services, products, quantities, unit prices\n"
        "   TOTALS           — subtotals, tax amounts, grand total, payment details, IBAN\n"
        "   SIGNATURE_STAMP  — signatures, stamps, seals, approval marks, authorisation\n"
        "   SUPPORTING_DOC   — attached receipt, quote, contract, ticket, boarding pass, photo\n"
        "   COVER_PAGE       — title page, cover letter, reference / transmittal letter\n"
        "   BLANK            — empty or near-empty page with no meaningful content\n\n"
        "2. Write a specific description (max 15 words) of what you actually see on THIS page.\n"
        "   Mention concrete details: organisation names, document titles, visible amounts,\n"
        "   languages, number of line items, type of stamp, etc.\n"
        "   Do NOT write a generic description — describe what is literally visible.\n\n"
        'Respond with ONLY valid JSON: {"category": "...", "description": "..."}'
    )

    inventory = []
    for i, path in enumerate(inventory_paths):
        page_num = i + 1
        category = "UNKNOWN"
        description = ""
        try:
            img_b64 = _image_to_base64(path)
            payload = {
                "model": model,
                "prompt": prompt,
                "images": [img_b64],
                "stream": False,
                "options": {"temperature": 0.1},
                "format": "json",
            }
            if provider is not None:
                llm_result = provider.generate_json(
                    model=model,
                    prompt=prompt,
                    images_b64=[img_b64],
                    temperature=0.1,
                    timeout_s=timeout_s,
                    response_format="json",
                )
                raw = llm_result.content_text.strip()
                parsed = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
            else:
                resp = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout_s)
                resp.raise_for_status()
                raw = resp.json().get("response", "").strip()
                raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
                parsed = json.loads(raw)
            category = parsed.get("category", "UNKNOWN").strip().upper()
            description = parsed.get("description", "").strip()
        except json.JSONDecodeError:
            # Model didn't return JSON — fall back to treating raw response as description
            description = resp.json().get("response", "").strip()[:120]
        except Exception as e:
            description = f"(error: {e})"

        inventory.append({
            "page": page_num,
            "path": path,
            "category": category,
            "description": description,
        })
        logger.debug(f"  inventory p{page_num} [{category}]: {description}")

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
