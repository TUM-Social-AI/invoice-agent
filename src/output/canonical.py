from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.agent.state import AgentState, AgentStatus, rule_verdict_summary


CANONICAL_SCHEMA_VERSION = "1"
CANONICAL_STATUS_VALUES = {
    "passed",
    "failed",
    "warning",
    "skipped",
    "flagged",
    "needs_review",
    "error",
    "unknown",
}

HIGH_VALUE_FIELD_NAMES = [
    "vendor_name",
    "vendor_vat_id",
    "invoice_number",
    "invoice_date",
    "currency",
    "gross_amount",
    "net_amount",
    "tax_amount",
]

INVOICE_SUMMARY_COLUMNS = [
    "schema_version",
    "run_id",
    "invoice_id",
    "invoice_file",
    "pdf_path",
    "source_type",
    "source_id",
    "source_uri",
    "source_hash",
    "revision_id",
    "invoice_type_id",
    "agent_status",
    "review_status",
    "finish_reason",
    "turns_used",
    "fields_extracted",
    "fields_flagged",
    "rules_total",
    "rules_passed",
    "rules_failed_error",
    "rules_failed_warning",
    "error_failed_rule_ids",
    "warning_failed_rule_ids",
    *HIGH_VALUE_FIELD_NAMES,
]

COMPLIANCE_RESULT_COLUMNS = [
    "schema_version",
    "run_id",
    "invoice_id",
    "invoice_file",
    "source_type",
    "source_id",
    "source_hash",
    "invoice_type_id",
    "rule_id",
    "rule_name",
    "field_id",
    "status",
    "normalized_status",
    "severity",
    "message",
    "agent_notes",
    "source_page",
    "evidence_refs",
    "policy_refs",
]


def _stringify_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(json.dumps(item, sort_keys=True))
            else:
                parts.append(_stringify_cell(item))
        return "; ".join(parts)
    if isinstance(value, dict):
        if "refs" in value and isinstance(value["refs"], list):
            return _stringify_cell(value["refs"])
        return json.dumps(value, sort_keys=True)
    return str(value)


def _empty_row(columns: list[str]) -> dict[str, str]:
    return {column: "" for column in columns}


def _agent_status_value(status: AgentStatus | str | Any) -> str:
    if isinstance(status, AgentStatus):
        return status.value
    if isinstance(status, str):
        return status.strip().lower()
    return ""


def normalize_agent_status(status: AgentStatus | str | Any) -> str:
    value = _agent_status_value(status)
    if value == AgentStatus.INTERRUPTED.value:
        return "error"
    if value in CANONICAL_STATUS_VALUES:
        return value
    if value == AgentStatus.RUNNING.value:
        return "unknown"
    return "unknown"


def normalize_rule_status(status: str | Any, severity: str | Any = "") -> str:
    raw_status = str(status or "").strip().lower()
    raw_severity = str(severity or "").strip().lower()
    if raw_status == "failed" and raw_severity == "warning":
        return "warning"
    if raw_status in {"passed", "failed", "skipped", "flagged"}:
        return raw_status
    return "unknown"


def _identity(state: AgentState) -> dict[str, str]:
    provenance = state.source_provenance
    run_identity = state.run_identity
    pdf_path = Path(state.pdf_path)
    return {
        "run_id": _stringify_cell(run_identity.run_id if run_identity else state.run_id),
        "invoice_id": _stringify_cell(run_identity.safe_document_stem if run_identity else pdf_path.stem),
        "invoice_file": pdf_path.name,
        "source_type": _stringify_cell(provenance.source_type if provenance else ""),
        "source_id": _stringify_cell(provenance.source_id if provenance else ""),
        "source_uri": _stringify_cell(provenance.source_uri if provenance else ""),
        "source_hash": _stringify_cell(
            provenance.source_hash if provenance else (run_identity.source_hash if run_identity else "")
        ),
        "revision_id": _stringify_cell(provenance.revision_id if provenance else ""),
    }


def _derive_review_status(state: AgentState) -> str:
    verdict = rule_verdict_summary(state.rule_results)
    agent_status = normalize_agent_status(state.status)
    flagged_fields = [field for field in state.extracted_fields.values() if field.flagged_for_review]

    if agent_status == "error":
        return "error"
    if agent_status == "needs_review":
        return "needs_review"
    if flagged_fields or verdict["error_failed_rule_ids"]:
        return "needs_review"
    if verdict["warning_failed_rule_ids"]:
        return "warning"
    if agent_status == "passed" and len(verdict["error_failed_rule_ids"]) == 0:
        return "passed"
    if agent_status == "failed":
        return "failed"
    return "unknown"


def _field_by_name(state: AgentState, field_name: str) -> Any:
    if field_name in state.extracted_fields:
        return state.extracted_fields[field_name].extracted_value
    for result in state.extracted_fields.values():
        if result.field_name == field_name:
            return result.extracted_value
    return ""


def _source_page_for_rule(state: AgentState, field_id: str) -> str:
    for result in state.extracted_fields.values():
        if result.field_id == field_id:
            return _stringify_cell(result.source_page)
    return ""


def build_invoice_summary_row(state: AgentState) -> dict[str, str]:
    verdict = rule_verdict_summary(state.rule_results)
    flagged = [field for field in state.extracted_fields.values() if field.flagged_for_review]
    identity = _identity(state)

    row = _empty_row(INVOICE_SUMMARY_COLUMNS)
    row.update(
        {
            "schema_version": CANONICAL_SCHEMA_VERSION,
            "run_id": identity["run_id"],
            "invoice_id": identity["invoice_id"],
            "invoice_file": identity["invoice_file"],
            "pdf_path": _stringify_cell(state.pdf_path),
            "source_type": identity["source_type"],
            "source_id": identity["source_id"],
            "source_uri": identity["source_uri"],
            "source_hash": identity["source_hash"],
            "revision_id": identity["revision_id"],
            "invoice_type_id": _stringify_cell(state.invoice_type_id),
            "agent_status": normalize_agent_status(state.status),
            "review_status": _derive_review_status(state),
            "finish_reason": _stringify_cell(state.finish_reason),
            "turns_used": _stringify_cell(state.turn),
            "fields_extracted": _stringify_cell(len(state.extracted_fields)),
            "fields_flagged": _stringify_cell(len(flagged)),
            "rules_total": _stringify_cell(len(state.rule_results)),
            "rules_passed": _stringify_cell(verdict["passed_count"]),
            "rules_failed_error": _stringify_cell(len(verdict["error_failed_rule_ids"])),
            "rules_failed_warning": _stringify_cell(len(verdict["warning_failed_rule_ids"])),
            "error_failed_rule_ids": _stringify_cell(verdict["error_failed_rule_ids"]),
            "warning_failed_rule_ids": _stringify_cell(verdict["warning_failed_rule_ids"]),
        }
    )
    for field_name in HIGH_VALUE_FIELD_NAMES:
        row[field_name] = _stringify_cell(_field_by_name(state, field_name))
    return {column: row[column] for column in INVOICE_SUMMARY_COLUMNS}


def build_compliance_result_rows(state: AgentState) -> list[dict[str, str]]:
    identity = _identity(state)
    rows: list[dict[str, str]] = []
    for result in state.rule_results:
        row = _empty_row(COMPLIANCE_RESULT_COLUMNS)
        row.update(
            {
                "schema_version": CANONICAL_SCHEMA_VERSION,
                "run_id": identity["run_id"],
                "invoice_id": identity["invoice_id"],
                "invoice_file": identity["invoice_file"],
                "source_type": identity["source_type"],
                "source_id": identity["source_id"],
                "source_hash": identity["source_hash"],
                "invoice_type_id": _stringify_cell(state.invoice_type_id),
                "rule_id": _stringify_cell(result.rule_id),
                "rule_name": _stringify_cell(result.rule_name),
                "field_id": _stringify_cell(result.field_id),
                "status": _stringify_cell(result.status),
                "normalized_status": normalize_rule_status(result.status, result.severity),
                "severity": _stringify_cell(result.severity),
                "message": _stringify_cell(result.message),
                "agent_notes": _stringify_cell(result.agent_notes),
                "source_page": _source_page_for_rule(state, result.field_id),
                "evidence_refs": _stringify_cell(state.rule_evidence.get(result.rule_id, {}).get("refs", [])),
                "policy_refs": _stringify_cell(state.rule_policy_refs.get(result.rule_id, [])),
            }
        )
        rows.append({column: row[column] for column in COMPLIANCE_RESULT_COLUMNS})
    return rows
