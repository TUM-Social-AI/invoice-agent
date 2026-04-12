"""Moved implementations for compliance_eval.py."""

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

def _policy_refs_for_rule(state: AgentState, rule: ComplianceRule) -> list[dict]:
    """
    Retrieve policy references from loaded learnings/context for a rule.
    If none are found, return a fallback reference to the rule config source.
    """
    refs: list[dict] = []
    ctx = state.learnings_context or ""
    if not ctx.strip():
        return [{"source": "config/csv/compliance_rules.csv", "snippet_id": rule.rule_id}]

    rule_id = (rule.rule_id or "").strip()
    rule_id_l = rule_id.lower()
    # 1) Direct grounding: prefer explicit mentions of the rule id.
    # Use word-boundaries so we don't match partial ids inside unrelated text.
    if rule_id_l:
        pat = re.compile(rf"\b{re.escape(rule_id_l)}\b")
        direct = [line.strip() for line in ctx.splitlines() if pat.search(line.lower())]
        for line in direct[:3]:
            snippet_id = rule_id
            m = re.match(r"^- \[(L\d+)\]", line)
            if m:
                snippet_id = m.group(1)
            refs.append({"source": "learnings", "snippet_id": snippet_id, "snippet": line[:240]})
        if refs:
            return refs

    # 2) Fallback grounding: rank learnings bullet lines by keyword overlap.
    # This helps when the learning describes the *policy behavior* but does not
    # mention the literal `R_*` rule id.
    keywords_blob = " ".join(
        [str(rule.agent_hint or ""), str(rule.rule_name or ""), str(rule.field_id or ""), str(rule.check_type or "")]
    ).lower()
    keywords = {
        k
        for k in re.findall(r"[a-z0-9_]+", keywords_blob)
        if len(k) >= 3 and k not in {"true", "false", "none"}
    }

    ranked: list[tuple[int, str, str]] = []  # (score, snippet_id, snippet)
    for line in ctx.splitlines():
        s = line.strip()
        if not s.startswith("- ["):
            continue
        m = re.match(r"^- \[(L\d+)\]", s)
        snippet_id = m.group(1) if m else rule_id
        score = 0
        l = s.lower()
        for kw in keywords:
            if kw in l:
                score += 1
        if score > 0:
            ranked.append((score, snippet_id, s))

    ranked.sort(key=lambda x: x[0], reverse=True)
    for _, snippet_id, snippet in ranked[:2]:
        refs.append({"source": "learnings", "snippet_id": snippet_id, "snippet": snippet[:240]})

    if not refs:
        refs.append({"source": "config/csv/compliance_rules.csv", "snippet_id": rule_id})
    return refs

def _normalize_numeric(value: Any) -> Optional[float]:
    """
    Parse numbers from common OCR formats:
    - thousands separators: "1.190,00" -> 1190.00
    - decimal separators: "1,90" -> 1.90
    - currency symbols: "€ 1.190,00" -> 1190.00
    - percent suffix: "19%" -> 19.0
    """
    if value is None:
        return None

    s = str(value).strip()
    if not s or s.lower() in ("null", "none"):
        return None

    # Strip HTML/XML fragments (OCR / PDF text sometimes embeds tags).
    s = re.sub(r"<[^>]+>", "", s)
    s = s.replace("&nbsp;", " ").replace("&#160;", " ").replace("\xa0", " ")
    s = s.strip()
    if not s or s.lower() in ("null", "none"):
        return None

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # Remove currency symbols and whitespace, then try to extract the first numeric token.
    # OCR strings are often like: "Total: 1.190,00 EUR" or "€ 1 190,00".
    s = re.sub(r"[€$£]", "", s)
    s = s.replace(" ", "")
    s = s.rstrip("%")

    m = re.search(r"[+-]?\d[\d\.,]*", s)
    if not m:
        return None
    s = m.group(0)

    # If both separators are present, assume "." is thousands and "," is decimal.
    if "," in s and "." in s:
        s = s.replace(".", "")
        s = s.replace(",", ".")
    elif "," in s:
        # Heuristic: single comma is treated as decimal separator.
        # If multiple commas exist, treat them as thousands separators.
        if s.count(",") == 1:
            s = s.replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(".") > 1:
        # Multiple dots but no comma: assume dots are thousands separators.
        parts = s.split(".")
        s = "".join(parts[:-1]) + "." + parts[-1]

    if s in ("", "-", "+"):
        return None

    try:
        v = float(s)
    except (ValueError, TypeError):
        return None

    return -v if negative else v

def _safe_eval_numeric(expr: str) -> float:
    """
    Safely evaluate a numeric arithmetic expression.

    Supports only numeric literals and operators: +, -, *, /, unary +/- and parentheses.
    Any other syntax (variables, function calls, attributes, etc.) raises ValueError.
    """
    tree = ast.parse(expr, mode="eval")

    allowed_binops = (ast.Add, ast.Sub, ast.Mult, ast.Div)
    allowed_unops = (ast.UAdd, ast.USub)
    allowed_consts = (ast.Constant,)

    def _check(node: ast.AST) -> None:
        if isinstance(node, ast.Expression):
            _check(node.body)
            return
        if isinstance(node, allowed_consts):
            # Only allow int/float constants.
            val = node.value
            if not isinstance(val, (int, float)):
                raise ValueError(f"Non-numeric constant in expression: {val!r}")
            return
        if isinstance(node, ast.BinOp):
            if not isinstance(node.op, allowed_binops):
                raise ValueError(f"Disallowed operator: {type(node.op).__name__}")
            _check(node.left)
            _check(node.right)
            return
        if isinstance(node, ast.UnaryOp):
            if not isinstance(node.op, allowed_unops):
                raise ValueError(f"Disallowed unary operator: {type(node.op).__name__}")
            _check(node.operand)
            return
        raise ValueError(f"Disallowed expression node: {type(node).__name__}")

    _check(tree)

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            return float(node.value)
        if isinstance(node, ast.UnaryOp):
            v = _eval(node.operand)
            if isinstance(node.op, ast.USub):
                return -v
            if isinstance(node.op, ast.UAdd):
                return v
        if isinstance(node, ast.BinOp):
            l = _eval(node.left)
            r = _eval(node.right)
            if isinstance(node.op, ast.Add):
                return l + r
            if isinstance(node.op, ast.Sub):
                return l - r
            if isinstance(node.op, ast.Mult):
                return l * r
            if isinstance(node.op, ast.Div):
                return l / r
        raise ValueError(f"Unexpected node during eval: {type(node).__name__}")

    return _eval(tree)

def _get_field_value(state: AgentState, field_name: str) -> Any:
    result = state.extracted_fields.get(field_name)
    return result.extracted_value if result else None

def _evaluate_rule(rule: ComplianceRule, state: AgentState, store: Optional[ConfigStore]) -> RuleResult:
    field_result = state.extracted_fields.get(
        _field_name_for_id(rule.field_id, state)
    )
    value = field_result.extracted_value if field_result else None

    def _field_is_required() -> bool:
        if store is None:
            # Conservative default: treat unknown fields as required.
            return True
        f = store.get_field_by_id(rule.field_id)
        return f.required if f else True

    def fail(msg=None):
        return RuleResult(
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            field_id=rule.field_id,
            status="failed",
            severity=rule.severity,
            message=msg or rule.error_message,
        )

    def passed():
        return RuleResult(
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            field_id=rule.field_id,
            status="passed",
            severity=rule.severity,
            message="OK",
        )

    if rule.check_type == "visual_check":
        # Visual rules require a page image — skip here, handled by check_compliance_visual()
        return RuleResult(
            rule_id=rule.rule_id,
            rule_name=rule.rule_name,
            field_id=rule.field_id,
            status="skipped",
            severity=rule.severity,
            message="Visual check — call check_compliance_visual(image_path, page_num) to evaluate",
        )

    if rule.check_type == "required":
        return passed() if value not in (None, "", "null") else fail()

    if rule.check_type == "regex":
        if value in (None, "", "null"):
            if _field_is_required():
                return fail(f"Value is null/missing, cannot match pattern {rule.check_value}")
            return RuleResult(
                rule_id=rule.rule_id,
                rule_name=rule.rule_name,
                field_id=rule.field_id,
                status="skipped",
                severity=rule.severity,
                message="Value missing/null — skipping regex check",
            )
        pattern = rule.check_value
        return passed() if re.match(pattern, str(value)) else fail()

    if rule.check_type == "range":
        if value in (None, "", "null"):
            if _field_is_required():
                return fail("Value is null/missing, cannot run range check")
            return RuleResult(
                rule_id=rule.rule_id,
                rule_name=rule.rule_name,
                field_id=rule.field_id,
                status="skipped",
                severity=rule.severity,
                message="Value missing/null — skipping range check",
            )
        try:
            lo, hi = map(float, rule.check_value.split(","))
            v = _normalize_numeric(value)
            if v is None:
                if not _field_is_required():
                    return RuleResult(
                        rule_id=rule.rule_id,
                        rule_name=rule.rule_name,
                        field_id=rule.field_id,
                        status="skipped",
                        severity=rule.severity,
                        message=f"Unparseable numeric value {value!r} — skipped for optional field",
                    )
                raise ValueError(f"Could not normalize value '{value}'")
            return passed() if lo <= v <= hi else fail()
        except (ValueError, TypeError) as e:
            if not _field_is_required():
                return RuleResult(
                    rule_id=rule.rule_id,
                    rule_name=rule.rule_name,
                    field_id=rule.field_id,
                    status="skipped",
                    severity=rule.severity,
                    message=f"Range check skipped (optional field): {e}",
                )
            return fail(f"Could not parse value '{value}' as number for range check")

    if rule.check_type == "enum":
        if value in (None, "", "null"):
            if _field_is_required():
                return fail("Value is null/missing, cannot run enum check")
            return RuleResult(
                rule_id=rule.rule_id,
                rule_name=rule.rule_name,
                field_id=rule.field_id,
                status="skipped",
                severity=rule.severity,
                message="Value missing/null — skipping enum check",
            )
        allowed = [x.strip() for x in rule.check_value.split(",")]
        clean = str(value).strip().upper()
        return passed() if clean in [a.upper() for a in allowed] else fail()

    if rule.check_type == "cross_field":
        # e.g. check_value = "net_amount * vat_rate"  → compare to field value
        try:
            expr_template = rule.check_value

            # Replace field names with their numeric values.
            # Important: some fields contain the substring "rate" but are NOT
            # percentages (e.g. per_diem_rate = EUR/day). So we try both:
            #   - scaled interpretation (rate treated as percent → /100)
            #   - unscaled interpretation (rate used as-is)
            replacements: dict[str, float] = {}
            for fname, fresult in state.extracted_fields.items():
                v = fresult.extracted_value
                if v is None:
                    continue
                n = _normalize_numeric(v)
                if n is None:
                    continue
                replacements[fname] = n

            # Identify all identifiers (potential field names) referenced in the
            # expression. If any are missing from replacements (no extracted value),
            # raise early so the rule is cleanly skipped rather than crashing in
            # _safe_eval_numeric with "Disallowed expression node: Name".
            referenced_names = set(re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', expr_template))
            missing_names = referenced_names - set(replacements.keys())
            if missing_names:
                raise ValueError(
                    f"Missing extracted values for field(s): {', '.join(sorted(missing_names))}"
                )

            def _render_expr(scale_rate_percent: bool) -> str:
                rendered = expr_template
                for fname, num in replacements.items():
                    n = num
                    if (
                        scale_rate_percent
                        and "rate" in fname
                        and n > 1
                        and "*" in expr_template
                    ):
                        n = n / 100.0
                    rendered = rendered.replace(fname, str(n))
                return rendered

            expected_scaled = _safe_eval_numeric(_render_expr(scale_rate_percent=True))
            expected_unscaled = _safe_eval_numeric(_render_expr(scale_rate_percent=False))

            actual = _normalize_numeric(value)
            if actual is None:
                raise ValueError(f"Could not parse actual value '{value}' as number")
            tol = 0.02
            if abs(expected_scaled - actual) <= tol or abs(expected_unscaled - actual) <= tol:
                return passed()
            return fail(
                f"Expected ~{expected_scaled:.2f} (scaled) or ~{expected_unscaled:.2f} (unscaled), got {actual:.2f}"
            )
        except Exception as e:
            return RuleResult(
                rule_id=rule.rule_id, rule_name=rule.rule_name,
                field_id=rule.field_id, status="skipped", severity=rule.severity,
                message=f"Cross-field check skipped (missing values): {e}",
            )

    if rule.check_type == "conditional_check":
        # e.g. "if reverse_charge_flag=true then vat_amount=0"
        try:
            parts = rule.check_value.split(" then ")
            condition_part = parts[0].replace("if ", "").strip()
            consequence_part = parts[1].strip()

            cond_field, cond_val = condition_part.split("=")
            cond_field = cond_field.strip()
            cond_val = cond_val.strip().lower()

            actual_cond = _get_field_value(state, cond_field)
            if str(actual_cond).lower() != cond_val:
                return RuleResult(
                    rule_id=rule.rule_id, rule_name=rule.rule_name,
                    field_id=rule.field_id, status="skipped", severity=rule.severity,
                    message="Condition not met, rule skipped",
                )

            cons_field, cons_val = consequence_part.split("=")
            cons_field = cons_field.strip()
            cons_val = cons_val.strip().lower()
            actual_cons = _get_field_value(state, cons_field)

            if cons_val in ("null", "none"):
                ok = actual_cons in (None, "null", "", 0, "0")
            elif cons_val == "0":
                ok = actual_cons in (None, 0, "0", 0.0)
            else:
                ok = str(actual_cons).lower() == cons_val

            return passed() if ok else fail()
        except Exception as e:
            return RuleResult(
                rule_id=rule.rule_id, rule_name=rule.rule_name,
                field_id=rule.field_id, status="skipped", severity=rule.severity,
                message=f"Conditional check skipped: {e}",
            )

    if rule.check_type == "required_one_of":
        # check_value = comma-separated list of alternative field_ids
        alternatives = [rule.field_id] + [x.strip() for x in rule.check_value.split(",")]
        for alt_id in alternatives:
            fname = _field_name_for_id(alt_id, state)
            if fname and state.extracted_fields.get(fname) and \
               state.extracted_fields[fname].extracted_value not in (None, "", "null"):
                return passed()
        return fail()

    return RuleResult(
        rule_id=rule.rule_id, rule_name=rule.rule_name,
        field_id=rule.field_id, status="skipped", severity=rule.severity,
        message=f"Unknown check_type: {rule.check_type}",
    )

def _field_name_for_id(field_id: str, state: AgentState) -> Optional[str]:
    """
    Look up field_name from field_id in extracted_fields.
    Also accepts field_name directly (for tests and direct usage).
    """
    # Direct match by field_id stored in FieldResult
    for fname, fresult in state.extracted_fields.items():
        if fresult.field_id == field_id:
            return fname
    # Fallback: treat field_id as a field_name directly
    if field_id in state.extracted_fields:
        return field_id
    return None

def check_compliance(state: AgentState, rules: list[ComplianceRule], store: Optional[ConfigStore] = None) -> dict:
    """Run all rules against current extracted fields. Updates state."""
    results = []
    failed = []
    passed = []

    # Build a lookup of results already committed by check_compliance_visual so we
    # don't re-evaluate visual rules and re-add them to visual_checks_pending.
    already_evaluated: dict[str, RuleResult] = {
        r.rule_id: r
        for r in state.rule_results
        if r.status in ("passed", "failed") and r.rule_id not in (
            r2.rule_id for r2 in state.rule_results if r2.status == "skipped"
        )
    }
    # Simpler: build a set of rule IDs that already have a definitive verdict
    definitive_rule_ids: set[str] = {
        r.rule_id for r in state.rule_results if r.status in ("passed", "failed")
    }

    for rule in rules:
        # If check_compliance_visual has already evaluated this rule (passed/failed),
        # reuse that result instead of re-evaluating and re-adding to visual_pending.
        if rule.rule_id in definitive_rule_ids:
            existing = next(r for r in state.rule_results if r.rule_id == rule.rule_id)
            results.append(existing)
            if existing.status == "passed":
                passed.append(rule.rule_id)
            elif existing.status == "failed":
                failed.append(rule.rule_id)
            continue

        result = _evaluate_rule(rule, state, store)
        results.append(result)
        if result.status == "passed":
            passed.append(rule.rule_id)
        elif result.status == "failed":
            failed.append(rule.rule_id)

        # Evidence schema tracking per rule.
        required_slots = required_slots_for_rule(rule)
        filled_slots: list[str] = []
        if result.status in ("passed", "failed"):
            # Base slot for field-based checks.
            if "field_values" in required_slots:
                filled_slots.append("field_values")
            # Visual checks are finalized by visual tool; keep conservative here.
            if rule.check_type == "visual_check" and "visual_observation" in required_slots:
                if result.status in ("passed", "failed") and "visual" not in result.message.lower():
                    # avoid false positives when visual checks are still skipped placeholders
                    filled_slots.append("visual_observation")
        missing_slots = [s for s in required_slots if s not in filled_slots]
        state.rule_evidence[rule.rule_id] = {
            "required_slots": required_slots,
            "filled_slots": filled_slots,
            "missing_slots": missing_slots,
            "refs": state.rule_evidence.get(rule.rule_id, {}).get("refs", []),
        }
        policy_refs = _policy_refs_for_rule(state, rule)
        state.rule_policy_refs[rule.rule_id] = policy_refs
        if result.status == "passed":
            state.rule_state[rule.rule_id] = "finalized_pass" if policy_refs else "needs_review"
        elif result.status == "failed":
            state.rule_state[rule.rule_id] = "finalized_fail"
        elif result.status == "skipped":
            state.rule_state[rule.rule_id] = "candidate"

    state.rule_results = results
    state.failed_rules = failed
    state.passed_rules = passed

    errors = [r for r in results if r.status == "failed" and r.severity == "error"]
    warnings = [r for r in results if r.status == "failed" and r.severity == "warning"]

    # Visual skips are expected — handled separately by check_compliance_visual().
    # Only include rules that haven't been evaluated yet (no definitive verdict).
    visual_pending = [
        r for r in results
        if r.status == "skipped" and "visual" in r.message.lower()
        and r.rule_id not in definitive_rule_ids
    ]

    # Non-visual skips mean a check couldn't run because a required field value was missing.
    # These are NOT safe to ignore — the math/format check simply didn't execute.
    # Surfaced separately so the agent knows it needs to extract the missing fields first.
    field_missing_skips = [
        r for r in results
        if r.status == "skipped" and "visual" not in r.message.lower()
    ]
    state.skipped_checks = [
        {"rule_id": r.rule_id, "severity": r.severity, "reason": r.message}
        for r in field_missing_skips
    ]
    state.visual_checks_pending = [r.rule_id for r in visual_pending]

    # Error-severity skipped checks block all_errors_resolved — the check never ran,
    # so we can't claim all errors are resolved.
    error_skips = [r for r in field_missing_skips if r.severity == "error"]

    return {
        "total_rules": len(rules),
        "passed": len(passed),
        "failed_errors": [{"rule_id": r.rule_id, "message": r.message} for r in errors],
        "failed_warnings": [{"rule_id": r.rule_id, "message": r.message} for r in warnings],
        "skipped_checks": [{"rule_id": r.rule_id, "severity": r.severity, "reason": r.message}
                           for r in field_missing_skips],
        "visual_checks_pending": [r.rule_id for r in visual_pending],
        "all_errors_resolved": len(errors) == 0 and len(visual_pending) == 0 and len(error_skips) == 0,
    }
