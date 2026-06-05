"""CSV output writers for agent runs."""

from __future__ import annotations

import csv
import time
from pathlib import Path

from src.agent.state import AgentState, rule_verdict_summary


def write_results(state: AgentState, output_dir: str) -> dict[str, str]:
    """Write fields, compliance, and rolling summary CSVs for a completed run."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")

    fields_path = out / f"results_{timestamp}.csv"
    compliance_path = out / f"compliance_{timestamp}.csv"
    summary_path = out / "summary.csv"

    field_columns = [
        "field_name",
        "field_id",
        "extracted_value",
        "confidence",
        "source_page",
        "source_region",
        "flagged_for_review",
        "review_reason",
    ]
    with open(fields_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=field_columns)
        writer.writeheader()
        for fr in state.extracted_fields.values():
            writer.writerow({
                "field_name": fr.field_name,
                "field_id": fr.field_id,
                "extracted_value": fr.extracted_value,
                "confidence": f"{fr.confidence:.2f}",
                "source_page": fr.source_page if fr.source_page is not None else "",
                "source_region": fr.source_region or "",
                "flagged_for_review": str(fr.flagged_for_review),
                "review_reason": fr.review_reason or "",
            })

    compliance_columns = [
        "rule_id",
        "rule_name",
        "field_id",
        "status",
        "severity",
        "message",
    ]
    with open(compliance_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=compliance_columns)
        writer.writeheader()
        for rr in state.rule_results:
            writer.writerow({
                "rule_id": rr.rule_id,
                "rule_name": rr.rule_name,
                "field_id": rr.field_id,
                "status": rr.status,
                "severity": rr.severity,
                "message": rr.message,
            })

    rv = rule_verdict_summary(state.rule_results)
    summary_columns = [
        "timestamp",
        "pdf",
        "invoice_type",
        "status",
        "turns",
        "fields_extracted",
        "rules_passed",
        "rules_failed_error",
        "rules_failed_warning",
        "finish_reason",
    ]
    summary_row = {
        "timestamp": timestamp,
        "pdf": Path(state.pdf_path).name,
        "invoice_type": state.invoice_type_id,
        "status": state.status.value,
        "turns": state.turn,
        "fields_extracted": len(state.extracted_fields),
        "rules_passed": rv["passed_count"],
        "rules_failed_error": len(rv["error_failed_rule_ids"]),
        "rules_failed_warning": len(rv["warning_failed_rule_ids"]),
        "finish_reason": state.finish_reason or "",
    }
    write_header = not summary_path.exists()
    with open(summary_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=summary_columns)
        if write_header:
            writer.writeheader()
        writer.writerow(summary_row)

    return {
        "fields_csv": str(fields_path),
        "compliance_csv": str(compliance_path),
        "summary_csv": str(summary_path),
    }
