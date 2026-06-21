"""
Output writer — serialises AgentState to CSV files.

Two output files per run:
  results_YYYYMMDD_HHMMSS_microseconds.csv — one row per field extracted
  compliance_YYYYMMDD_HHMMSS_microseconds.csv — one row per rule evaluated
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any

from src.agent.state import AgentState, rule_verdict_summary


FIELD_COLUMNS = [
    "invoice_file",
    "invoice_type_id",
    "field_id",
    "field_name",
    "extracted_value",
    "confidence",
    "source_page",
    "source_region",
    "extraction_attempts",
    "flagged_for_review",
    "batch_review",
    "review_reason",
]

COMPLIANCE_COLUMNS = [
    "invoice_file",
    "invoice_type_id",
    "rule_id",
    "rule_name",
    "field_id",
    "status",
    "severity",
    "message",
    "agent_notes",
]

SUMMARY_COLUMNS = [
    "timestamp",
    "invoice_file",
    "pdf_path",
    "invoice_type_id",
    "agent_status",
    "turns_used",
    "finish_reason",
    "fields_extracted",
    "fields_flagged",
    "rules_total",
    "rules_passed",
    "rules_failed_error",
    "rules_failed_warning",
    "source_type",
    "source_id",
    "source_hash",
    "run_id",
]


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _write_dict_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _stringify(row.get(k, "")) for k in fieldnames})


def _append_summary(path: Path, row: dict[str, Any]) -> None:
    exists = path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SUMMARY_COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerow({k: _stringify(row.get(k, "")) for k in SUMMARY_COLUMNS})


def write_results(state: AgentState, output_dir: str | Path) -> dict[str, str]:
    """Write fields, compliance, and rolling summary CSVs for a completed run."""

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    ts = _timestamp()
    pdf_name = Path(state.pdf_path).name

    fields_path = out / f"results_{ts}.csv"
    compliance_path = out / f"compliance_{ts}.csv"
    summary_path = out / "summary.csv"

    field_rows = [
        {
            "invoice_file": pdf_name,
            "invoice_type_id": state.invoice_type_id,
            "field_id": result.field_id,
            "field_name": result.field_name,
            "extracted_value": result.extracted_value,
            "confidence": f"{result.confidence:.2f}",
            "source_page": result.source_page,
            "source_region": result.source_region,
            "extraction_attempts": result.extraction_attempts,
            "flagged_for_review": result.flagged_for_review,
            "batch_review": getattr(result, "batch_review", False),
            "review_reason": result.review_reason,
        }
        for result in state.extracted_fields.values()
    ]
    _write_dict_rows(fields_path, FIELD_COLUMNS, field_rows)

    compliance_rows = [
        {
            "invoice_file": pdf_name,
            "invoice_type_id": state.invoice_type_id,
            "rule_id": result.rule_id,
            "rule_name": result.rule_name,
            "field_id": result.field_id,
            "status": result.status,
            "severity": result.severity,
            "message": result.message,
            "agent_notes": result.agent_notes,
        }
        for result in state.rule_results
    ]
    _write_dict_rows(compliance_path, COMPLIANCE_COLUMNS, compliance_rows)

    verdict = rule_verdict_summary(state.rule_results)
    flagged = [f for f in state.extracted_fields.values() if f.flagged_for_review]
    provenance = state.source_provenance
    run_identity = state.run_identity
    _append_summary(
        summary_path,
        {
            "timestamp": ts,
            "invoice_file": pdf_name,
            "pdf_path": state.pdf_path,
            "invoice_type_id": state.invoice_type_id,
            "agent_status": state.status.value,
            "turns_used": state.turn,
            "finish_reason": state.finish_reason,
            "fields_extracted": len(state.extracted_fields),
            "fields_flagged": len(flagged),
            "rules_total": len(state.rule_results),
            "rules_passed": verdict["passed_count"],
            "rules_failed_error": len(verdict["error_failed_rule_ids"]),
            "rules_failed_warning": len(verdict["warning_failed_rule_ids"]),
            "source_type": provenance.source_type if provenance else "",
            "source_id": provenance.source_id if provenance else "",
            "source_hash": provenance.source_hash if provenance else "",
            "run_id": run_identity.run_id if run_identity else state.run_id,
        },
    )

    return {
        "fields_csv": str(fields_path),
        "compliance_csv": str(compliance_path),
        "summary_csv": str(summary_path),
    }
