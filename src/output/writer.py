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

from src.agent.state import AgentState, rule_verdict_summary


def write_results(state: AgentState, output_dir: str) -> dict[str, str]:
    """Write fields, compliance, and rolling summary CSVs for a completed run."""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    fields_path = out / f"results_{timestamp}.csv"
    compliance_path = out / f"compliance_{timestamp}.csv"
    summary_path = out / "summary.csv"
    pdf_name = Path(state.pdf_path).name

    field_columns = [
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
    with fields_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=field_columns)
        writer.writeheader()
        for fr in state.extracted_fields.values():
            writer.writerow({
                "invoice_file": pdf_name,
                "invoice_type_id": state.invoice_type_id,
                "field_id": fr.field_id,
                "field_name": fr.field_name,
                "extracted_value": fr.extracted_value,
                "confidence": f"{fr.confidence:.2f}",
                "source_page": fr.source_page if fr.source_page is not None else "",
                "source_region": fr.source_region or "",
                "extraction_attempts": fr.extraction_attempts,
                "flagged_for_review": str(fr.flagged_for_review),
                "batch_review": str(getattr(fr, "batch_review", False)),
                "review_reason": fr.review_reason or "",
            })

    compliance_columns = [
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
    with compliance_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=compliance_columns)
        writer.writeheader()
        for rr in state.rule_results:
            writer.writerow({
                "invoice_file": pdf_name,
                "invoice_type_id": state.invoice_type_id,
                "rule_id": rr.rule_id,
                "rule_name": rr.rule_name,
                "field_id": rr.field_id,
                "status": rr.status,
                "severity": rr.severity,
                "message": rr.message,
                "agent_notes": rr.agent_notes or "",
            })

    rv = rule_verdict_summary(state.rule_results)
    summary_columns = [
        "timestamp",
        "invoice_file",
        "invoice_type_id",
        "agent_status",
        "turns_used",
        "fields_extracted",
        "fields_flagged",
        "rules_passed",
        "rules_failed_error",
        "rules_failed_warning",
        "finish_reason",
    ]
    summary_row = {
        "timestamp": timestamp,
        "invoice_file": pdf_name,
        "invoice_type_id": state.invoice_type_id,
        "agent_status": state.status.value,
        "turns_used": state.turn,
        "fields_extracted": len(state.extracted_fields),
        "fields_flagged": sum(1 for f in state.extracted_fields.values() if f.flagged_for_review),
        "rules_passed": rv["passed_count"],
        "rules_failed_error": len(rv["error_failed_rule_ids"]),
        "rules_failed_warning": len(rv["warning_failed_rule_ids"]),
        "finish_reason": state.finish_reason or "",
    }
    write_header = not summary_path.exists()
    with summary_path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_columns)
        if write_header:
            writer.writeheader()
        writer.writerow(summary_row)

    return {
        "fields_csv": str(fields_path),
        "compliance_csv": str(compliance_path),
        "summary_csv": str(summary_path),
    }
