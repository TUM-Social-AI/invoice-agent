"""Plain-English rule wording and one consistent set of counts for the browser UI.

The agent's tool results carry rule ids (R_VIA_004), snake_case names (some Spanish) and raw
dicts. Everything the page shows about rules goes through this module so that the per-step
headlines, the rule rows, and the final banner are all built from the same buckets:

    passed · blocking error (failed, error severity) · warning (failed, warning severity)
    · couldn't be checked (rule did not run, e.g. a needed value is missing)
"""

from __future__ import annotations

from typing import Any, Iterable

# English labels keyed by `rule_name` in config/csv/compliance_rules.csv (names repeat across
# invoice types, so ~45 labels cover all rules). Unknown names fall back to `humanize`.
RULE_LABELS: dict[str, str] = {
    # Field-based checks (Spanish names in the config)
    "fecha_requerida": "Document date present",
    "importe_total_requerido": "Total amount present",
    "total_requerido": "Total amount present",
    "beneficiario_requerido": "Beneficiary / traveller name present",
    "destino_requerido": "Trip destination stated",
    "motivo_recomendado": "Trip purpose stated",
    "importe_dieta_razonable": "Daily allowance within the expected limit",
    "calculo_dietas": "Allowance total matches days × daily rate",
    "moneda_requerida": "Currency stated",
    "empleado_requerido": "Employee name present",
    "periodo_requerido": "Payroll period present",
    "bruto_requerido": "Gross salary present",
    "neto_requerido": "Net salary present",
    "calculo_neto": "Net salary matches gross minus deductions",
    "imputacion_valida": "Project allocation percentage is valid (0–100%)",
    "proveedor_requerido": "Supplier name present",
    "numero_factura_requerido": "Invoice number present",
    "descripcion_requerida": "Description present",
    "calculo_iva": "VAT amount matches the VAT rate",
    "tipo_iva_valido": "VAT rate is a valid Spanish rate",
    # Visual checks (stamps, signatures, attachments, eligibility)
    "xunta_galicia_stamp_present": "Xunta de Galicia stamp present",
    "grant_call_reference_present": "Grant call reference on the stamp",
    "expedient_number_present_stamp": "Expedient number on the stamp",
    "project_funding_percentage_indicated": "Project funding percentage indicated",
    "expense_description_sufficient": "Expense description is sufficient",
    "project_execution_within_2023": "Document dated within the 2023 project period",
    "translation_provided": "Translation provided for foreign-language documents",
    "payment_method_stated": "Payment method stated",
    "proof_of_payment_attached": "Proof of payment attached",
    "documentation_match": "Attached documents match the invoice",
    "procedure_code_mentioned": "Procedure code (PR811A) mentioned",
    "economy_class_flights_mandated": "Flights are economy class",
    "expatriate_housing_not_subsidized": "No expatriate housing costs",
    "external_transport_elements_limited": "Transport costs are of an eligible type",
    "protocol_expenses_not_subsidized": "No protocol / representation expenses",
    "debtor_interest_not_subsidized": "No debtor interest charged",
    "sanctions_fines_not_subsidized": "No sanctions or fines",
    "judicial_costs_not_subsidized": "No judicial costs",
    "dismissal_indemnities_not_subsidized": "No dismissal indemnities",
    "three_quotes_required_works": "Three quotes provided (works)",
    "three_quotes_required_supplies_services": "Three quotes provided (supplies / services)",
    "audit_cost_limit": "Audit cost within the 2,500 € limit",
    "evaluation_cost_limit": "Evaluation cost within the 5,000 € limit",
    "amortization_not_subsidized": "No amortization / depreciation charges",
}

# Why a rule failed, for non-visual rules whose config message is in Spanish.
_FAILED_TEXT_BY_CHECK = {
    "required": "Not found in the document",
    "required_one_of": "None of the expected values was found",
    "range": "Value is outside the expected range",
    "cross_field": "The numbers don't add up",
    "enum": "Value is not one of the allowed options",
    "regex": "Value doesn't have the expected format",
    "conditional_check": "Condition not satisfied",
}

_FINISH_EXPLANATIONS = (
    ("Run status reflects blocking", "There are blocking rule failures, or required checks are missing evidence."),
    ("No blocking error-severity failures", "No blocking problems, but some warning-level rules did not pass."),
    ("No rule blockers", "No blocking problems, but some extracted fields are flagged for human review."),
    ("All evaluated rules passed", "All checks passed and nothing is flagged."),
)


def humanize(identifier: str) -> str:
    """'compliance_passed' -> 'Compliance passed'. Text that already has spaces is left alone."""
    text = (identifier or "").strip()
    if not text or " " in text:
        return text
    spaced = text.replace("_", " ")
    return spaced[:1].upper() + spaced[1:]


def plural(n: int, singular: str, plural_form: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural_form or singular + 's')}"


def friendly_finish_reason(reason: str) -> str:
    return humanize(reason)


def friendly_finish_explanation(text: str) -> str:
    for prefix, friendly in _FINISH_EXPLANATIONS:
        if text.startswith(prefix):
            return friendly
    return text


class RuleCatalog:
    """rule_id -> readable label / check type, built from the loaded config store."""

    def __init__(self, rules: Iterable[Any] = ()) -> None:
        self._by_id: dict[str, Any] = {r.rule_id: r for r in rules}

    @classmethod
    def from_store(cls, store: Any) -> "RuleCatalog":
        by_type = getattr(store, "compliance_rules", {}) or {}
        return cls(rule for rules in by_type.values() for rule in rules)

    def label(self, rule_id: str) -> str:
        rule = self._by_id.get(rule_id)
        if rule is None:
            return rule_id
        return RULE_LABELS.get(rule.rule_name) or humanize(rule.rule_name)

    def check_type(self, rule_id: str) -> str:
        rule = self._by_id.get(rule_id)
        return getattr(rule, "check_type", "") if rule is not None else ""


def _skip_reason(message: str) -> str:
    """Turn the evaluator's technical skip message into something a reader can act on."""
    msg = (message or "").strip()
    if msg.startswith("Value missing/null"):
        return "No value was extracted for the field this rule needs"
    if msg.startswith("Cross-field check skipped (missing values):"):
        return "Missing values: " + msg.split(":", 1)[1].strip()
    if msg.startswith("Visual check"):
        return "The visual check has not been run yet"
    if msg.startswith("Condition not met"):
        return "Does not apply to this document"
    if msg.startswith("Unparseable numeric value") or msg.startswith("Range check skipped"):
        return "The extracted value could not be read as a number"
    return msg


def rule_row(
    catalog: RuleCatalog,
    rule_id: str,
    outcome: str,
    severity: str,
    message: str = "",
) -> dict[str, str]:
    """
    One rule as the page shows it. `outcome` is blocking | warning | not_checked.
    The rule id is kept in `rule_id` for a tooltip only — the label is what people read.
    """
    if outcome == "not_checked":
        text = _skip_reason(message)
        impact = "Blocks approval until checked" if severity == "error" else "Does not block approval"
        text = f"{text}. {impact}." if text else f"{impact}."
    elif catalog.check_type(rule_id) == "visual_check":
        text = (message or "").strip()  # the vision model's own (English) observation
    else:
        text = _FAILED_TEXT_BY_CHECK.get(catalog.check_type(rule_id), "Requirement not met")
    return {
        "rule_id": rule_id,
        "label": catalog.label(rule_id),
        "outcome": outcome,
        "severity": severity,
        "text": text,
    }


def _failed_rows(catalog: RuleCatalog, items: list[dict], outcome: str, severity: str) -> list[dict]:
    return [
        rule_row(catalog, str(i.get("rule_id", "")), outcome, severity, str(i.get("message", "")))
        for i in items
    ]


def summarize_compliance(result: dict, catalog: RuleCatalog) -> tuple[str, list[dict], list[dict]]:
    """check_compliance -> (headline, rule rows, extra detail lines)."""
    errors = result.get("failed_errors") or []
    warnings = result.get("failed_warnings") or []
    skipped = result.get("skipped_checks") or []
    pending = result.get("visual_checks_pending") or []

    parts = [
        f"{int(result.get('passed') or 0)} passed",
        plural(len(errors), "blocking error"),
        plural(len(warnings), "warning"),
    ]
    if skipped:
        parts.append(f"{len(skipped)} couldn't be checked")
    if pending:
        parts.append(plural(len(pending), "visual check") + " still to run")

    rows = _failed_rows(catalog, errors, "blocking", "error")
    rows += _failed_rows(catalog, warnings, "warning", "warning")
    rows += [
        rule_row(
            catalog, str(s.get("rule_id", "")), "not_checked",
            str(s.get("severity", "warning")), str(s.get("reason", "")),
        )
        for s in skipped
    ]
    return " · ".join(parts), rows, []


def summarize_visual(result: dict, catalog: RuleCatalog) -> tuple[str, list[dict], list[dict]]:
    """check_compliance_visual -> (headline, rule rows, extra detail lines)."""
    errors = result.get("failed_errors") or []
    warnings = result.get("failed_warnings") or []
    checked = int(result.get("visual_rules_checked") or 0)
    passed = int(result.get("passed") or 0)

    parts = [
        plural(checked, "visual rule") + " checked",
        f"{passed} passed",
        plural(len(errors), "blocking error"),
        plural(len(warnings), "warning"),
    ]
    inconclusive = checked - passed - len(errors) - len(warnings)
    if inconclusive > 0:
        parts.append(f"{inconclusive} inconclusive")

    rows = _failed_rows(catalog, errors, "blocking", "error")
    rows += _failed_rows(catalog, warnings, "warning", "warning")

    details: list[dict] = []
    if not result.get("success", True) and result.get("error"):
        details.append({"text": str(result["error"]), "level": "error"})
    backfilled = result.get("backfilled_fields") or []
    if backfilled:
        details.append({"text": f"Also filled in missing fields: {', '.join(str(b) for b in backfilled)}", "level": "info"})
    return " · ".join(parts), rows, details


def summarize_run(rule_results: list[Any], catalog: RuleCatalog) -> dict[str, Any]:
    """Final tallies for the banner, from the same rule results the agent decided on."""
    blocking, warnings, not_checked = [], [], []
    passed = 0
    for r in rule_results:
        if r.status == "passed":
            passed += 1
        elif r.status == "failed":
            outcome = "blocking" if r.severity == "error" else "warning"
            row = rule_row(catalog, r.rule_id, outcome, r.severity, r.message)
            (blocking if r.severity == "error" else warnings).append(row)
        else:
            not_checked.append(rule_row(catalog, r.rule_id, "not_checked", r.severity, r.message))
    return {"passed": passed, "blocking": blocking, "warnings": warnings, "not_checked": not_checked}


def finish_rows(catalog: RuleCatalog, result: dict) -> list[dict]:
    """Rows for the finish step. It only knows rule ids, so rows carry a label and no detail."""
    rows = []
    for rid in result.get("error_failures") or []:
        rows.append({**rule_row(catalog, str(rid), "blocking", "error"), "text": ""})
    for rid in result.get("warning_failures") or []:
        rows.append({**rule_row(catalog, str(rid), "warning", "warning"), "text": ""})
    return rows
