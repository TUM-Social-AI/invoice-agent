from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from src.agent.state import AgentState
from src.output.canonical import (
    CANONICAL_STATUS_VALUES,
    COMPLIANCE_RESULT_COLUMNS,
    HIGH_VALUE_FIELD_NAMES,
    INVOICE_SUMMARY_COLUMNS,
    _stringify_cell,
    build_compliance_result_rows,
    build_invoice_summary_row,
)


INVOICE_SUMMARY_TABLE = "Invoice Summary"
TECHNICAL_RUN_DATA_TABLE = "Technical Run Data"
RAW_COMPLIANCE_RESULTS_TABLE = "Compliance Results"
COMPLIANCE_MATRIX_TABLE = "Compliance Matrix"
REVIEW_QUEUE_TABLE = "Review"
DASHBOARD_TABLE = "Dashboard"
RULE_GUIDE_TABLE = "Rule Guide"

# Backward-compatible alias for older imports/tests that referred to the raw summary tab.
RAW_INVOICE_SUMMARY_TABLE = TECHNICAL_RUN_DATA_TABLE

LEGACY_DASHBOARD_STATUS_COUNTS_TABLE = "Dashboard Status Counts"
LEGACY_DASHBOARD_RULE_COUNTS_TABLE = "Dashboard Rule Counts"
LEGACY_DASHBOARD_SEVERITY_COUNTS_TABLE = "Dashboard Severity Counts"

DASHBOARD_STATUS_COUNTS_TABLE = LEGACY_DASHBOARD_STATUS_COUNTS_TABLE
DASHBOARD_RULE_COUNTS_TABLE = LEGACY_DASHBOARD_RULE_COUNTS_TABLE
DASHBOARD_SEVERITY_COUNTS_TABLE = LEGACY_DASHBOARD_SEVERITY_COUNTS_TABLE

COMPLIANCE_MATRIX_COLUMNS = [
    "invoice_file",
    "invoice_type",
    "review_status",
]

INVOICE_SUMMARY_REVIEWER_COLUMNS = [
    "invoice_file",
    "invoice_type",
    "review_status",
    "vendor_name",
    "vendor_vat_id",
    "invoice_number",
    "invoice_date",
    "currency",
    "gross_amount",
    "net_amount",
    "tax_amount",
    "missing_critical_fields",
    "blocking_rules",
    "warning_rules",
]

REVIEW_QUEUE_COLUMNS = [
    "priority",
    "invoice_file",
    "invoice_type",
    "review_status",
    "blocking_rules",
    "warning_rules",
    "review_reasons",
    "recommended_action",
    "source_page",
    "evidence_refs",
    "policy_refs",
    "source_uri",
    "missing_critical_fields",
    "reviewer",
    "decision",
    "notes",
    "resolved_date",
]

DASHBOARD_COLUMNS = ["section", "metric", "value", "count"]
STATUS_COUNT_COLUMNS = [
    "passed",
    "failed",
    "warning",
    "flagged",
    "skipped",
    "unknown",
    "not_checked",
    "not_applicable",
]
RULE_GUIDE_COLUMNS = [
    "rule",
    "invoice_type",
    "severity",
    "field",
    "condition",
    "guidance",
    "failure_message",
    "passed",
    "failed",
    "warning",
    "flagged",
    "skipped",
    "unknown",
    "total_results",
    "failing_invoices",
    "warning_invoices",
    "reasoning",
]

_MATRIX_INVOICE_KEYS = ["invoice_id", "invoice_file", "invoice_type_id", "review_status"]
_COMPLIANCE_KEYS = [
    "invoice_id",
    "rule_id",
    "rule_name",
    "normalized_status",
    "severity",
    "message",
]
_REVIEW_INVOICE_KEYS = [
    "invoice_id",
    "invoice_file",
    "invoice_type_id",
    "review_status",
]
_ATTENTION_REVIEW_STATUSES = {"needs_review", "warning", "failed", "error", "unknown"}
_VALID_COMPLIANCE_STATUSES = CANONICAL_STATUS_VALUES - {"needs_review", "error"}
_MISSING_FIELD_LABELS = {
    "vendor_name": "Vendor name",
    "vendor_vat_id": "Vendor VAT ID",
    "invoice_number": "Invoice number",
    "invoice_date": "Invoice date",
    "currency": "Currency",
    "gross_amount": "Gross amount",
    "net_amount": "Net amount",
    "tax_amount": "Tax amount",
}


@dataclass(frozen=True)
class WorkbookTable:
    name: str
    headers: list[str]
    rows: list[dict[str, object]]


def _require_keys(row: dict[str, object], keys: Iterable[str], row_label: str) -> None:
    for key in keys:
        if key not in row:
            raise ValueError(f"{row_label} missing required key: {key}")


def _text(row: dict[str, object], key: str) -> str:
    return _stringify_cell(row.get(key, ""))


def _normalized_status(row: dict[str, object]) -> str:
    status = _text(row, "normalized_status").strip().lower()
    if status in _VALID_COMPLIANCE_STATUSES:
        return status
    return "unknown"


def _status_counts() -> dict[str, int]:
    return {status: 0 for status in STATUS_COUNT_COLUMNS}


def _display_text(value: str) -> str:
    text = (value or "").strip().replace("_", " ").replace("-", " ")
    text = " ".join(text.split())
    if not text:
        return ""
    return text[:1].upper() + text[1:]


def _condition(row: dict[str, object]) -> str:
    check_type = _display_text(_text(row, "check_type"))
    check_value = _text(row, "check_value")
    if check_type and check_value:
        return f"{check_type}: {check_value}"
    return check_type or check_value


def _metadata_row(rule: Any) -> dict[str, object]:
    if isinstance(rule, dict):
        return dict(rule)
    return {
        "rule_id": getattr(rule, "rule_id", ""),
        "invoice_type_id": getattr(rule, "invoice_type_id", ""),
        "rule_name": getattr(rule, "rule_name", ""),
        "field_id": getattr(rule, "field_id", ""),
        "check_type": getattr(rule, "check_type", ""),
        "check_value": getattr(rule, "check_value", ""),
        "severity": getattr(rule, "severity", ""),
        "agent_hint": getattr(rule, "agent_hint", ""),
        "error_message": getattr(rule, "error_message", ""),
    }


def _rule_metadata_lookup(rule_metadata: Iterable[Any] | None) -> dict[str, dict[str, object]]:
    lookup: dict[str, dict[str, object]] = {}
    for rule in rule_metadata or []:
        row = _metadata_row(rule)
        rule_id = _text(row, "rule_id")
        if rule_id:
            lookup[rule_id] = row
    return lookup


def _rule_sources(
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None,
) -> dict[str, dict[str, object]]:
    sources = _rule_metadata_lookup(rule_metadata)
    for row in compliance_rows:
        rule_id = _text(row, "rule_id")
        if rule_id and rule_id not in sources:
            sources[rule_id] = dict(row)
    return sources


def _rule_labels(
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> dict[str, str]:
    sources = _rule_sources(compliance_rows, rule_metadata)
    bases: dict[str, str] = {}
    grouped: dict[str, list[str]] = defaultdict(list)
    for rule_id, row in sources.items():
        base = _display_text(_text(row, "rule_name")) or _display_text(_text(row, "message")) or "Rule"
        bases[rule_id] = base
        grouped[base.lower()].append(rule_id)

    labels: dict[str, str] = {}
    for base_key, rule_ids in grouped.items():
        for rule_id in sorted(rule_ids):
            base = bases[rule_id]
            row = sources[rule_id]
            if len(rule_ids) == 1:
                labels[rule_id] = base
                continue
            context = _display_text(_text(row, "field_id")) or _condition(row)
            labels[rule_id] = f"{base} - {context}" if context else base
    return labels


def _join_rule_labels(rule_ids: Iterable[str], labels: dict[str, str]) -> str:
    return "; ".join(sorted({labels.get(rule_id, rule_id) for rule_id in rule_ids if rule_id}))


def _join_values(values: Iterable[str]) -> str:
    return "; ".join(sorted({value for value in values if value}))


def _missing_critical_fields(invoice: dict[str, object]) -> list[str]:
    return [
        _MISSING_FIELD_LABELS.get(field_name, _display_text(field_name))
        for field_name in HIGH_VALUE_FIELD_NAMES
        if not _text(invoice, field_name)
    ]


def _rows_by_invoice(compliance_rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in compliance_rows:
        _require_keys(row, _COMPLIANCE_KEYS, "compliance row")
        grouped[_text(row, "invoice_id")].append(row)
    return dict(grouped)


def _rule_invoice_type(rule_id: str, sources: dict[str, dict[str, object]]) -> str:
    return _text(sources.get(rule_id, {}), "invoice_type_id")


def _matrix_missing_status(
    invoice_type: str,
    rule_id: str,
    sources: dict[str, dict[str, object]],
) -> str:
    rule_invoice_type = _rule_invoice_type(rule_id, sources)
    if rule_invoice_type and invoice_type and rule_invoice_type != invoice_type:
        return "not_applicable"
    return "not_checked"


def _action_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        row
        for row in rows
        if _normalized_status(row) in {"failed", "flagged", "warning", "unknown"}
    ]


def _recommended_action(
    blocking_rules: list[str],
    warning_rules: list[str],
    unknown_rules: list[str],
    missing_fields: list[str],
    labels: dict[str, str],
) -> str:
    if blocking_rules:
        return f"Block approval until reviewed: confirm {_join_rule_labels(blocking_rules, labels)}."
    if warning_rules:
        return f"Check warning before approval: {_join_rule_labels(warning_rules, labels)}."
    if unknown_rules:
        return f"Review unchecked rule result: {_join_rule_labels(unknown_rules, labels)}."
    if missing_fields:
        return "Complete missing invoice fields."
    return "No action needed."


def build_compliance_matrix(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> WorkbookTable:
    for row in invoice_rows:
        _require_keys(row, _MATRIX_INVOICE_KEYS, "invoice row")
    for row in compliance_rows:
        _require_keys(row, ["invoice_id", "rule_id", "normalized_status"], "compliance row")

    labels = _rule_labels(compliance_rows, rule_metadata)
    sources = _rule_sources(compliance_rows, rule_metadata)
    rule_ids = sorted(
        {_text(row, "rule_id") for row in compliance_rows if _text(row, "rule_id")},
        key=lambda rule_id: labels.get(rule_id, rule_id).lower(),
    )
    cell_values: dict[tuple[str, str], str] = {}
    source_rows: dict[tuple[str, str], dict[str, object]] = {}
    for row in compliance_rows:
        invoice_id = _text(row, "invoice_id")
        rule_id = _text(row, "rule_id")
        if not invoice_id or not rule_id:
            continue
        key = (invoice_id, rule_id)
        status = _normalized_status(row)
        if key in source_rows and source_rows[key] != row:
            raise ValueError(f"duplicate compliance result for {invoice_id}/{rule_id}")
        source_rows[key] = row
        cell_values[key] = status

    matrix_rows: list[dict[str, object]] = []
    for invoice in sorted(invoice_rows, key=lambda row: _text(row, "invoice_id")):
        invoice_id = _text(invoice, "invoice_id")
        invoice_type = _text(invoice, "invoice_type_id")
        matrix_row: dict[str, object] = {
            "invoice_file": _text(invoice, "invoice_file"),
            "invoice_type": invoice_type,
            "review_status": _text(invoice, "review_status"),
        }
        for rule_id in rule_ids:
            matrix_row[labels.get(rule_id, rule_id)] = cell_values.get(
                (invoice_id, rule_id),
                _matrix_missing_status(invoice_type, rule_id, sources),
            )
        matrix_rows.append(matrix_row)

    return WorkbookTable(
        COMPLIANCE_MATRIX_TABLE,
        [*COMPLIANCE_MATRIX_COLUMNS, *[labels.get(rule_id, rule_id) for rule_id in rule_ids]],
        matrix_rows,
    )


def build_invoice_summary(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> WorkbookTable:
    labels = _rule_labels(compliance_rows, rule_metadata)
    grouped = _rows_by_invoice(compliance_rows)
    rows: list[dict[str, object]] = []

    for invoice in sorted(invoice_rows, key=lambda row: _text(row, "invoice_file")):
        _require_keys(invoice, _MATRIX_INVOICE_KEYS, "invoice row")
        invoice_id = _text(invoice, "invoice_id")
        related = grouped.get(invoice_id, [])
        blocking_rules = [
            _text(row, "rule_id")
            for row in related
            if _normalized_status(row) in {"failed", "flagged"}
        ]
        warning_rules = [
            _text(row, "rule_id")
            for row in related
            if _normalized_status(row) == "warning"
        ]
        summary = {
            "invoice_file": _text(invoice, "invoice_file"),
            "invoice_type": _text(invoice, "invoice_type_id"),
            "review_status": _text(invoice, "review_status"),
            "vendor_name": _text(invoice, "vendor_name"),
            "vendor_vat_id": _text(invoice, "vendor_vat_id"),
            "invoice_number": _text(invoice, "invoice_number"),
            "invoice_date": _text(invoice, "invoice_date"),
            "currency": _text(invoice, "currency"),
            "gross_amount": _text(invoice, "gross_amount"),
            "net_amount": _text(invoice, "net_amount"),
            "tax_amount": _text(invoice, "tax_amount"),
            "missing_critical_fields": "; ".join(_missing_critical_fields(invoice)),
            "blocking_rules": _join_rule_labels(blocking_rules, labels),
            "warning_rules": _join_rule_labels(warning_rules, labels),
        }
        rows.append(summary)

    return WorkbookTable(INVOICE_SUMMARY_TABLE, INVOICE_SUMMARY_REVIEWER_COLUMNS, rows)


def build_review_queue(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> WorkbookTable:
    grouped = _rows_by_invoice(compliance_rows)
    labels = _rule_labels(compliance_rows, rule_metadata)
    queue_rows: list[dict[str, object]] = []

    for invoice in invoice_rows:
        _require_keys(invoice, _REVIEW_INVOICE_KEYS, "invoice row")
        invoice_id = _text(invoice, "invoice_id")
        review_status = _text(invoice, "review_status").strip().lower() or "unknown"
        related = grouped.get(invoice_id, [])
        blocking_rules = [
            _text(row, "rule_id")
            for row in related
            if _normalized_status(row) in {"failed", "flagged"}
        ]
        warning_rules = [
            _text(row, "rule_id")
            for row in related
            if _normalized_status(row) == "warning"
        ]
        unknown_rules = [
            _text(row, "rule_id")
            for row in related
            if _normalized_status(row) == "unknown"
        ]
        missing_fields = _missing_critical_fields(invoice)
        needs_attention = (
            review_status in _ATTENTION_REVIEW_STATUSES
            or bool(blocking_rules)
            or bool(warning_rules)
            or bool(unknown_rules)
            or bool(missing_fields)
        )
        if not needs_attention:
            continue

        reasons = []
        if review_status != "passed":
            reasons.append(f"review_status={review_status}")
        for row in sorted(related, key=lambda item: (_text(item, "rule_id"), _text(item, "message"))):
            status = _normalized_status(row)
            if status in {"failed", "flagged", "warning", "unknown"}:
                message = _text(row, "message")
                rule_label = labels.get(_text(row, "rule_id"), _text(row, "rule_name"))
                if message:
                    reasons.append(f"{rule_label}: {message}")
                else:
                    reasons.append(f"{rule_label}: {status}")
        if missing_fields:
            reasons.append(f"Missing critical fields: {'; '.join(missing_fields)}")

        action_rows = _action_rows(related)
        priority = _review_priority(
            review_status,
            blocking_rules,
            warning_rules,
            unknown_rules,
            missing_fields,
        )
        queue_rows.append(
            {
                "priority": str(priority),
                "invoice_file": _text(invoice, "invoice_file"),
                "invoice_type": _text(invoice, "invoice_type_id"),
                "review_status": review_status,
                "blocking_rules": _join_rule_labels(blocking_rules, labels),
                "warning_rules": _join_rule_labels(warning_rules, labels),
                "review_reasons": "; ".join(reasons),
                "recommended_action": _recommended_action(
                    blocking_rules,
                    warning_rules,
                    unknown_rules,
                    missing_fields,
                    labels,
                ),
                "source_page": _join_values(_text(row, "source_page") for row in action_rows),
                "evidence_refs": _join_values(_text(row, "evidence_refs") for row in action_rows),
                "policy_refs": _join_values(_text(row, "policy_refs") for row in action_rows),
                "source_uri": _text(invoice, "source_uri"),
                "missing_critical_fields": "; ".join(missing_fields),
                "reviewer": "",
                "decision": "",
                "notes": "",
                "resolved_date": "",
            }
        )

    queue_rows.sort(
        key=lambda row: (
            int(_text(row, "priority") or "99"),
            _text(row, "invoice_file"),
            _text(row, "blocking_rules"),
            _text(row, "warning_rules"),
        )
    )
    return WorkbookTable(REVIEW_QUEUE_TABLE, REVIEW_QUEUE_COLUMNS, queue_rows)


def _review_priority(
    review_status: str,
    blocking_rules: list[str],
    warning_rules: list[str],
    unknown_rules: list[str],
    missing_fields: list[str] | None = None,
) -> int:
    if review_status in {"error", "failed"} or blocking_rules:
        return 1
    if review_status == "needs_review":
        return 1
    if review_status == "warning" or warning_rules:
        return 2
    if review_status == "unknown" or unknown_rules:
        return 3
    if missing_fields:
        return 3
    return 4


def build_dashboard(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> WorkbookTable:
    invoice_review_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    compliance_status_counts: Counter[str] = Counter()
    rule_counts: Counter[str] = Counter()
    missing_field_counts: Counter[str] = Counter()
    labels = _rule_labels(compliance_rows, rule_metadata)

    for invoice in invoice_rows:
        _require_keys(invoice, ["review_status", "invoice_type_id"], "invoice row")
        review_status = _text(invoice, "review_status") or "unknown"
        invoice_review_counts[review_status] += 1
        if review_status in {"needs_review", "failed", "error"}:
            action_counts["Blocked / needs review"] += 1
        elif review_status == "warning":
            action_counts["Warnings"] += 1
        elif review_status == "passed":
            action_counts["Passed"] += 1
        else:
            action_counts["Unknown"] += 1
        for field in _missing_critical_fields(invoice):
            missing_field_counts[field] += 1
    for row in compliance_rows:
        _require_keys(row, ["rule_id", "normalized_status"], "compliance row")
        status = _normalized_status(row)
        compliance_status_counts[status] += 1
        if status in {"failed", "flagged", "warning", "unknown"}:
            rule_counts[labels.get(_text(row, "rule_id"), _text(row, "rule_name"))] += 1

    rows = [
        {"section": "Action summary", "metric": "Invoices", "value": value, "count": str(count)}
        for value, count in sorted(action_counts.items())
    ]
    rows.extend(
        {"section": "Invoice review", "metric": "Review status", "value": value, "count": str(count)}
        for value, count in sorted(invoice_review_counts.items())
    )
    rows.extend(
        {"section": "Compliance status", "metric": "Rule results", "value": value, "count": str(count)}
        for value, count in sorted(compliance_status_counts.items())
    )
    rows.extend(
        {"section": "Missing critical fields", "metric": "Field", "value": value, "count": str(count)}
        for value, count in sorted(missing_field_counts.items())
    )
    rows.extend(
        {"section": "Rules needing attention", "metric": "Rule", "value": value, "count": str(count)}
        for value, count in sorted(rule_counts.items())
    )
    rows.extend(
        [
            {
                "section": "Legend",
                "metric": "Status",
                "value": "not_checked",
                "count": "",
            },
            {
                "section": "Legend",
                "metric": "Status",
                "value": "not_applicable",
                "count": "",
            },
        ]
    )
    return WorkbookTable(DASHBOARD_TABLE, DASHBOARD_COLUMNS, rows)


def build_rule_guide(
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> WorkbookTable:
    sources = _rule_sources(compliance_rows, rule_metadata)
    labels = _rule_labels(compliance_rows, rule_metadata)
    grouped: dict[str, dict[str, int]] = {}
    related_rows: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in compliance_rows:
        _require_keys(row, ["rule_id", "normalized_status"], "compliance row")
        rule_id = _text(row, "rule_id")
        counts = grouped.setdefault(rule_id, _status_counts())
        counts[_normalized_status(row)] += 1
        related_rows[rule_id].append(row)

    rows: list[dict[str, object]] = []
    for rule_id, row in sorted(
        sources.items(),
        key=lambda item: (labels.get(item[0], item[0]).lower(), _text(item[1], "invoice_type_id")),
    ):
        counts = grouped.setdefault(rule_id, _status_counts())
        total = sum(counts.values())
        failing_invoices = sorted(
            {
                _text(result, "invoice_file")
                for result in related_rows.get(rule_id, [])
                if _normalized_status(result) in {"failed", "flagged"}
            }
        )
        warning_invoices = sorted(
            {
                _text(result, "invoice_file")
                for result in related_rows.get(rule_id, [])
                if _normalized_status(result) == "warning"
            }
        )
        reasoning = []
        for result in sorted(
            related_rows.get(rule_id, []),
            key=lambda item: (_text(item, "invoice_file"), _normalized_status(item), _text(item, "message")),
        ):
            status = _normalized_status(result)
            if status not in {"failed", "flagged", "warning", "unknown"}:
                continue
            invoice_file = _text(result, "invoice_file") or "invoice"
            message = _text(result, "message") or status
            reasoning.append(f"{invoice_file}: {message}")
        rows.append(
            {
                "rule": labels.get(rule_id, _display_text(_text(row, "rule_name"))),
                "invoice_type": _text(row, "invoice_type_id"),
                "severity": _text(row, "severity"),
                "field": _text(row, "field_id"),
                "condition": _condition(row),
                "guidance": _text(row, "agent_hint"),
                "failure_message": _text(row, "error_message") or _text(row, "message"),
                **{status: str(counts[status]) for status in STATUS_COUNT_COLUMNS},
                "total_results": str(total),
                "failing_invoices": "; ".join(failing_invoices),
                "warning_invoices": "; ".join(warning_invoices),
                "reasoning": "; ".join(reasoning),
            }
        )
    return WorkbookTable(RULE_GUIDE_TABLE, RULE_GUIDE_COLUMNS, rows)


def build_workbook_tables(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
    rule_metadata: Iterable[Any] | None = None,
) -> list[WorkbookTable]:
    return [
        build_invoice_summary(invoice_rows, compliance_rows, rule_metadata),
        build_review_queue(invoice_rows, compliance_rows, rule_metadata),
        build_compliance_matrix(invoice_rows, compliance_rows, rule_metadata),
        build_dashboard(invoice_rows, compliance_rows, rule_metadata),
        build_rule_guide(compliance_rows, rule_metadata),
        WorkbookTable(RAW_COMPLIANCE_RESULTS_TABLE, COMPLIANCE_RESULT_COLUMNS, compliance_rows),
        WorkbookTable(TECHNICAL_RUN_DATA_TABLE, INVOICE_SUMMARY_COLUMNS, invoice_rows),
    ]


def build_workbook_from_states(
    states: Iterable[AgentState],
    rule_metadata: Iterable[Any] | None = None,
) -> list[WorkbookTable]:
    materialized_states = list(states)
    invoice_rows = [build_invoice_summary_row(state) for state in materialized_states]
    compliance_rows = [
        row
        for state in materialized_states
        for row in build_compliance_result_rows(state)
    ]
    return build_workbook_tables(invoice_rows, compliance_rows, rule_metadata)
