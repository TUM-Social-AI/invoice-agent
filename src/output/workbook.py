from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable

from src.agent.state import AgentState
from src.output.canonical import (
    CANONICAL_STATUS_VALUES,
    COMPLIANCE_RESULT_COLUMNS,
    INVOICE_SUMMARY_COLUMNS,
    _stringify_cell,
    build_compliance_result_rows,
    build_invoice_summary_row,
)


RAW_INVOICE_SUMMARY_TABLE = "Invoice Summary"
RAW_COMPLIANCE_RESULTS_TABLE = "Compliance Results"
COMPLIANCE_MATRIX_TABLE = "Compliance Matrix"
REVIEW_QUEUE_TABLE = "Review Queue"
DASHBOARD_STATUS_COUNTS_TABLE = "Dashboard Status Counts"
DASHBOARD_RULE_COUNTS_TABLE = "Dashboard Rule Counts"
DASHBOARD_SEVERITY_COUNTS_TABLE = "Dashboard Severity Counts"

COMPLIANCE_MATRIX_COLUMNS = [
    "invoice_id",
    "invoice_file",
    "invoice_type_id",
    "review_status",
]

REVIEW_QUEUE_COLUMNS = [
    "priority",
    "invoice_id",
    "invoice_file",
    "source_type",
    "source_id",
    "source_hash",
    "invoice_type_id",
    "review_status",
    "blocking_rule_ids",
    "warning_rule_ids",
    "review_reasons",
]

DASHBOARD_STATUS_COUNTS_COLUMNS = ["metric", "value", "count"]
STATUS_COUNT_COLUMNS = ["passed", "failed", "warning", "flagged", "skipped", "unknown"]
DASHBOARD_RULE_COUNTS_COLUMNS = [
    "rule_id",
    "rule_name",
    "severity",
    *STATUS_COUNT_COLUMNS,
    "total",
]
DASHBOARD_SEVERITY_COUNTS_COLUMNS = ["severity", *STATUS_COUNT_COLUMNS, "total"]

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
    "source_type",
    "source_id",
    "source_hash",
    "invoice_type_id",
    "review_status",
]
_ATTENTION_REVIEW_STATUSES = {"needs_review", "warning", "failed", "error", "unknown"}
_VALID_COMPLIANCE_STATUSES = CANONICAL_STATUS_VALUES - {"needs_review", "error"}


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


def _join_rule_ids(rule_ids: Iterable[str]) -> str:
    return "; ".join(sorted({rule_id for rule_id in rule_ids if rule_id}))


def _rows_by_invoice(compliance_rows: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in compliance_rows:
        _require_keys(row, _COMPLIANCE_KEYS, "compliance row")
        grouped[_text(row, "invoice_id")].append(row)
    return dict(grouped)


def build_compliance_matrix(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
) -> WorkbookTable:
    for row in invoice_rows:
        _require_keys(row, _MATRIX_INVOICE_KEYS, "invoice row")
    for row in compliance_rows:
        _require_keys(row, ["invoice_id", "rule_id", "normalized_status"], "compliance row")

    rule_ids = sorted({_text(row, "rule_id") for row in compliance_rows if _text(row, "rule_id")})
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
        matrix_row: dict[str, object] = {
            column: _text(invoice, column) for column in COMPLIANCE_MATRIX_COLUMNS
        }
        for rule_id in rule_ids:
            matrix_row[rule_id] = cell_values.get((invoice_id, rule_id), "")
        matrix_rows.append(matrix_row)

    return WorkbookTable(
        COMPLIANCE_MATRIX_TABLE,
        [*COMPLIANCE_MATRIX_COLUMNS, *rule_ids],
        matrix_rows,
    )


def build_review_queue(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
) -> WorkbookTable:
    grouped = _rows_by_invoice(compliance_rows)
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
        needs_attention = (
            review_status in _ATTENTION_REVIEW_STATUSES
            or bool(blocking_rules)
            or bool(warning_rules)
            or bool(unknown_rules)
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
                if message:
                    reasons.append(f"{_text(row, 'rule_id')}: {message}")
                else:
                    reasons.append(f"{_text(row, 'rule_id')}: {status}")

        priority = _review_priority(review_status, blocking_rules, warning_rules, unknown_rules)
        queue_rows.append(
            {
                "priority": str(priority),
                "invoice_id": invoice_id,
                "invoice_file": _text(invoice, "invoice_file"),
                "source_type": _text(invoice, "source_type"),
                "source_id": _text(invoice, "source_id"),
                "source_hash": _text(invoice, "source_hash"),
                "invoice_type_id": _text(invoice, "invoice_type_id"),
                "review_status": review_status,
                "blocking_rule_ids": _join_rule_ids(blocking_rules),
                "warning_rule_ids": _join_rule_ids(warning_rules),
                "review_reasons": "; ".join(reasons),
            }
        )

    queue_rows.sort(
        key=lambda row: (
            int(_text(row, "priority") or "99"),
            _text(row, "invoice_id"),
            _text(row, "blocking_rule_ids"),
            _text(row, "warning_rule_ids"),
        )
    )
    return WorkbookTable(REVIEW_QUEUE_TABLE, REVIEW_QUEUE_COLUMNS, queue_rows)


def _review_priority(
    review_status: str,
    blocking_rules: list[str],
    warning_rules: list[str],
    unknown_rules: list[str],
) -> int:
    if review_status in {"error", "failed"} or blocking_rules:
        return 1
    if review_status == "needs_review":
        return 1
    if review_status == "warning" or warning_rules:
        return 2
    if review_status == "unknown" or unknown_rules:
        return 3
    return 4


def build_dashboard_status_counts(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
) -> WorkbookTable:
    invoice_review_counts: Counter[str] = Counter()
    invoice_type_counts: Counter[str] = Counter()
    compliance_status_counts: Counter[str] = Counter()

    for invoice in invoice_rows:
        _require_keys(invoice, ["review_status", "invoice_type_id"], "invoice row")
        invoice_review_counts[_text(invoice, "review_status") or "unknown"] += 1
        invoice_type_counts[_text(invoice, "invoice_type_id") or "unknown"] += 1
    for row in compliance_rows:
        _require_keys(row, ["normalized_status"], "compliance row")
        compliance_status_counts[_normalized_status(row)] += 1

    rows = [
        {"metric": "compliance_status", "value": value, "count": str(count)}
        for value, count in sorted(compliance_status_counts.items())
    ]
    rows.extend(
        {"metric": "invoice_review_status", "value": value, "count": str(count)}
        for value, count in sorted(invoice_review_counts.items())
    )
    rows.extend(
        {"metric": "invoice_type", "value": value, "count": str(count)}
        for value, count in sorted(invoice_type_counts.items())
    )
    return WorkbookTable(DASHBOARD_STATUS_COUNTS_TABLE, DASHBOARD_STATUS_COUNTS_COLUMNS, rows)


def build_dashboard_rule_counts(compliance_rows: list[dict[str, object]]) -> WorkbookTable:
    grouped: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in compliance_rows:
        _require_keys(row, ["rule_id", "rule_name", "severity", "normalized_status"], "compliance row")
        key = (_text(row, "rule_id"), _text(row, "rule_name"), _text(row, "severity"))
        counts = grouped.setdefault(key, _status_counts())
        counts[_normalized_status(row)] += 1

    rows: list[dict[str, object]] = []
    for (rule_id, rule_name, severity), counts in sorted(grouped.items()):
        total = sum(counts.values())
        rows.append(
            {
                "rule_id": rule_id,
                "rule_name": rule_name,
                "severity": severity,
                **{status: str(counts[status]) for status in STATUS_COUNT_COLUMNS},
                "total": str(total),
            }
        )
    return WorkbookTable(DASHBOARD_RULE_COUNTS_TABLE, DASHBOARD_RULE_COUNTS_COLUMNS, rows)


def build_dashboard_severity_counts(compliance_rows: list[dict[str, object]]) -> WorkbookTable:
    grouped: dict[str, dict[str, int]] = {}
    for row in compliance_rows:
        _require_keys(row, ["severity", "normalized_status"], "compliance row")
        severity = _text(row, "severity") or "unknown"
        counts = grouped.setdefault(severity, _status_counts())
        counts[_normalized_status(row)] += 1

    rows: list[dict[str, object]] = []
    for severity, counts in sorted(grouped.items()):
        total = sum(counts.values())
        rows.append(
            {
                "severity": severity,
                **{status: str(counts[status]) for status in STATUS_COUNT_COLUMNS},
                "total": str(total),
            }
        )
    return WorkbookTable(DASHBOARD_SEVERITY_COUNTS_TABLE, DASHBOARD_SEVERITY_COUNTS_COLUMNS, rows)


def build_workbook_tables(
    invoice_rows: list[dict[str, object]],
    compliance_rows: list[dict[str, object]],
) -> list[WorkbookTable]:
    return [
        WorkbookTable(RAW_INVOICE_SUMMARY_TABLE, INVOICE_SUMMARY_COLUMNS, invoice_rows),
        WorkbookTable(RAW_COMPLIANCE_RESULTS_TABLE, COMPLIANCE_RESULT_COLUMNS, compliance_rows),
        build_compliance_matrix(invoice_rows, compliance_rows),
        build_review_queue(invoice_rows, compliance_rows),
        build_dashboard_status_counts(invoice_rows, compliance_rows),
        build_dashboard_rule_counts(compliance_rows),
        build_dashboard_severity_counts(compliance_rows),
    ]


def build_workbook_from_states(states: Iterable[AgentState]) -> list[WorkbookTable]:
    materialized_states = list(states)
    invoice_rows = [build_invoice_summary_row(state) for state in materialized_states]
    compliance_rows = [
        row
        for state in materialized_states
        for row in build_compliance_result_rows(state)
    ]
    return build_workbook_tables(invoice_rows, compliance_rows)
