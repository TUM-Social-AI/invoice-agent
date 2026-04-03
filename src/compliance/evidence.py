"""
Evidence schemas and cross-page linkage helpers for compliance grounding.
"""

from __future__ import annotations

import re
from typing import Any


def required_slots_for_rule(rule: Any) -> list[str]:
    """
    Return required evidence slots for a rule.
    Rule-specific overrides can be added here over time.
    """
    # Rule-name/check-value heuristics for payment-proof type rules.
    text = f"{getattr(rule, 'rule_name', '')} {getattr(rule, 'check_value', '')}".lower()
    if "proof_of_payment" in text or "justificante" in text or "payment" in text:
        return ["receipt_page_candidate", "payment_indicator", "invoice_receipt_link"]

    if getattr(rule, "check_type", "") == "visual_check":
        return ["visual_observation"]
    if getattr(rule, "check_type", "") in ("cross_field", "conditional_check"):
        return ["field_values"]
    if getattr(rule, "check_type", "") in ("required", "regex", "range", "enum", "required_one_of"):
        return ["field_values"]
    return ["field_values"]


def normalize_date_token(v: str) -> str:
    s = str(v).strip()
    # Basic normalization for dd/mm/yyyy and yyyy-mm-dd forms.
    m = re.match(r"^(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})$", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.match(r"^(\d{4})[/.\-](\d{2})[/.\-](\d{2})$", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return s.lower()


def link_pages(primary_facts: dict, supporting_facts: dict, amount_tolerance: float = 0.02) -> dict:
    """
    Deterministically score linkage between two pages using amount/date/reference overlap.
    """
    p_entities = primary_facts.get("entities", {}) if primary_facts else {}
    s_entities = supporting_facts.get("entities", {}) if supporting_facts else {}

    amount_match = False
    p_amounts = p_entities.get("amounts", [])
    s_amounts = s_entities.get("amounts", [])
    for pa in p_amounts:
        for sa in s_amounts:
            try:
                if abs(float(pa) - float(sa)) <= amount_tolerance:
                    amount_match = True
                    break
            except Exception:
                continue
        if amount_match:
            break

    p_dates = {normalize_date_token(d) for d in p_entities.get("dates", [])}
    s_dates = {normalize_date_token(d) for d in s_entities.get("dates", [])}
    date_match = bool(p_dates & s_dates)

    p_refs = {str(x).lower() for x in p_entities.get("references", [])}
    s_refs = {str(x).lower() for x in s_entities.get("references", [])}
    reference_match = bool(p_refs & s_refs)

    matched_signals = sum([amount_match, date_match, reference_match])
    linked = matched_signals >= 2 or (amount_match and date_match)

    return {
        "linked": linked,
        "amount_match": amount_match,
        "date_match": date_match,
        "reference_match": reference_match,
        "matched_signals": matched_signals,
    }
