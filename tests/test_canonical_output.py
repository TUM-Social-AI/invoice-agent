from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from src.agent.state import AgentState, AgentStatus, FieldResult, RuleResult
from src.output.canonical import (
    CANONICAL_SCHEMA_VERSION,
    CANONICAL_STATUS_VALUES,
    COMPLIANCE_RESULT_COLUMNS,
    INVOICE_SUMMARY_COLUMNS,
    build_compliance_result_rows,
    build_invoice_summary_row,
    normalize_agent_status,
    normalize_rule_status,
)
from src.output.canonical_csv import InMemoryWorkbookWriter, write_canonical_csvs
from src.output.writer import write_results
from src.sources.models import RunIdentity, SourceProvenance


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "output"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _source_provenance(
    *,
    source_type: str = "local",
    source_id: str = "local:/synthetic/invoice-alpha.pdf",
    source_uri: str = "file:///synthetic/invoice-alpha.pdf",
    source_hash: str = "hash-alpha",
    revision_id: str = "rev-alpha",
) -> SourceProvenance:
    timestamp = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    return SourceProvenance(
        source_type=source_type,
        source_id=source_id,
        source_uri=source_uri,
        display_name=Path(source_uri).name,
        original_filename=Path(source_uri).name,
        revision_id=revision_id,
        source_hash=source_hash,
        discovered_at_utc=timestamp,
        materialized_at_utc=timestamp,
        materialization_method="passthrough" if source_type == "local" else "download",
    )


def _run_identity(
    *,
    run_id: str = "run-alpha",
    safe_document_stem: str = "invoice-alpha",
    source_hash: str = "hash-alpha",
) -> RunIdentity:
    return RunIdentity(
        run_id=run_id,
        created_at_utc=datetime(2026, 1, 15, 12, 1, tzinfo=timezone.utc),
        safe_document_stem=safe_document_stem,
        source_hash=source_hash,
    )


def make_passed_state() -> AgentState:
    state = AgentState(
        pdf_path="/synthetic/invoice-alpha.pdf",
        output_dir="/tmp/canonical",
        invoice_type_id="EU_VAT",
        status=AgentStatus.PASSED,
        turn=6,
        finish_reason="compliance_passed",
        run_id="legacy-alpha",
        source_provenance=_source_provenance(),
        run_identity=_run_identity(),
    )
    state.extracted_fields = {
        "vendor_name": FieldResult(
            field_id="F_VENDOR",
            field_name="vendor_name",
            extracted_value="Example Supplies GmbH",
            confidence=0.98,
            source_page=1,
            source_region="header",
        ),
        "vendor_vat_id": FieldResult(
            field_id="F_VAT",
            field_name="vendor_vat_id",
            extracted_value="DE123456789",
            confidence=0.95,
            source_page=1,
            source_region="header",
        ),
        "invoice_number": FieldResult(
            field_id="F_NUMBER",
            field_name="invoice_number",
            extracted_value="INV-001",
            confidence=0.99,
            source_page=1,
            source_region="header",
        ),
        "currency": FieldResult(
            field_id="F_CURRENCY",
            field_name="currency",
            extracted_value="EUR",
            confidence=0.93,
            source_page=2,
            source_region="totals",
        ),
        "gross_amount": FieldResult(
            field_id="F_GROSS",
            field_name="gross_amount",
            extracted_value="119.00",
            confidence=0.94,
            source_page=2,
            source_region="totals",
        ),
    }
    state.rule_results = [
        RuleResult(
            rule_id="R_VAT_REQUIRED",
            rule_name="vendor_vat_required",
            field_id="F_VAT",
            status="passed",
            severity="error",
            message="VAT ID present",
        ),
        RuleResult(
            rule_id="R_TOTAL_PRESENT",
            rule_name="gross_total_present",
            field_id="F_GROSS",
            status="passed",
            severity="error",
            message="Gross amount present",
            agent_notes="Matched totals block",
        ),
    ]
    state.rule_evidence["R_TOTAL_PRESENT"] = {"refs": ["p2:totals"]}
    state.rule_policy_refs["R_TOTAL_PRESENT"] = [{"snippet_id": "POL-1", "source": "synthetic"}]
    return state


def make_warning_state() -> AgentState:
    state = AgentState(
        pdf_path="/synthetic/invoice-beta.pdf",
        output_dir="/tmp/canonical",
        invoice_type_id="EU_VAT",
        status=AgentStatus.PASSED,
        turn=4,
        finish_reason="warning_only",
        run_id="run-beta",
    )
    state.extracted_fields = {
        "vendor_name": FieldResult(
            field_id="F_VENDOR",
            field_name="vendor_name",
            extracted_value="Beta Parts SRL",
            confidence=0.91,
            source_page=1,
            source_region="header",
        ),
        "invoice_number": FieldResult(
            field_id="F_NUMBER",
            field_name="invoice_number",
            extracted_value="B-200",
            confidence=0.9,
            source_page=1,
            source_region="header",
        ),
        "tax_amount": FieldResult(
            field_id="F_TAX",
            field_name="tax_amount",
            extracted_value="18.20",
            confidence=0.87,
            source_page=2,
            source_region="tax_summary",
        ),
    }
    state.rule_results = [
        RuleResult(
            rule_id="R_VAT_RATE_WARN",
            rule_name="vat_rate_plausibility",
            field_id="F_TAX",
            status="failed",
            severity="warning",
            message="VAT rate differs from expected rate",
        )
    ]
    return state


def make_needs_review_state() -> AgentState:
    state = AgentState(
        pdf_path="/materialized/drive-invoice-gamma.pdf",
        output_dir="/tmp/canonical",
        invoice_type_id="SERVICES",
        status=AgentStatus.NEEDS_REVIEW,
        turn=9,
        finish_reason="field_flagged_for_review",
        source_provenance=_source_provenance(
            source_type="google_drive",
            source_id="drive-file-123",
            source_uri="gdrive://drive-file-123",
            source_hash="hash-gamma",
            revision_id="42",
        ),
        run_identity=_run_identity(
            run_id="run-gamma",
            safe_document_stem="drive-invoice-gamma",
            source_hash="hash-gamma",
        ),
    )
    state.extracted_fields = {
        "vendor_name": FieldResult(
            field_id="F_VENDOR",
            field_name="vendor_name",
            extracted_value="Gamma Consulting Ltd",
            confidence=0.82,
            source_page=1,
            source_region="header",
            batch_review=True,
        ),
        "net_amount": FieldResult(
            field_id="F_NET",
            field_name="net_amount",
            extracted_value="250.00",
            confidence=0.6,
            source_page=3,
            source_region="subtotal",
            flagged_for_review=True,
            review_reason="Low confidence subtotal",
        ),
    }
    state.rule_results = [
        RuleResult(
            rule_id="R_NET_REQUIRED",
            rule_name="net_amount_required",
            field_id="F_NET",
            status="flagged",
            severity="error",
            message="Net amount needs human confirmation",
            agent_notes="Subtotal and total are close together",
        ),
        RuleResult(
            rule_id="R_SERVICE_DATE",
            rule_name="service_date_present",
            field_id="F_SERVICE_DATE",
            status="skipped",
            severity="warning",
            message="No service date field configured for fixture",
        ),
    ]
    state.rule_evidence["R_NET_REQUIRED"] = {"refs": ["p3:subtotal", "p3:total"]}
    state.rule_policy_refs["R_NET_REQUIRED"] = [{"snippet_id": "POL-9", "source": "synthetic"}]
    return state


def canonical_states() -> list[AgentState]:
    return [make_passed_state(), make_warning_state(), make_needs_review_state()]


def test_invoice_summary_headers_are_deterministic() -> None:
    row = build_invoice_summary_row(make_passed_state())

    assert INVOICE_SUMMARY_COLUMNS[0] == "schema_version"
    assert list(row) == INVOICE_SUMMARY_COLUMNS
    assert row["schema_version"] == CANONICAL_SCHEMA_VERSION
    assert {
        "run_id",
        "invoice_id",
        "source_type",
        "source_id",
        "source_hash",
        "fields_extracted",
        "rules_failed_warning",
        "review_status",
    }.issubset(INVOICE_SUMMARY_COLUMNS)


def test_compliance_result_headers_are_deterministic() -> None:
    rows = build_compliance_result_rows(make_passed_state())

    assert COMPLIANCE_RESULT_COLUMNS[0] == "schema_version"
    assert len(rows) == 2
    assert all(list(row) == COMPLIANCE_RESULT_COLUMNS for row in rows)
    assert all(row["schema_version"] == CANONICAL_SCHEMA_VERSION for row in rows)


def test_status_normalization_uses_canonical_vocabulary() -> None:
    assert CANONICAL_STATUS_VALUES == {
        "passed",
        "failed",
        "warning",
        "skipped",
        "flagged",
        "needs_review",
        "error",
        "unknown",
    }
    assert normalize_rule_status("passed", "error") == "passed"
    assert normalize_rule_status("failed", "error") == "failed"
    assert normalize_rule_status("failed", "warning") == "warning"
    assert normalize_rule_status("skipped", "warning") == "skipped"
    assert normalize_rule_status("flagged", "error") == "flagged"
    assert normalize_rule_status("surprising", "error") == "unknown"
    assert normalize_agent_status(AgentStatus.PASSED) == "passed"
    assert normalize_agent_status(AgentStatus.NEEDS_REVIEW) == "needs_review"
    assert normalize_agent_status(AgentStatus.INTERRUPTED) == "error"
    assert normalize_agent_status("strange") == "unknown"


def test_invoice_summary_rows_match_expected_content() -> None:
    rows = [build_invoice_summary_row(state) for state in canonical_states()]

    assert rows[0]["invoice_id"] == "invoice-alpha"
    assert rows[0]["run_id"] == "run-alpha"
    assert rows[0]["review_status"] == "passed"
    assert rows[0]["rules_passed"] == "2"
    assert rows[0]["vendor_name"] == "Example Supplies GmbH"
    assert rows[1]["invoice_id"] == "invoice-beta"
    assert rows[1]["review_status"] == "warning"
    assert rows[1]["warning_failed_rule_ids"] == "R_VAT_RATE_WARN"
    assert rows[2]["source_type"] == "google_drive"
    assert rows[2]["review_status"] == "needs_review"
    assert rows[2]["fields_flagged"] == "1"


def test_compliance_result_rows_include_rule_identity_status_and_grounding() -> None:
    rows = build_compliance_result_rows(make_needs_review_state())

    assert [row["rule_id"] for row in rows] == ["R_NET_REQUIRED", "R_SERVICE_DATE"]
    assert rows[0]["invoice_id"] == "drive-invoice-gamma"
    assert rows[0]["status"] == "flagged"
    assert rows[0]["normalized_status"] == "flagged"
    assert rows[0]["severity"] == "error"
    assert rows[0]["source_page"] == "3"
    assert rows[0]["evidence_refs"] == "p3:subtotal; p3:total"
    assert "POL-9" in rows[0]["policy_refs"]
    assert rows[1]["normalized_status"] == "skipped"


def test_canonical_csvs_match_golden_fixtures(tmp_path: Path) -> None:
    paths = write_canonical_csvs(canonical_states(), tmp_path)

    assert set(paths) == {"invoice_summary_csv", "compliance_results_csv"}
    assert _read_csv(Path(paths["invoice_summary_csv"])) == _read_csv(
        FIXTURE_DIR / "canonical_invoice_summary.csv"
    )
    assert _read_csv(Path(paths["compliance_results_csv"])) == _read_csv(
        FIXTURE_DIR / "canonical_compliance_results.csv"
    )


def test_in_memory_workbook_writer_captures_stringified_sheets() -> None:
    summary_rows = [build_invoice_summary_row(make_passed_state())]
    compliance_rows = build_compliance_result_rows(make_passed_state())
    writer = InMemoryWorkbookWriter()

    writer.write_sheet("Invoice Summary", INVOICE_SUMMARY_COLUMNS, summary_rows)
    writer.write_sheet("Compliance Results", COMPLIANCE_RESULT_COLUMNS, compliance_rows)

    assert list(writer.sheets) == ["Invoice Summary", "Compliance Results"]
    assert writer.sheets["Invoice Summary"][0] == INVOICE_SUMMARY_COLUMNS
    assert writer.sheets["Invoice Summary"][1][0] == CANONICAL_SCHEMA_VERSION
    assert writer.sheets["Compliance Results"][0] == COMPLIANCE_RESULT_COLUMNS
    assert writer.sheets["Compliance Results"][1][
        COMPLIANCE_RESULT_COLUMNS.index("rule_id")
    ] == "R_VAT_REQUIRED"


def test_canonical_exports_are_additive_to_legacy_writer(tmp_path: Path) -> None:
    state = make_passed_state()
    before = state.model_dump()

    legacy_paths = write_results(state, tmp_path)
    canonical_paths = write_canonical_csvs([state], tmp_path)

    assert set(legacy_paths) == {"fields_csv", "compliance_csv", "summary_csv"}
    assert Path(legacy_paths["fields_csv"]).exists()
    assert Path(legacy_paths["compliance_csv"]).exists()
    assert Path(legacy_paths["summary_csv"]).exists()
    assert Path(canonical_paths["invoice_summary_csv"]).name == "invoice_summary.csv"
    assert Path(canonical_paths["compliance_results_csv"]).name == "compliance_results.csv"
    assert state.model_dump() == before
