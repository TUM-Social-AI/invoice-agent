from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

from src.agent.state import AgentState
from src.output.canonical import (
    COMPLIANCE_RESULT_COLUMNS,
    INVOICE_SUMMARY_COLUMNS,
    _stringify_cell,
    build_compliance_result_rows,
    build_invoice_summary_row,
)


class InMemoryWorkbookWriter:
    def __init__(self) -> None:
        self.sheets: dict[str, list[list[str]]] = {}

    def write_sheet(
        self,
        name: str,
        headers: list[str],
        rows: list[dict[str, object]],
    ) -> None:
        self.sheets[name] = [
            list(headers),
            *[
                [_stringify_cell(row.get(header, "")) for header in headers]
                for row in rows
            ],
        ]


def _write_csv(path: Path, headers: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({header: _stringify_cell(row.get(header, "")) for header in headers})


def write_canonical_csvs(
    states: Iterable[AgentState],
    output_dir: str | Path,
) -> dict[str, str]:
    materialized_states = list(states)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    invoice_summary_path = out / "invoice_summary.csv"
    compliance_results_path = out / "compliance_results.csv"

    summary_rows = [build_invoice_summary_row(state) for state in materialized_states]
    compliance_rows = [
        row
        for state in materialized_states
        for row in build_compliance_result_rows(state)
    ]

    _write_csv(invoice_summary_path, INVOICE_SUMMARY_COLUMNS, summary_rows)
    _write_csv(compliance_results_path, COMPLIANCE_RESULT_COLUMNS, compliance_rows)

    return {
        "invoice_summary_csv": str(invoice_summary_path),
        "compliance_results_csv": str(compliance_results_path),
    }


def _workbook_filename(table_name: str) -> str:
    return f"{table_name.strip().lower().replace(' ', '_')}.csv"


def write_workbook_csvs(
    tables: Iterable[object],
    output_dir: str | Path,
) -> dict[str, str]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    paths: dict[str, str] = {}
    for table in tables:
        name = getattr(table, "name")
        headers = getattr(table, "headers")
        rows = getattr(table, "rows")
        path = out / _workbook_filename(name)
        _write_csv(path, headers, rows)
        paths[name] = str(path)
    return paths


def write_canonical_workbook_csvs(
    states: Iterable[AgentState],
    output_dir: str | Path,
) -> dict[str, str]:
    from src.output.workbook import build_workbook_from_states

    return write_workbook_csvs(build_workbook_from_states(states), output_dir)
