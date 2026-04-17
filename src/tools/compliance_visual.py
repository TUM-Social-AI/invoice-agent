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
from typing import Any, Optional

import requests
from PIL import Image

from src.agent.state import AgentState, FieldResult, RuleResult
from src.compliance.evidence import required_slots_for_rule, link_pages
from src.config.loader import ConfigStore, ComplianceRule
from src.llm.base import LLMProvider
from src.models.tool_io_models import VisualVerdictModel
from src.prompts.llm_prompts import build_compliance_visual_prompt
from src.tools.vision_llm import _sanitize_extracted_string_value
from src.tools.pdf_pages import image_to_base64_scaled
from src.tools.compliance_eval import _evaluate_rule, _policy_refs_for_rule

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

    def _rule_target_categories(rule_text: str) -> set[str]:
        txt = rule_text.lower()
        cats = {"INVOICE_HEADER"}
        if any(k in txt for k in ("payment", "proof", "justificante", "receipt", "bank")):
            cats |= {"SIGNATURE_STAMP", "SUPPORTING_DOC", "TOTALS"}
        if any(k in txt for k in ("translation", "translated", "idioma", "language")):
            cats |= {"SUPPORTING_DOC", "COVER_PAGE"}
        if any(k in txt for k in ("quote", "presupuesto", "budget", "supplier")):
            cats |= {"SUPPORTING_DOC", "LINE_ITEMS"}
        if any(k in txt for k in ("stamp", "seal", "signature", "signed")):
            cats |= {"SIGNATURE_STAMP"}
        return cats

    target_categories: set[str] = {"INVOICE_HEADER", "SIGNATURE_STAMP"}
    for r in visual_rules:
        target_categories |= _rule_target_categories(f"{r.rule_name} {r.check_value or ''}")

    selected_pages: list[int] = []
    if page_num not in selected_pages:
        selected_pages.append(page_num)
    for p, e in sorted(inv_by_page.items()):
        cat = str(e.get("category", "")).upper()
        if cat in target_categories and p not in selected_pages:
            selected_pages.append(p)
    # Backstop: include first page if still missing
    if 1 in page_by_num_full and 1 not in selected_pages:
        selected_pages.append(1)

    selected_pages = [p for p in selected_pages if p in page_by_num_full][: max(1, int(max_evidence_pages))]

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
                llm_result = provider.generate_json(
                    model=model,
                    prompt=prompt,
                    images_b64=images_b64,
                    temperature=0.1,
                    timeout_s=timeout_s,
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

    def _run_ladder(pbn: dict[int, str]) -> tuple[dict | None, str, str | None, list[int]]:
        seen_signatures: set[tuple[tuple[int, ...], int]] = set()
        ver: dict | None = None
        raw_l = ""
        err: str | None = None
        finals = list(selected_pages)
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
            prompt = build_compliance_visual_prompt("\n".join(ev_lines), rule_lines)
            ver, raw_l, err = _run_vision(images_b64, prompt)
            if ver is not None:
                finals = pages_try
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
        return ver, raw_l, err, finals

    verdicts: dict | None = None
    raw = ""
    last_err: str | None = None
    final_pages = list(selected_pages)

    if page_by_num_medium:
        verdicts, raw, last_err, final_pages = _run_ladder(page_by_num_medium)
    if verdicts is None:
        if page_by_num_medium:
            logger.info("check_compliance_visual: hybrid promoting to full-res disk images for visual check")
        verdicts, raw, last_err, final_pages = _run_ladder(page_by_num_full)

    if verdicts is None:
        return {"success": False, "error": last_err or "Visual model returned no verdict", "raw_response": raw}

    new_results = []
    passed_ids = []
    failed_ids = []
    validated_verdicts: dict[str, VisualVerdictModel] = {}

    for rule in visual_rules:
        verdict = VisualVerdictModel.model_validate(verdicts.get(rule.rule_id, {}))
        validated_verdicts[rule.rule_id] = verdict
        passes = verdict.passes
        confidence = float(verdict.confidence)
        observation = verdict.observation

        status = "passed" if passes else "failed"
        rr = RuleResult(
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            field_id=rule.field_id,
            status=status,
            severity=rule.severity,
            message=observation,
            agent_notes=f"visual check page {page_num}, confidence={confidence:.2f}",
        )
        new_results.append(rr)
        if passes:
            passed_ids.append(rule.rule_id)
        else:
            failed_ids.append(rule.rule_id)

        logger.info(f"  visual [{status.upper()}] {rule.rule_id}: {observation} (conf={confidence:.2f})")

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
            state.rule_state[rule.rule_id] = "candidate"

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
        "visual_rules_checked": len(visual_rules),
        "passed": len(passed_ids),
        "failed_errors": [{"rule_id": r.rule_id, "message": r.message} for r in errors],
        "failed_warnings": [{"rule_id": r.rule_id, "message": r.message} for r in warnings],
        "backfilled_fields": backfilled_fields,
    }
