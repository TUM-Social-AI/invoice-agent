from __future__ import annotations

from pathlib import Path

import pytest

from src.agent.state import AgentState, AgentStatus, FieldResult, RuleResult
from src.output.canonical import (
    COMPLIANCE_RESULT_COLUMNS,
    INVOICE_SUMMARY_COLUMNS,
    build_compliance_result_rows,
    build_invoice_summary_row,
)
from src.output.canonical_csv import (
    InMemoryWorkbookWriter,
    write_canonical_workbook_csvs,
    write_workbook_csvs,
)
from src.output.workbook import (
    COMPLIANCE_MATRIX_TABLE,
    INVOICE_SUMMARY_REVIEWER_COLUMNS,
    INVOICE_SUMMARY_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    REVIEW_ISSUES_COLUMNS,
    REVIEW_ISSUES_TABLE,
    REVIEW_QUEUE_COLUMNS,
    REVIEW_QUEUE_TABLE,
    RULE_GUIDE_COLUMNS,
    RULE_GUIDE_TABLE,
    TECHNICAL_RUN_DATA_TABLE,
    WorkbookTable,
    build_compliance_matrix,
    build_workbook_from_states,
    build_workbook_tables,
)
from tests.test_canonical_output import FIXTURE_DIR, _read_csv, canonical_states


def _canonical_rows() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    states = canonical_states()
    return (
        [build_invoice_summary_row(state) for state in states],
        [
            row
            for state in states
            for row in build_compliance_result_rows(state)
        ],
    )


def _tables_by_name() -> dict[str, WorkbookTable]:
    return {table.name: table for table in build_workbook_from_states(canonical_states())}


def test_compliance_matrix_uses_reviewer_rule_labels() -> None:
    invoice_rows, compliance_rows = _canonical_rows()

    table = build_compliance_matrix(invoice_rows, compliance_rows)

    assert table.name == COMPLIANCE_MATRIX_TABLE
    assert table.headers == [
        "invoice_file",
        "invoice_type",
        "review_status",
        "Gross total present",
        "Net amount required",
        "Service date present",
        "Vat rate plausibility",
        "Vendor vat required",
    ]
    assert "R_NET_REQUIRED" not in table.headers
    assert table.rows[0]["Net amount required"] == "flagged"
    assert table.rows[0]["Vendor vat required"] == "not_applicable"
    assert table.rows[1]["Vat rate plausibility"] == "not_checked"
    assert table.rows[2]["Gross total present"] == "not_checked"


def test_compliance_matrix_rejects_ambiguous_duplicate_cells() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    duplicate = dict(compliance_rows[0])
    duplicate["normalized_status"] = "failed"

    with pytest.raises(ValueError, match="duplicate compliance result for invoice-alpha/R_VAT_REQUIRED"):
        build_compliance_matrix(invoice_rows, [*compliance_rows, duplicate])


def test_visible_reviewer_tabs_do_not_expose_rule_ids() -> None:
    tables = _tables_by_name()

    visible_tables = [
        tables[INVOICE_SUMMARY_TABLE],
        tables[REVIEW_QUEUE_TABLE],
        tables[REVIEW_ISSUES_TABLE],
        tables[COMPLIANCE_MATRIX_TABLE],
        tables[RULE_GUIDE_TABLE],
    ]
    for table in visible_tables:
        assert "rule_id" not in table.headers
        assert all("R_" not in str(value) for row in table.rows for value in row.values())

    raw = tables[RAW_COMPLIANCE_RESULTS_TABLE]
    assert "rule_id" in raw.headers
    assert any(row["rule_id"] == "R_NET_REQUIRED" for row in raw.rows)


def test_invoice_summary_is_user_facing_and_technical_data_is_hidden_table() -> None:
    tables = _tables_by_name()

    summary = tables[INVOICE_SUMMARY_TABLE]
    assert summary.headers == INVOICE_SUMMARY_REVIEWER_COLUMNS
    assert "turns_used" not in summary.headers
    assert "source_hash" not in summary.headers
    assert summary.rows[0]["blocking_rules"] == "Net amount required"
    assert summary.rows[0]["primary_action"] == (
        "Block approval until reviewed: confirm Net amount required; complete missing fields: "
        "Vendor VAT ID; Invoice number; Invoice date; Currency; Gross amount; Tax amount."
    )
    assert summary.rows[0]["missing_critical_fields"] == (
        "Vendor VAT ID; Invoice number; Invoice date; Currency; Gross amount; Tax amount"
    )

    technical = tables[TECHNICAL_RUN_DATA_TABLE]
    assert technical.headers == INVOICE_SUMMARY_COLUMNS
    assert "turns_used" in technical.headers
    assert "source_hash" in technical.headers


def test_review_queue_uses_readable_rule_reasons() -> None:
    tables = _tables_by_name()
    queue = tables[REVIEW_QUEUE_TABLE]

    assert queue.headers == REVIEW_QUEUE_COLUMNS
    assert [row["invoice_file"] for row in queue.rows] == [
        "drive-invoice-gamma.pdf",
        "invoice-beta.pdf",
        "invoice-alpha.pdf",
    ]
    assert queue.rows[0]["blocking_rules"] == "Net amount required"
    assert "R_NET_REQUIRED" not in queue.rows[0]["review_reasons"]
    assert queue.rows[0]["recommended_action"] == (
        "Block approval until reviewed: confirm Net amount required; complete missing fields: "
        "Vendor VAT ID; Invoice number; Invoice date; Currency; Gross amount; Tax amount."
    )
    assert queue.rows[0]["source_page"] == "3"
    assert queue.rows[0]["evidence_refs"] == "p3:subtotal; p3:total"
    assert queue.rows[0]["policy_summary"] == "POL-9 - synthetic"
    assert queue.rows[0]["source_url"] == "https://drive.google.com/file/d/drive-file-123/view"
    assert queue.rows[0]["source_uri"] == "gdrive://drive-file-123"
    assert queue.rows[0]["reviewer"] == ""
    assert queue.rows[0]["decision"] == ""
    assert queue.rows[2]["recommended_action"] == (
        "Complete missing invoice fields: Invoice date; Net amount; Tax amount."
    )
    assert queue.rows[2]["missing_critical_fields"] == "Invoice date; Net amount; Tax amount"


def test_review_issues_splits_invoice_queue_into_actionable_rows() -> None:
    tables = _tables_by_name()
    issues = tables[REVIEW_ISSUES_TABLE]

    assert issues.headers == REVIEW_ISSUES_COLUMNS
    assert "rule_id" not in issues.headers
    assert issues.rows[0]["issue_type"] == "blocking_rule"
    assert issues.rows[0]["rule"] == "Net amount required"
    assert issues.rows[0]["recommended_action"] == (
        "Block approval until reviewed: confirm Net amount required."
    )
    assert issues.rows[0]["policy_summary"] == "POL-9 - synthetic"
    assert issues.rows[0]["source_url"] == "https://drive.google.com/file/d/drive-file-123/view"
    assert issues.rows[0]["source_uri"] == "gdrive://drive-file-123"
    assert any(
        row["issue_type"] == "missing_critical_field"
        and row["invoice_file"] == "drive-invoice-gamma.pdf"
        and row["field"] == "Currency"
        for row in issues.rows
    )


def test_review_issues_include_internal_contradictions_from_existing_rows() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update(
        {
            "invoice_id": "contradiction-invoice",
            "invoice_file": "contradiction.pdf",
            "invoice_type_id": "EU_VAT",
            "currency": "EUR",
        }
    )
    failed_currency_rule = dict(compliance_rows[0])
    failed_currency_rule.update(
        {
            "invoice_id": "contradiction-invoice",
            "invoice_file": "contradiction.pdf",
            "rule_id": "R_CURRENCY_REQUIRED",
            "rule_name": "currency_required",
            "normalized_status": "warning",
            "severity": "warning",
            "message": "Currency not identified",
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_currency_rule])
    }
    issues = tables[REVIEW_ISSUES_TABLE]

    assert any(
        row["issue_type"] == "internal_contradiction"
        and row["issue"] == "Currency value exists but Currency required still needs review"
        for row in issues.rows
    )


def test_review_issues_do_not_contradict_unrelated_amount_fields() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update(
        {
            "invoice_id": "gross-only-invoice",
            "invoice_file": "gross-only.pdf",
            "invoice_type_id": "EU_VAT",
            "gross_amount": "119.00",
            "net_amount": "",
            "tax_amount": "",
        }
    )
    failed_net_rule = dict(compliance_rows[0])
    failed_net_rule.update(
        {
            "invoice_id": "gross-only-invoice",
            "invoice_file": "gross-only.pdf",
            "rule_id": "R_NET_REQUIRED",
            "rule_name": "net_amount_required",
            "normalized_status": "failed",
            "severity": "error",
            "message": "Net amount not identified",
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_net_rule])
    }
    issues = tables[REVIEW_ISSUES_TABLE]

    assert not any(row["issue_type"] == "internal_contradiction" for row in issues.rows)


def test_review_issues_contradict_matching_total_amount_fields() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update(
        {
            "invoice_id": "gross-total-invoice",
            "invoice_file": "gross-total.pdf",
            "invoice_type_id": "EU_VAT",
            "gross_amount": "119.00",
            "net_amount": "",
            "tax_amount": "",
        }
    )
    failed_total_rule = dict(compliance_rows[0])
    failed_total_rule.update(
        {
            "invoice_id": "gross-total-invoice",
            "invoice_file": "gross-total.pdf",
            "rule_id": "R_GROSS_TOTAL_REQUIRED",
            "rule_name": "gross_total_required",
            "normalized_status": "failed",
            "severity": "error",
            "message": "Total amount not identified",
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_total_rule])
    }
    issues = tables[REVIEW_ISSUES_TABLE]

    assert any(
        row["issue_type"] == "internal_contradiction"
        and row["issue"] == "Amount value exists but Gross total required still needs review"
        for row in issues.rows
    )


def test_dashboard_counts_passed_invoice_with_contradiction_as_blocked() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update(
        {
            "invoice_id": "passed-with-contradiction",
            "invoice_file": "passed-with-contradiction.pdf",
            "invoice_type_id": "EU_VAT",
            "review_status": "passed",
            "currency": "EUR",
        }
    )
    failed_currency_rule = dict(compliance_rows[0])
    failed_currency_rule.update(
        {
            "invoice_id": "passed-with-contradiction",
            "invoice_file": "passed-with-contradiction.pdf",
            "rule_id": "R_CURRENCY_REQUIRED",
            "rule_name": "currency_required",
            "normalized_status": "warning",
            "severity": "warning",
            "message": "Currency not identified",
        }
    )

    dashboard = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_currency_rule])
    }["Dashboard"]
    action_rows = {
        row["value"]: row["count"]
        for row in dashboard.rows
        if row["section"] == "Action summary"
    }

    assert action_rows["Blocked / needs review"] == "1"
    assert "Passed" not in action_rows


def test_build_workbook_from_states_detects_total_amount_contradiction_without_raw_schema_change() -> None:
    state = AgentState(
        pdf_path="/synthetic/total-contradiction.pdf",
        output_dir="/tmp/canonical",
        invoice_type_id="VIAJES",
        status=AgentStatus.PASSED,
    )
    state.extracted_fields["total_amount"] = FieldResult(
        field_id="VIA_011",
        field_name="total_amount",
        extracted_value="384560.00",
        confidence=0.92,
        source_page=10,
        source_region="totals",
    )
    state.rule_results = [
        RuleResult(
            rule_id="R_VIA_002",
            rule_name="importe_total_requerido",
            field_id="VIA_011",
            status="failed",
            severity="error",
            message="Falta el importe total del gasto de viaje. Campo obligatorio.",
        )
    ]

    tables = {table.name: table for table in build_workbook_from_states([state])}
    issues = tables[REVIEW_ISSUES_TABLE]
    technical = tables[TECHNICAL_RUN_DATA_TABLE]

    assert "total_amount" not in technical.headers
    assert any(
        row["issue_type"] == "internal_contradiction"
        and row["issue"] == "Amount value exists but Importe total requerido still needs review"
        for row in issues.rows
    )


def test_viajes_procurement_contradiction_ignores_rule_boilerplate_travel_words() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update(
        {
            "invoice_id": "travel-procurement",
            "invoice_file": "travel-procurement.pdf",
            "invoice_type_id": "VIAJES",
        }
    )
    procurement_rule = dict(compliance_rows[0])
    procurement_rule.update(
        {
            "invoice_id": "travel-procurement",
            "invoice_file": "travel-procurement.pdf",
            "rule_id": "R_VIA_002",
            "rule_name": "importe_total_requerido",
            "normalized_status": "failed",
            "severity": "error",
            "message": "Falta el importe total del gasto de viaje. Campo obligatorio.",
            "agent_notes": "supplier quotation and payment voucher for wheat flour",
            "evidence_refs": "procurement bid summary",
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [procurement_rule])
    }
    issues = tables[REVIEW_ISSUES_TABLE]

    assert any(
        row["issue_type"] == "internal_contradiction"
        and row["issue"] == "Invoice classified as travel but visible context suggests procurement or goods"
        for row in issues.rows
    )


def test_viajes_procurement_contradiction_ignores_negated_or_passed_evidence() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update(
        {
            "invoice_id": "travel-no-procurement",
            "invoice_file": "travel-no-procurement.pdf",
            "invoice_type_id": "VIAJES",
        }
    )
    failed_rule = dict(compliance_rows[0])
    failed_rule.update(
        {
            "invoice_id": "travel-no-procurement",
            "invoice_file": "travel-no-procurement.pdf",
            "rule_id": "R_VIA_002",
            "rule_name": "importe_total_requerido",
            "normalized_status": "failed",
            "severity": "error",
            "message": "Falta el importe total del gasto de viaje. Campo obligatorio.",
            "agent_notes": "No procurement or supplier quotation evidence",
            "evidence_refs": "",
        }
    )
    passed_procurement_note = dict(compliance_rows[1])
    passed_procurement_note.update(
        {
            "invoice_id": "travel-no-procurement",
            "invoice_file": "travel-no-procurement.pdf",
            "rule_id": "R_VIA_PASSED",
            "rule_name": "passed_context",
            "normalized_status": "passed",
            "severity": "warning",
            "message": "OK",
            "agent_notes": "supplier quotation payment voucher wheat flour",
            "evidence_refs": "procurement bid summary",
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_rule, passed_procurement_note])
    }
    issues = tables[REVIEW_ISSUES_TABLE]

    assert not any(
        row["issue_type"] == "internal_contradiction"
        and "procurement or goods" in row["issue"]
        for row in issues.rows
    )


def test_visible_policy_summary_suppresses_raw_rule_ids() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update({"invoice_id": "policy-summary", "invoice_file": "policy-summary.pdf"})
    failed_rule = dict(compliance_rows[0])
    failed_rule.update(
        {
            "invoice_id": "policy-summary",
            "invoice_file": "policy-summary.pdf",
            "rule_id": "R_VIA_001",
            "rule_name": "fecha_requerida",
            "normalized_status": "failed",
            "severity": "error",
            "message": "Falta la fecha.",
            "policy_refs": '{"snippet_id": "R_VIA_001", "source": "config/csv/compliance_rules.csv"}',
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_rule])
    }
    review = tables[REVIEW_QUEUE_TABLE]
    issues = tables[REVIEW_ISSUES_TABLE]

    assert review.rows[0]["policy_summary"] == "config/csv/compliance_rules.csv"
    assert "R_VIA_001" not in review.rows[0]["policy_summary"]
    assert "R_VIA_001" not in issues.rows[0]["policy_summary"]


def test_visible_policy_summary_suppresses_bare_rule_id_refs() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    invoice = dict(invoice_rows[0])
    invoice.update({"invoice_id": "bare-policy", "invoice_file": "bare-policy.pdf"})
    failed_rule = dict(compliance_rows[0])
    failed_rule.update(
        {
            "invoice_id": "bare-policy",
            "invoice_file": "bare-policy.pdf",
            "rule_id": "R_VIA_001",
            "rule_name": "fecha_requerida",
            "normalized_status": "failed",
            "severity": "error",
            "message": "Falta la fecha.",
            "policy_refs": "R_VIA_001; {\"snippet_id\": \"R_VIA_002\"}",
        }
    )

    tables = {
        table.name: table
        for table in build_workbook_tables([invoice], [failed_rule])
    }
    summary = tables[REVIEW_QUEUE_TABLE].rows[0]["policy_summary"]

    assert "R_VIA_" not in summary
    assert "Configured compliance rule" in summary


def test_dashboard_focuses_on_reviewer_actions_and_missing_fields() -> None:
    tables = _tables_by_name()
    dashboard = tables["Dashboard"]

    assert {"section", "metric", "value", "count"} == set(dashboard.headers)
    assert {
        (row["section"], row["metric"], row["value"], row["count"])
        for row in dashboard.rows
    } >= {
        ("Action summary", "Invoices", "Blocked / needs review", "1"),
        ("Action summary", "Invoices", "Passed", "1"),
        ("Action summary", "Invoices", "Warnings", "1"),
        ("Missing critical fields", "Field", "Invoice date", "3"),
        ("Missing critical fields", "Field", "Net amount", "2"),
        ("Rules needing attention", "Rule", "Net amount required", "1"),
        ("Legend", "Status", "not_checked", ""),
        ("Legend", "Status", "not_applicable", ""),
        ("Legend", "Issue", "internal_contradiction", ""),
    }


def test_rule_guide_uses_config_metadata_and_disambiguates_duplicate_names() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    rule_metadata = [
        {
            "rule_id": "R_DUP_A",
            "invoice_type_id": "TYPE",
            "rule_name": "same_rule",
            "field_id": "FIELD_A",
            "check_type": "required",
            "check_value": "",
            "severity": "error",
            "agent_hint": "Check field A.",
            "error_message": "Field A missing.",
        },
        {
            "rule_id": "R_DUP_B",
            "invoice_type_id": "TYPE",
            "rule_name": "same_rule",
            "field_id": "FIELD_B",
            "check_type": "required",
            "check_value": "",
            "severity": "warning",
            "agent_hint": "Check field B.",
            "error_message": "Field B missing.",
        },
    ]

    tables = {
        table.name: table
        for table in build_workbook_tables(invoice_rows, compliance_rows, rule_metadata)
    }
    guide = tables[RULE_GUIDE_TABLE]

    assert guide.headers == RULE_GUIDE_COLUMNS
    assert "rule_id" not in guide.headers
    assert {
        row["rule"]
        for row in guide.rows
        if str(row["rule"]).startswith("Same rule")
    } == {"Same rule - FIELD A", "Same rule - FIELD B"}
    assert any(row["guidance"] == "Check field A." for row in guide.rows)


def test_rule_guide_includes_invoice_level_failure_reasoning() -> None:
    tables = _tables_by_name()
    guide = tables[RULE_GUIDE_TABLE]

    net_rule = next(row for row in guide.rows if row["rule"] == "Net amount required")
    assert net_rule["failing_invoices"] == "drive-invoice-gamma.pdf"
    assert net_rule["warning_invoices"] == ""
    assert net_rule["reasoning"] == "drive-invoice-gamma.pdf: Net amount needs human confirmation"

    vat_rule = next(row for row in guide.rows if row["rule"] == "Vat rate plausibility")
    assert vat_rule["failing_invoices"] == ""
    assert vat_rule["warning_invoices"] == "invoice-beta.pdf"
    assert vat_rule["reasoning"] == "invoice-beta.pdf: VAT rate differs from expected rate"


def test_workbook_tables_are_built_from_canonical_rows_only() -> None:
    invoice_rows, compliance_rows = _canonical_rows()

    from_rows = build_workbook_tables(invoice_rows, compliance_rows)
    from_states = build_workbook_from_states(canonical_states())

    assert [table.name for table in from_states] == [
        "Dashboard",
        REVIEW_QUEUE_TABLE,
        REVIEW_ISSUES_TABLE,
        INVOICE_SUMMARY_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        RULE_GUIDE_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        TECHNICAL_RUN_DATA_TABLE,
    ]
    assert [(table.name, table.headers, table.rows) for table in from_states] == [
        (table.name, table.headers, table.rows) for table in from_rows
    ]
    assert from_states[-2] == WorkbookTable(
        RAW_COMPLIANCE_RESULTS_TABLE,
        COMPLIANCE_RESULT_COLUMNS,
        compliance_rows,
    )
    assert from_states[-1] == WorkbookTable(
        TECHNICAL_RUN_DATA_TABLE,
        INVOICE_SUMMARY_COLUMNS,
        invoice_rows,
    )


def test_in_memory_writer_captures_every_workbook_tab() -> None:
    writer = InMemoryWorkbookWriter()

    for table in build_workbook_from_states(canonical_states()):
        writer.write_sheet(table.name, table.headers, table.rows)

    assert list(writer.sheets) == [
        "Dashboard",
        REVIEW_QUEUE_TABLE,
        REVIEW_ISSUES_TABLE,
        INVOICE_SUMMARY_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        RULE_GUIDE_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        TECHNICAL_RUN_DATA_TABLE,
    ]
    assert "Net amount required" in writer.sheets[COMPLIANCE_MATRIX_TABLE][0]
    review_headers = writer.sheets[REVIEW_QUEUE_TABLE][0]
    blocking_index = review_headers.index("blocking_rules")
    assert writer.sheets[REVIEW_QUEUE_TABLE][1][blocking_index] == "Net amount required"


def test_write_workbook_csvs_writes_reviewer_and_raw_tabs(tmp_path: Path) -> None:
    tables = build_workbook_from_states(canonical_states())

    paths = write_workbook_csvs(tables, tmp_path)

    assert set(paths) == {table.name for table in tables}
    assert Path(paths[INVOICE_SUMMARY_TABLE]).name == "invoice_summary.csv"
    assert Path(paths[RAW_COMPLIANCE_RESULTS_TABLE]).name == "compliance_results.csv"
    assert Path(paths[TECHNICAL_RUN_DATA_TABLE]).name == "technical_run_data.csv"
    assert _read_csv(Path(paths[RAW_COMPLIANCE_RESULTS_TABLE])) == _read_csv(
        FIXTURE_DIR / "canonical_compliance_results.csv"
    )
    assert _read_csv(Path(paths[TECHNICAL_RUN_DATA_TABLE])) == _read_csv(
        FIXTURE_DIR / "canonical_invoice_summary.csv"
    )


def test_write_canonical_workbook_csvs_builds_tables_from_states(tmp_path: Path) -> None:
    paths = write_canonical_workbook_csvs(canonical_states(), tmp_path)

    assert set(paths) == {
        INVOICE_SUMMARY_TABLE,
        REVIEW_QUEUE_TABLE,
        REVIEW_ISSUES_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        "Dashboard",
        RULE_GUIDE_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        TECHNICAL_RUN_DATA_TABLE,
    }
    review_rows = _read_csv(Path(paths[REVIEW_QUEUE_TABLE]))
    assert review_rows[0]["blocking_rules"] == "Net amount required"
