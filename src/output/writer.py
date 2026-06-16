"""
Output writer — serialises AgentState to CSV files.

Two output files per run:
  results_YYYYMMDD_HHMMSS.csv  — one row per field extracted
  compliance_YYYYMMDD_HHMMSS.csv — one row per rule evaluated
"""

import csv
from datetime import datetime
from pathlib import Path

from src.agent.state import AgentState, AgentStatus


def write_results(state: AgentState, output_dir: str) -> dict[str, str]:
    # Include microseconds so rapid consecutive calls never overwrite each other
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # --- Extraction results ---
    fields_path = out / f"results_{timestamp}.csv"
    with open(fields_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
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
        ])
        pdf_name = Path(state.pdf_path).name
        for field_name, fr in state.extracted_fields.items():
            writer.writerow([
                pdf_name,
                state.invoice_type_id,
                fr.field_id,
                fr.field_name,
                fr.extracted_value,
                f"{fr.confidence:.2f}",
                fr.source_page,
                fr.source_region,
                fr.extraction_attempts,
                fr.flagged_for_review,
                getattr(fr, "batch_review", False),
                fr.review_reason or "",
            ])

    # --- Compliance results ---
    compliance_path = out / f"compliance_{timestamp}.csv"
    with open(compliance_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "invoice_file",
            "invoice_type_id",
            "rule_id",
            "rule_name",
            "field_id",
            "status",
            "severity",
            "message",
            "agent_notes",
        ])
        pdf_name = Path(state.pdf_path).name
        for rr in state.rule_results:
            writer.writerow([
                pdf_name,
                state.invoice_type_id,
                rr.rule_id,
                rr.rule_name,
                rr.field_id,
                rr.status,
                rr.severity,
                rr.message,
                rr.agent_notes or "",
            ])

    # --- Summary row appended to a rolling summary file ---
    summary_path = out / "summary.csv"
    write_header = not summary_path.exists()
    with open(summary_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "timestamp", "invoice_file", "invoice_type_id",
                "agent_status", "turns_used", "fields_extracted",
                "fields_flagged", "rules_passed", "rules_failed_error",
                "rules_failed_warning", "finish_reason",
            ])
        failed_errors = sum(1 for r in state.rule_results if r.status == "failed" and r.severity == "error")
        failed_warnings = sum(1 for r in state.rule_results if r.status == "failed" and r.severity == "warning")
        writer.writerow([
            timestamp,
            Path(state.pdf_path).name,
            state.invoice_type_id,
            state.status.value,
            state.turn,
            len(state.extracted_fields),
            sum(1 for f in state.extracted_fields.values() if f.flagged_for_review),
            len(state.passed_rules),
            failed_errors,
            failed_warnings,
            state.finish_reason or "",
        ])

    return {
        "fields_csv": str(fields_path),
        "compliance_csv": str(compliance_path),
        "summary_csv": str(summary_path),
    }
