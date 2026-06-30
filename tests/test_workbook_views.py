from __future__ import annotations

from pathlib import Path

import pytest

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
    COMPLIANCE_MATRIX_COLUMNS,
    COMPLIANCE_MATRIX_TABLE,
    DASHBOARD_RULE_COUNTS_COLUMNS,
    DASHBOARD_RULE_COUNTS_TABLE,
    DASHBOARD_SEVERITY_COUNTS_COLUMNS,
    DASHBOARD_SEVERITY_COUNTS_TABLE,
    DASHBOARD_STATUS_COUNTS_COLUMNS,
    DASHBOARD_STATUS_COUNTS_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    RAW_INVOICE_SUMMARY_TABLE,
    REVIEW_QUEUE_COLUMNS,
    REVIEW_QUEUE_TABLE,
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


def test_compliance_matrix_matches_golden_fixture() -> None:
    invoice_rows, compliance_rows = _canonical_rows()

    table = build_compliance_matrix(invoice_rows, compliance_rows)

    assert table.name == COMPLIANCE_MATRIX_TABLE
    assert table.headers == [
        *COMPLIANCE_MATRIX_COLUMNS,
        "R_NET_REQUIRED",
        "R_SERVICE_DATE",
        "R_TOTAL_PRESENT",
        "R_VAT_RATE_WARN",
        "R_VAT_REQUIRED",
    ]
    assert table.rows == _read_csv(FIXTURE_DIR / "workbook_compliance_matrix.csv")


def test_compliance_matrix_rejects_ambiguous_duplicate_cells() -> None:
    invoice_rows, compliance_rows = _canonical_rows()
    duplicate = dict(compliance_rows[0])
    duplicate["normalized_status"] = "failed"

    with pytest.raises(ValueError, match="duplicate compliance result for invoice-alpha/R_VAT_REQUIRED"):
        build_compliance_matrix(invoice_rows, [*compliance_rows, duplicate])


def test_review_queue_matches_golden_fixture() -> None:
    invoice_rows, compliance_rows = _canonical_rows()

    tables = build_workbook_tables(invoice_rows, compliance_rows)
    queue = next(table for table in tables if table.name == REVIEW_QUEUE_TABLE)

    assert queue.headers == REVIEW_QUEUE_COLUMNS
    assert queue.rows == _read_csv(FIXTURE_DIR / "workbook_review_queue.csv")
    assert [row["invoice_id"] for row in queue.rows] == [
        "drive-invoice-gamma",
        "invoice-beta",
    ]
    assert all(row["invoice_id"] != "invoice-alpha" for row in queue.rows)


def test_dashboard_tables_match_golden_fixtures() -> None:
    invoice_rows, compliance_rows = _canonical_rows()

    tables = {table.name: table for table in build_workbook_tables(invoice_rows, compliance_rows)}

    assert tables[DASHBOARD_STATUS_COUNTS_TABLE].headers == DASHBOARD_STATUS_COUNTS_COLUMNS
    assert tables[DASHBOARD_STATUS_COUNTS_TABLE].rows == _read_csv(
        FIXTURE_DIR / "workbook_dashboard_status_counts.csv"
    )
    assert tables[DASHBOARD_RULE_COUNTS_TABLE].headers == DASHBOARD_RULE_COUNTS_COLUMNS
    assert tables[DASHBOARD_RULE_COUNTS_TABLE].rows == _read_csv(
        FIXTURE_DIR / "workbook_dashboard_rule_counts.csv"
    )
    assert tables[DASHBOARD_SEVERITY_COUNTS_TABLE].headers == DASHBOARD_SEVERITY_COUNTS_COLUMNS
    assert tables[DASHBOARD_SEVERITY_COUNTS_TABLE].rows == _read_csv(
        FIXTURE_DIR / "workbook_dashboard_severity_counts.csv"
    )


def test_workbook_tables_are_built_from_canonical_rows_only() -> None:
    invoice_rows, compliance_rows = _canonical_rows()

    from_rows = build_workbook_tables(invoice_rows, compliance_rows)
    from_states = build_workbook_from_states(canonical_states())

    assert [table.name for table in from_states] == [
        RAW_INVOICE_SUMMARY_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        REVIEW_QUEUE_TABLE,
        DASHBOARD_STATUS_COUNTS_TABLE,
        DASHBOARD_RULE_COUNTS_TABLE,
        DASHBOARD_SEVERITY_COUNTS_TABLE,
    ]
    assert [(table.name, table.headers, table.rows) for table in from_states] == [
        (table.name, table.headers, table.rows) for table in from_rows
    ]
    assert from_states[0] == WorkbookTable(
        RAW_INVOICE_SUMMARY_TABLE,
        INVOICE_SUMMARY_COLUMNS,
        invoice_rows,
    )
    assert from_states[1] == WorkbookTable(
        RAW_COMPLIANCE_RESULTS_TABLE,
        COMPLIANCE_RESULT_COLUMNS,
        compliance_rows,
    )


def test_in_memory_writer_captures_every_workbook_tab() -> None:
    writer = InMemoryWorkbookWriter()

    for table in build_workbook_from_states(canonical_states()):
        writer.write_sheet(table.name, table.headers, table.rows)

    assert list(writer.sheets) == [
        RAW_INVOICE_SUMMARY_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        REVIEW_QUEUE_TABLE,
        DASHBOARD_STATUS_COUNTS_TABLE,
        DASHBOARD_RULE_COUNTS_TABLE,
        DASHBOARD_SEVERITY_COUNTS_TABLE,
    ]
    assert writer.sheets[COMPLIANCE_MATRIX_TABLE][0] == [
        *COMPLIANCE_MATRIX_COLUMNS,
        "R_NET_REQUIRED",
        "R_SERVICE_DATE",
        "R_TOTAL_PRESENT",
        "R_VAT_RATE_WARN",
        "R_VAT_REQUIRED",
    ]
    assert writer.sheets[DASHBOARD_STATUS_COUNTS_TABLE][1] == [
        "compliance_status",
        "flagged",
        "1",
    ]


def test_write_workbook_csvs_writes_raw_and_generated_tabs(tmp_path: Path) -> None:
    tables = build_workbook_from_states(canonical_states())

    paths = write_workbook_csvs(tables, tmp_path)

    assert set(paths) == {table.name for table in tables}
    assert Path(paths[RAW_INVOICE_SUMMARY_TABLE]).name == "invoice_summary.csv"
    assert Path(paths[RAW_COMPLIANCE_RESULTS_TABLE]).name == "compliance_results.csv"
    assert Path(paths[COMPLIANCE_MATRIX_TABLE]).name == "compliance_matrix.csv"
    assert _read_csv(Path(paths[RAW_INVOICE_SUMMARY_TABLE])) == _read_csv(
        FIXTURE_DIR / "canonical_invoice_summary.csv"
    )
    assert _read_csv(Path(paths[RAW_COMPLIANCE_RESULTS_TABLE])) == _read_csv(
        FIXTURE_DIR / "canonical_compliance_results.csv"
    )
    assert _read_csv(Path(paths[COMPLIANCE_MATRIX_TABLE])) == _read_csv(
        FIXTURE_DIR / "workbook_compliance_matrix.csv"
    )
    assert _read_csv(Path(paths[REVIEW_QUEUE_TABLE])) == _read_csv(
        FIXTURE_DIR / "workbook_review_queue.csv"
    )
    assert _read_csv(Path(paths[DASHBOARD_STATUS_COUNTS_TABLE])) == _read_csv(
        FIXTURE_DIR / "workbook_dashboard_status_counts.csv"
    )
    assert _read_csv(Path(paths[DASHBOARD_RULE_COUNTS_TABLE])) == _read_csv(
        FIXTURE_DIR / "workbook_dashboard_rule_counts.csv"
    )
    assert _read_csv(Path(paths[DASHBOARD_SEVERITY_COUNTS_TABLE])) == _read_csv(
        FIXTURE_DIR / "workbook_dashboard_severity_counts.csv"
    )


def test_write_canonical_workbook_csvs_builds_tables_from_states(tmp_path: Path) -> None:
    paths = write_canonical_workbook_csvs(canonical_states(), tmp_path)

    assert set(paths) == {
        RAW_INVOICE_SUMMARY_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        REVIEW_QUEUE_TABLE,
        DASHBOARD_STATUS_COUNTS_TABLE,
        DASHBOARD_RULE_COUNTS_TABLE,
        DASHBOARD_SEVERITY_COUNTS_TABLE,
    }
    assert _read_csv(Path(paths[REVIEW_QUEUE_TABLE])) == _read_csv(
        FIXTURE_DIR / "workbook_review_queue.csv"
    )
