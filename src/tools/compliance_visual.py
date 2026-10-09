"""Moved implementations for compliance_visual.py."""

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
from typing import Any, Callable, Optional

import requests
from PIL import Image

from src.agent.state import AgentState, FieldResult, RuleResult
from src.compliance.evidence import required_slots_for_rule, link_pages
from src.config.loader import ConfigStore, ComplianceRule
from src.llm.base import LLMProvider
from src.llm.response_format import provider_json_mode
from src.models.tool_io_models import VisualVerdictModel
from src.trace.evidence import parse_line_id
from src.trace.ocr_cache import format_lines_with_ids, lines_with_ids
from src.prompts.llm_prompts import build_compliance_visual_prompt
from src.tools.vision_llm import _sanitize_extracted_string_value
from src.tools.pdf_pages import image_to_base64_scaled
from src.tools.compliance_eval import VISUAL_SKIP_KEY, _evaluate_rule, _policy_refs_for_rule

logger = logging.getLogger(__name__)

# When a denylist phrase is a substring of the candidate, reject only if the string is shorter than this.
_ROLE_SUBSTRING_MAX_LEN = 35


def _reject_employee_name_role_like(name: str, phrases: list[str]) -> bool:
    """True if the string should not be used as employee_name (role/title line, etc.)."""
    low = name.lower().strip()
    if len(name) < 3:
        return True
    for p in phrases:
        pl = p.strip().lower()
        if not pl:
            continue
        if low == pl:
            return True
        if pl in low and len(name) < _ROLE_SUBSTRING_MAX_LEN:
            return True
    return False


def _merge_visual_field_updates(
    state: AgentState,
    store: ConfigStore,
    rule_id: str,
    page_num: int,
    field_updates: dict[str, str],
) -> list[str]:
    """
    Apply optional structured field_updates from a visual verdict into state.extracted_fields.
    Only fills empty slots (or replaces payment_method when the current value is a date fragment).
    Keys must match extraction field names for the active invoice type.
    """
    if not field_updates:
        return []
    schema = store.build_extraction_schema(state.invoice_type_id)
    applied: list[str] = []
    for key, raw in field_updates.items():
        key = str(key).strip()
        if key not in schema:
            logger.debug("check_compliance_visual: skip field_updates[%s] (not in schema)", key)
            continue
        ftype = str(schema[key].get("type") or "string").strip().lower()
        val = _sanitize_extracted_string_value(raw, ftype if ftype in ("string", "date") else "string")
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        val = str(val).strip()
        if key == "employee_name" and _reject_employee_name_role_like(val, store.employee_name_role_denylist):
            continue
        if key == "payment_method" and _is_short_date_fragment(val):
            continue

        existing = state.extracted_fields.get(key)
        ev = existing.extracted_value if existing else None
        empty = existing is None or ev in (None, "", "null")
        pm_junk = key == "payment_method" and ev is not None and _is_short_date_fragment(str(ev))

        if key == "payment_method":
            if not empty and not pm_junk:
                continue
        elif not empty:
            continue

        fid = str(schema[key].get("field_id") or key)
        state.extracted_fields[key] = FieldResult(
            field_id=fid,
            field_name=key,
            extracted_value=val,
            confidence=0.86,
            source_page=page_num,
            source_region=f"visual_{rule_id}_field_updates",
            extraction_attempts=existing.extraction_attempts if existing else 0,
            flagged_for_review=True,
            review_reason=f"Suggested by visual compliance {rule_id} (field_updates) — verify",
        )
        applied.append(key)
        logger.info(
            "check_compliance_visual: merged field_updates[%s] from %s (%d chars)",
            key,
            rule_id,
            len(val),
        )
    return applied


def _is_short_date_fragment(s: str) -> bool:
    return bool(re.match(r"^\d{1,2}[/.\-]\d{1,2}([/.\-]\d{2,4})?$", str(s).strip()))


def _parse_employee_name_from_visual_observation(text: str, store: ConfigStore) -> str | None:
    """Extract a quoted employee name from visual-check observation prose."""
    if not text or not str(text).strip():
        return None
    patterns = (
        r"employee\s+name\s+'([^']+)'",
        r"name\s+'([^']+)'",
    )
    for pat in patterns:
        match = re.search(pat, str(text), flags=re.IGNORECASE)
        if not match:
            continue
        name = match.group(1).strip()
        if _reject_employee_name_role_like(name, store.employee_name_role_denylist):
            return None
        return name
    return None


def _parse_payment_phrase_from_visual_observation(text: str) -> str | None:
    """Extract a short payment-evidence phrase from visual-check observation prose."""
    if not text or not str(text).strip():
        return None
    patterns = (
        r"(Payé par[^,.;)]*)",
        r"(Paid by[^,.;)]*)",
        r"(pagado por[^,.;)]*)",
        r"(payment method[^,.;)]*)",
    )
    for pat in patterns:
        match = re.search(pat, str(text), flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _verdict_missing(raw_verdict: Any) -> bool:
    """True when the model gave no usable verdict (rule omitted, empty, or no "passes" key)."""
    return not isinstance(raw_verdict, dict) or "passes" not in raw_verdict


def _visual_trace_record(
    verdict: VisualVerdictModel,
    raw_verdict: Any,
    page_num: int,
    final_pages: list[int],
    ocr_by_page: dict[int, Any],
    max_chars_per_page: int = 0,
    pages_dropped: Optional[list[int]] = None,
    attempt: str = "",
    skip_reason: str = "",
) -> dict:
    """Latest visual verdict's evidence, keeping only line IDs that were shown in the final attempt."""
    shown: set[str] = set()
    for p in final_pages:
        if p in ocr_by_page:
            shown |= lines_with_ids(ocr_by_page[p], page=p, max_chars=max_chars_per_page)[1]
    valid: list[str] = []
    for lid in verdict.evidence_line_ids:
        p, idx = parse_line_id(lid)
        if p is None and len(final_pages) == 1:
            p = final_pages[0]  # bare "L39" is unambiguous when one page was sent
        if p is None or idx is None:
            continue
        lid = f"p{p}_L{idx}"
        if lid in shown and lid not in valid:
            valid.append(lid)
    return {
        "page_num": page_num,
        "pages_sent": list(final_pages),
        "line_ids": valid,
        "cited_line_ids": list(verdict.evidence_line_ids),
        "evidence_kind": verdict.evidence_kind,
        # A rule the model skipped silently defaults to passes=False; report it as not evaluated.
        "verdict_missing": _verdict_missing(raw_verdict),
        "pages_dropped": list(pages_dropped or []),
        "attempt": attempt,
        "skip_reason": skip_reason,
        "observation": verdict.observation,
        "confidence": float(verdict.confidence),
    }


# Keyword fallback for rules without an evidence_categories column value. Patterns are
# matched on word boundaries against "rule_name check_value" (underscores read as spaces).
_PAYMENT_TERMS = (
    r"payments?|paid|proof|receipts?|bank|cheques?|transfers?|transferencia|justificante|"
    r"pagad[oa]|recibo|virement|re[çc]us?|pay[ée]e?s?|acquitt[ée]e?s?|relev[ée]|statements?"
)
_KEYWORD_CATEGORIES: tuple[tuple[str, frozenset[str]], ...] = (
    (_PAYMENT_TERMS, frozenset({"SUPPORTING_DOC", "SIGNATURE_STAMP", "TOTALS"})),
    (r"translations?|translated|idioma|language|traduction|traducci[óo]n", frozenset({"SUPPORTING_DOC", "COVER_PAGE"})),
    (r"quotes?|presupuestos?|budget|suppliers?|proveedor|fournisseur|devis", frozenset({"SUPPORTING_DOC", "LINE_ITEMS"})),
    (
        r"stamps?|stamped|seals?|signatures?|signed|sello|firma(do)?|cachet|tampon|sign[ée]e?|aexcid",
        frozenset({"SIGNATURE_STAMP"}),
    ),
    (
        r"quantit(y|ies)|qty|unit prices?|line items?|totals?|subtotal|amounts? in words|words|arithmetic|"
        r"sums?|tax(es)?|vat|iva|tva|ht|ttc|tax regime|cantidad|precio unitario|importe|"
        r"montant( en lettres)?|en lettres|en letras|prix unitaire|quantit[ée]|"
        r"descriptions?|described|goods|items|articles?|concepto|d[ée]signation",
        frozenset({"LINE_ITEMS", "TOTALS"}),
    ),
    (
        r"pages|attached|attachments?|supporting|purchase orders?|order forms?|orden de compra|pro ?forma|"
        r"bon de commande|annex(es)?|anexos?|funds request|solicitud de fondos",
        frozenset({"SUPPORTING_DOC"}),
    ),
    (r"unrelated|all pages|every page|consistency|consistent", frozenset({"ALL"})),
)
_KEYWORD_PATTERNS = tuple((re.compile(rf"\b(?:{pat})\b"), cats) for pat, cats in _KEYWORD_CATEGORIES)

# Context pages always considered for selection, even when no rule names them.
_BASELINE_CATEGORIES = frozenset({"INVOICE_HEADER", "SIGNATURE_STAMP"})
_CATEGORY_ORDER = ("INVOICE_HEADER", "SIGNATURE_STAMP", "SUPPORTING_DOC", "TOTALS", "LINE_ITEMS", "COVER_PAGE")
_NON_EVIDENCE_CATEGORIES = frozenset({"BLANK", "UNKNOWN", ""})
_PAYMENT_PAGE_RE = re.compile(rf"\b(?:{_PAYMENT_TERMS})\b", re.IGNORECASE)


def rule_evidence_categories(rule: ComplianceRule) -> set[str]:
    """Page categories a visual rule needs: the CSV column when set, else the keyword fallback."""
    if rule.evidence_categories:
        return set(rule.evidence_categories)
    text = re.sub(r"[_\-]+", " ", f"{rule.rule_name} {rule.check_value or ''}").lower()
    cats: set[str] = set()
    for pattern, pattern_cats in _KEYWORD_PATTERNS:
        if pattern.search(text):
            cats |= pattern_cats
    return cats or {"INVOICE_HEADER"}


def _page_category(inv_by_page: dict[int, dict], page: int) -> str:
    return str(inv_by_page.get(page, {}).get("category", "")).upper()


def _is_payment_page(page: int, inv_by_page: dict[int, dict], page_facts: dict) -> bool:
    if _PAYMENT_PAGE_RE.search(str(inv_by_page.get(page, {}).get("description", ""))):
        return True
    return bool((page_facts.get(page) or {}).get("entities", {}).get("payment_markers"))


def select_evidence_pages(
    anchor: int,
    inv_by_page: dict[int, dict],
    available: set[int],
    rule_needs: list[set[str]],
    max_pages: int,
    page_facts: Optional[dict] = None,
) -> tuple[list[int], list[int]]:
    """
    Pick the pages sent to the vision model: (selected, dropped).

    Candidates are the anchor, every page whose category some rule needs (or every page
    when a rule needs ALL), plus page 1. Under the cap all candidates are kept. Over it,
    pages are picked round-robin across the needed categories (most-needed first), so each
    category gets one page before any gets a second; within a category, pages with payment
    evidence come first. The anchor is always kept. Deterministic for a given input.
    """
    page_facts = page_facts or {}
    cap = max(1, int(max_pages))
    wants_all = any("ALL" in needs for needs in rule_needs)
    target = set(_BASELINE_CATEGORIES)
    for needs in rule_needs:
        target |= needs - {"ALL"}

    candidates: list[int] = [anchor] if anchor in available else []
    for p in sorted(inv_by_page):
        if p in available and p not in candidates and (wants_all or _page_category(inv_by_page, p) in target):
            candidates.append(p)
    if 1 in available and 1 not in candidates:
        candidates.append(1)
    if len(candidates) <= cap:
        return candidates, []

    def _demand(cat: str) -> int:
        return sum(1 for needs in rule_needs if cat in needs or "ALL" in needs)

    queues: dict[str, list[int]] = {}
    for p in candidates:
        if p == anchor:
            continue
        queues.setdefault(_page_category(inv_by_page, p), []).append(p)
    for pages in queues.values():
        pages.sort(key=lambda p: (not _is_payment_page(p, inv_by_page, page_facts), p))
    order = sorted(
        queues,
        key=lambda c: (
            -_demand(c),
            _CATEGORY_ORDER.index(c) if c in _CATEGORY_ORDER else len(_CATEGORY_ORDER),
            c,
        ),
    )
    # The anchor's category is already covered, so it waits until the other categories had a turn.
    anchor_cat = _page_category(inv_by_page, anchor) if anchor in candidates else None
    if anchor_cat in order:
        order.remove(anchor_cat)
        order.append(anchor_cat)

    picked: list[int] = [anchor] if anchor in candidates else []
    while len(picked) < cap and any(queues[c] for c in order):
        for cat in order:
            if len(picked) >= cap:
                break
            if queues[cat]:
                picked.append(queues[cat].pop(0))

    rest = sorted(p for p in picked if p != anchor)
    selected = ([anchor] if anchor in picked else []) + rest
    dropped = sorted(p for p in candidates if p not in selected)
    return selected, dropped


def missing_evidence_categories(
    needs: set[str], inv_by_page: dict[int, dict], pages_sent: list[int]
) -> list[str]:
    """Categories the rule needs that exist in the document but were not among the pages sent."""
    doc_cats = {_page_category(inv_by_page, p) for p in inv_by_page} - _NON_EVIDENCE_CATEGORIES
    wanted = doc_cats if "ALL" in needs else (needs & doc_cats)
    sent_cats = {_page_category(inv_by_page, p) for p in pages_sent}
    return sorted(wanted - sent_cats)


def check_compliance_visual(
    state: AgentState,
    image_path: str,
    page_num: int,
    rules: list[ComplianceRule],
    ollama_url: str,
    model: str,
    max_evidence_pages: int = 6,
    provider: "LLMProvider | None" = None,
    timeout_s: int = 240,
    hybrid_visual: bool = True,
    store: Optional[ConfigStore] = None,
    trace_ocr: Optional[Callable[[int], Any]] = None,
    trace_ocr_max_chars_per_page: int = 3000,
) -> dict:
    """
    Run visual_check compliance rules against a page image.
    Sends all visual rules to the vision model in a single call and gets
    a pass/fail verdict with confidence and observation for each.
    Updates state.rule_results in-place (merges with field-based results).
    """
    visual_rules = [r for r in rules if r.check_type == "visual_check"]
    if not visual_rules:
        return {"success": True, "message": "No visual rules to check", "results": []}

    rule_lines = "\n".join(
        f'- "{r.rule_id}" ({r.severity}): {r.check_value or r.rule_name}'
        + (f" | hint: {r.agent_hint}" if r.agent_hint else "")
        for r in visual_rules
    )

    # Full-res paths (authoritative). image_path only fills a missing slot (tests / edge cases).
    page_by_num_full: dict[int, str] = {}
    for i, p in enumerate(state.page_image_paths or []):
        page_by_num_full[i + 1] = p
    if page_num not in page_by_num_full and image_path:
        page_by_num_full[page_num] = image_path

    page_by_num_medium: dict[int, str] | None = None
    if (
        hybrid_visual
        and getattr(state, "medium_page_paths", None)
        and len(state.medium_page_paths) == len(state.page_image_paths or [])
        and state.medium_page_paths
    ):
        page_by_num_medium = {
            i + 1: state.medium_page_paths[i] for i in range(len(state.medium_page_paths))
        }

    inv_by_page = {int(e.get("page", 0)): e for e in (state.page_inventory or []) if e.get("page")}

    rule_needs = {r.rule_id: rule_evidence_categories(r) for r in visual_rules}
    selected_pages, pages_dropped = select_evidence_pages(
        page_num,
        inv_by_page,
        set(page_by_num_full),
        list(rule_needs.values()),
        max_evidence_pages,
        page_facts=state.page_facts,
    )
    if pages_dropped:
        logger.info(
            "check_compliance_visual: page cap %d reached; sending %s, dropped %s",
            max_evidence_pages,
            selected_pages,
            pages_dropped,
        )

    # Traceability: OCR of every page that may be sent, so the model can cite lines.
    ocr_by_page: dict[int, Any] = {}
    if trace_ocr is not None:
        for p in selected_pages:
            try:
                ocr_p = trace_ocr(p)
            except Exception as e:  # tracing must never break the compliance check
                logger.warning("check_compliance_visual: OCR for page %d failed: %s", p, e)
                ocr_p = None
            if ocr_p is not None and not ocr_p.is_empty():
                ocr_by_page[p] = ocr_p

    def _ocr_block_for(pages: list[int]) -> str:
        blocks = []
        for p in pages:
            if p in ocr_by_page:
                blocks.append(
                    f"--- page_num={p} ---\n"
                    + format_lines_with_ids(ocr_by_page[p], page=p, max_chars=trace_ocr_max_chars_per_page)
                )
        return "\n".join(blocks) if blocks else "(no OCR text available)"

    def _evidence_lines_for(pages: list[int]) -> list[str]:
        lines = []
        for idx, p in enumerate(pages, start=1):
            inv = inv_by_page.get(p, {})
            cat = inv.get("category", "UNKNOWN")
            desc = inv.get("description", "")
            lines.append(f"- image#{idx} => page_num={p}, category={cat}, note={desc}")
        return lines

    def _run_vision(images_b64: list[str], prompt: str) -> tuple[dict | None, str | None, str]:
        """Returns (verdicts dict, raw_response fragment, error_message)."""
        raw_local = ""
        payload = {
            "model": model,
            "prompt": prompt,
            "images": images_b64,
            "stream": False,
            "options": {"temperature": 0.1},
        }
        try:
            if provider is not None:
                pname = getattr(provider, "provider_name", "")
                llm_result = provider.generate_json(
                    model=model,
                    prompt=prompt,
                    images_b64=images_b64,
                    temperature=0.1,
                    timeout_s=timeout_s,
                    response_format=provider_json_mode(pname),
                )
                raw_local = llm_result.content_text
                if llm_result.content_json is not None:
                    return llm_result.content_json, raw_local, None
                verdicts = json.loads(raw_local)
                return verdicts, raw_local, None
            resp = requests.post(f"{ollama_url}/api/generate", json=payload, timeout=timeout_s)
            resp.raise_for_status()
            raw_local = resp.json().get("response", "")
            raw_local = re.sub(r"```(?:json)?", "", raw_local).strip().rstrip("`").strip()
            verdicts = json.loads(raw_local)
            return verdicts, raw_local, None
        except json.JSONDecodeError as e:
            return None, raw_local, f"JSON parse error: {e}"
        except requests.RequestException as e:
            return None, raw_local, f"Ollama request failed: {e}"
        except Exception as e:
            logger.warning("Vision provider raised unexpected error: %s", e)
            return None, raw_local, f"Vision call failed: {e}"

    # Scaled JPEGs + retries: multiple full-DPI JPEGs often trigger Ollama 500 (OOM / context).
    attempt_plans: list[tuple[str, list[int], int]] = [
        ("multi_resized", list(selected_pages), 1280),
        ("multi_smaller", list(selected_pages), 768),
        ("single_anchor", [page_num], 1280),
    ]

    def _run_ladder(pbn: dict[int, str]) -> tuple[dict | None, str, str | None, list[int], str]:
        seen_signatures: set[tuple[tuple[int, ...], int]] = set()
        ver: dict | None = None
        raw_l = ""
        err: str | None = None
        finals = list(selected_pages)
        used = ""
        for tag, pages_try, max_side in attempt_plans:
            pages_try = [p for p in pages_try if p in pbn]
            if not pages_try:
                continue
            sig = (tuple(pages_try), max_side)
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            try:
                images_b64 = [image_to_base64_scaled(pbn[p], max_side=max_side) for p in pages_try]
            except OSError as e:
                err = f"Failed to read page image: {e}"
                logger.warning("check_compliance_visual: %s", err)
                continue
            ev_lines = _evidence_lines_for(pages_try)
            prompt = build_compliance_visual_prompt(
                "\n".join(ev_lines),
                rule_lines,
                ocr_block=_ocr_block_for(pages_try) if trace_ocr is not None else "",
                cite_lines=trace_ocr is not None,
            )
            ver, raw_l, err = _run_vision(images_b64, prompt)
            if ver is not None and not isinstance(ver, dict):
                ver, err = None, f"Vision model returned {type(ver).__name__}, expected a JSON object"
            if ver is not None:
                finals = pages_try
                used = tag
                if tag != "multi_resized":
                    logger.info(
                        "check_compliance_visual: succeeded after %s (%d image(s), max_side=%d)",
                        tag,
                        len(pages_try),
                        max_side,
                    )
                break
            logger.warning(
                "check_compliance_visual: attempt %s failed: %s — retrying with smaller payload if possible",
                tag,
                err,
            )
        return ver, raw_l, err, finals, used

    verdicts: dict | None = None
    raw = ""
    last_err: str | None = None
    final_pages = list(selected_pages)
    attempt = ""

    if page_by_num_medium:
        verdicts, raw, last_err, final_pages, attempt = _run_ladder(page_by_num_medium)
        attempt = f"medium:{attempt}" if attempt else ""
    if verdicts is None:
        if page_by_num_medium:
            logger.info("check_compliance_visual: hybrid promoting to full-res disk images for visual check")
        verdicts, raw, last_err, final_pages, attempt = _run_ladder(page_by_num_full)
        attempt = f"full:{attempt}" if attempt else ""

    if verdicts is None:
        return {"success": False, "error": last_err or "Visual model returned no verdict", "raw_response": raw}

    new_results = []
    passed_ids = []
    failed_ids = []
    not_evaluated: list[dict] = []
    validated_verdicts: dict[str, VisualVerdictModel] = {}
    if attempt and not attempt.endswith("multi_resized"):
        logger.info("check_compliance_visual: verdicts from attempt %s, pages %s", attempt, final_pages)

    for rule in visual_rules:
        raw_verdict = verdicts.get(rule.rule_id)
        verdict = VisualVerdictModel.model_validate(raw_verdict if isinstance(raw_verdict, dict) else {})
        validated_verdicts[rule.rule_id] = verdict
        passes = verdict.passes
        confidence = float(verdict.confidence)
        observation = verdict.observation

        # A verdict is only meaningful if the model saw the pages the rule needs and answered it.
        missing_cats = missing_evidence_categories(rule_needs[rule.rule_id], inv_by_page, final_pages)
        skip_reason = ""
        if missing_cats:
            skip_reason = (
                f"Not evaluated: evidence pages not sent ({', '.join(missing_cats)}); "
                f"vision call {attempt or 'unknown'} saw only page(s) {final_pages}"
            )
        elif _verdict_missing(raw_verdict):
            skip_reason = "Not evaluated: vision model returned no verdict for this rule"

        if skip_reason:
            status = "skipped"
            message = skip_reason
        else:
            status = "passed" if passes else "failed"
            message = observation
        rr = RuleResult(
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            field_id=rule.field_id,
            status=status,
            severity=rule.severity,
            message=message,
            agent_notes=f"visual check page {page_num}, confidence={confidence:.2f}",
        )
        new_results.append(rr)
        if status == "passed":
            passed_ids.append(rule.rule_id)
        elif status == "failed":
            failed_ids.append(rule.rule_id)
        else:
            not_evaluated.append({"rule_id": rule.rule_id, "severity": rule.severity, "reason": skip_reason})

        logger.info(f"  visual [{status.upper()}] {rule.rule_id}: {message} (conf={confidence:.2f})")

        # Evidence tracking for visual rules.
        required_slots = required_slots_for_rule(rule)
        refs = state.rule_evidence.get(rule.rule_id, {}).get("refs", [])
        # Keep all evidence pages used in this visual call.
        for p in final_pages:
            refs.append({"page_num": p, "source": "visual_evidence_page"})
        refs.append({"page_num": page_num, "observation": observation, "confidence": confidence})
        filled_slots = ["visual_observation"] if "visual_observation" in required_slots else []

        # Deterministic cross-page linkage for payment-proof style rules.
        rule_text = f"{rule.rule_name} {rule.check_value}".lower()
        if "payment" in rule_text or "justificante" in rule_text or "proof" in rule_text:
            invoice_page = next(
                (p for p, facts in state.page_facts.items() if facts.get("category") == "INVOICE_HEADER"),
                None,
            )
            support_pages = [p for p in final_pages if p != invoice_page]
            for sp in support_pages:
                if invoice_page is not None and sp in state.page_facts:
                    linkage = link_pages(state.page_facts.get(invoice_page, {}), state.page_facts.get(sp, {}))
                    refs.append(
                        {
                            "linkage": linkage,
                            "invoice_page": invoice_page,
                            "supporting_page": sp,
                        }
                    )
                    if linkage.get("linked"):
                        filled_slots.append("invoice_receipt_link")
                    if state.page_facts.get(sp, {}).get("entities", {}).get("payment_markers"):
                        filled_slots.append("payment_indicator")
                    filled_slots.append("receipt_page_candidate")

        filled_slots = sorted(set(filled_slots))
        missing_slots = [s for s in required_slots if s not in filled_slots]
        state.rule_evidence[rule.rule_id] = {
            "required_slots": required_slots,
            "filled_slots": filled_slots,
            "missing_slots": missing_slots,
            "refs": refs,
        }
        if skip_reason:
            # Marks a terminal "not evaluated" outcome so check_compliance does not re-queue it.
            state.rule_evidence[rule.rule_id][VISUAL_SKIP_KEY] = skip_reason
        if trace_ocr is not None:
            state.rule_evidence[rule.rule_id]["trace"] = _visual_trace_record(
                verdict,
                raw_verdict,
                page_num,
                final_pages,
                ocr_by_page,
                trace_ocr_max_chars_per_page,
                pages_dropped=pages_dropped,
                attempt=attempt,
                skip_reason=skip_reason,
            )
        policy_refs = _policy_refs_for_rule(state, rule)
        state.rule_policy_refs[rule.rule_id] = policy_refs
        # A visual PASS is definitive for compliance state; missing optional linkage
        # slots (see required_slots_for_rule "payment" heuristics) must not leave the
        # rule stuck in "candidate" — that incorrectly blocked finish() downstream.
        if status == "passed":
            state.rule_state[rule.rule_id] = "finalized_pass" if policy_refs else "needs_review"
        elif status == "failed":
            state.rule_state[rule.rule_id] = "finalized_fail"
        else:
            # Re-running the same call would see the same pages; a human has to judge it.
            state.rule_state[rule.rule_id] = "needs_review"

    backfilled_fields: list[str] = []
    if store is not None:
        for rule in visual_rules:
            verdict = validated_verdicts[rule.rule_id]
            nr = next((r for r in new_results if r.rule_id == rule.rule_id), None)
            if not nr or nr.status != "passed":
                continue
            merged = _merge_visual_field_updates(
                state, store, rule.rule_id, page_num, verdict.field_updates
            )
            backfilled_fields.extend(merged)
        backfilled_fields = list(dict.fromkeys(backfilled_fields))

    # Merge into state — replace any existing entries for these rule_ids
    existing = [r for r in state.rule_results if r.rule_id not in {x.rule_id for x in new_results}]
    state.rule_results = existing + new_results

    state.passed_rules = [r.rule_id for r in state.rule_results if r.status == "passed"]
    state.failed_rules = [r.rule_id for r in state.rule_results if r.status == "failed"]
    # Clear the pending list for rules that were just evaluated
    evaluated_ids = {r.rule_id for r in new_results}
    state.visual_checks_pending = [r for r in state.visual_checks_pending if r not in evaluated_ids]

    errors = [r for r in new_results if r.status == "failed" and r.severity == "error"]
    warnings = [r for r in new_results if r.status == "failed" and r.severity == "warning"]

    return {
        "success": True,
        "page_num": page_num,
        "evidence_pages": final_pages,
        "pages_dropped": pages_dropped,
        "attempt": attempt,
        "visual_rules_checked": len(visual_rules),
        "passed": len(passed_ids),
        "failed_errors": [{"rule_id": r.rule_id, "message": r.message} for r in errors],
        "failed_warnings": [{"rule_id": r.rule_id, "message": r.message} for r in warnings],
        "not_evaluated": not_evaluated,
        "backfilled_fields": backfilled_fields,
    }
