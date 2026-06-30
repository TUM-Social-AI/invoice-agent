from __future__ import annotations

import csv
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from src.output.canonical import _stringify_cell
from src.output.canonical_csv import _workbook_filename
from src.output.workbook import (
    COMPLIANCE_MATRIX_TABLE,
    DASHBOARD_RULE_COUNTS_TABLE,
    DASHBOARD_SEVERITY_COUNTS_TABLE,
    DASHBOARD_STATUS_COUNTS_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    RAW_INVOICE_SUMMARY_TABLE,
    REVIEW_QUEUE_TABLE,
    WorkbookTable,
)
from src.sources.google_drive import GoogleDriveSourceError, resolve_google_drive_credentials


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
TRANSIENT_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    socket.timeout,
    BrokenPipeError,
)


class GoogleSheetsOutputError(ValueError):
    """Raised when Google Sheets output config or upload behavior is invalid."""


WORKBOOK_TABLE_ORDER = [
    RAW_INVOICE_SUMMARY_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    COMPLIANCE_MATRIX_TABLE,
    REVIEW_QUEUE_TABLE,
    DASHBOARD_STATUS_COUNTS_TABLE,
    DASHBOARD_RULE_COUNTS_TABLE,
    DASHBOARD_SEVERITY_COUNTS_TABLE,
]

_RAW_FIXTURE_FILENAME_FALLBACKS = {
    RAW_INVOICE_SUMMARY_TABLE: "canonical_invoice_summary.csv",
    RAW_COMPLIANCE_RESULTS_TABLE: "canonical_compliance_results.csv",
    COMPLIANCE_MATRIX_TABLE: "workbook_compliance_matrix.csv",
    REVIEW_QUEUE_TABLE: "workbook_review_queue.csv",
    DASHBOARD_STATUS_COUNTS_TABLE: "workbook_dashboard_status_counts.csv",
    DASHBOARD_RULE_COUNTS_TABLE: "workbook_dashboard_rule_counts.csv",
    DASHBOARD_SEVERITY_COUNTS_TABLE: "workbook_dashboard_severity_counts.csv",
}


@dataclass(frozen=True)
class GoogleSheetsTarget:
    enabled: bool = True
    spreadsheet_id: str | None = None
    create_title: str | None = None
    mode: str = "replace"
    value_input_option: str = "RAW"
    include_generated_views: bool = True


@dataclass(frozen=True)
class GoogleSheetsUploadResult:
    spreadsheet_id: str
    spreadsheet_url: str
    managed_tabs: list[str]
    updated_ranges: int
    updated_cells: int


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_delay_s: float = 1.0
    backoff_multiplier: float = 2.0
    sleep: Callable[[float], None] = field(default=time.sleep, compare=False)


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _google_sheets_config(app_config: dict) -> dict:
    return (((app_config or {}).get("output") or {}).get("google_sheets") or {})


def parse_google_sheets_target(
    app_config: dict,
    overrides: dict[str, Any] | None = None,
) -> GoogleSheetsTarget:
    cfg = dict(_google_sheets_config(app_config))
    if overrides:
        cfg.update({key: value for key, value in overrides.items() if value is not None})

    enabled = bool(cfg.get("enabled", False))
    spreadsheet_id = _clean_optional(cfg.get("spreadsheet_id"))
    create_title = _clean_optional(cfg.get("create_title"))
    mode = str(cfg.get("mode") or "replace").strip().lower()
    value_input_option = str(cfg.get("value_input_option") or "RAW").strip().upper()
    include_generated_views = bool(cfg.get("include_generated_views", True))

    if spreadsheet_id and create_title:
        raise GoogleSheetsOutputError(
            "Google Sheets output cannot set both spreadsheet_id and create_title."
        )
    if mode != "replace":
        raise GoogleSheetsOutputError("Google Sheets output only supports replace mode in this version.")
    if enabled and not (spreadsheet_id or create_title):
        raise GoogleSheetsOutputError(
            "Enabled Google Sheets output requires spreadsheet_id or create_title."
        )

    return GoogleSheetsTarget(
        enabled=enabled,
        spreadsheet_id=spreadsheet_id,
        create_title=create_title,
        mode=mode,
        value_input_option=value_input_option,
        include_generated_views=include_generated_views,
    )


def build_google_sheets_service(app_config: dict, credentials: Any = None):
    creds = credentials or resolve_google_drive_credentials(app_config)
    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleDriveSourceError(
            "Google Sheets API dependency is missing. Run `pip install -r requirements.txt`."
        ) from e
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def load_workbook_tables_from_csv_dir(path: str | Path) -> list[WorkbookTable]:
    fixture_dir = Path(path)
    if not fixture_dir.is_dir():
        raise GoogleSheetsOutputError(f"Workbook CSV fixture directory does not exist: {fixture_dir}")

    resolved_paths: dict[str, Path] = {}
    missing: list[str] = []
    for table_name in WORKBOOK_TABLE_ORDER:
        candidates = [fixture_dir / _workbook_filename(table_name)]
        fallback = _RAW_FIXTURE_FILENAME_FALLBACKS.get(table_name)
        if fallback:
            candidates.append(fixture_dir / fallback)
        csv_path = next((candidate for candidate in candidates if candidate.exists()), None)
        if csv_path is None:
            missing.append(_workbook_filename(table_name))
        else:
            resolved_paths[table_name] = csv_path

    if missing:
        raise GoogleSheetsOutputError(
            "Workbook CSV fixture directory is missing required files: "
            + ", ".join(missing)
        )

    return [
        _load_workbook_table_csv(table_name, resolved_paths[table_name])
        for table_name in WORKBOOK_TABLE_ORDER
    ]


def _load_workbook_table_csv(table_name: str, path: Path) -> WorkbookTable:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        headers = list(reader.fieldnames or [])
        if not headers:
            raise GoogleSheetsOutputError(f"Workbook CSV fixture has no header row: {path}")
        rows = [{header: row.get(header, "") for header in headers} for row in reader]
    return WorkbookTable(table_name, headers, rows)


def _error_status(exc: BaseException) -> int | None:
    response = getattr(exc, "resp", None) or getattr(exc, "response", None)
    status = getattr(response, "status", None) or getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_retryable_error(exc: BaseException) -> bool:
    status = _error_status(exc)
    if status is not None:
        return status in RETRYABLE_STATUS_CODES
    return isinstance(exc, TRANSIENT_EXCEPTIONS)


def _quote_sheet_name(name: str) -> str:
    return "'" + name.replace("'", "''") + "'"


def _metadata_tab_titles(metadata: dict[str, Any]) -> set[str]:
    titles = set()
    for sheet in metadata.get("sheets", []) or []:
        title = ((sheet.get("properties") or {}).get("title") or "").strip()
        if title:
            titles.add(title)
    return titles


class GoogleSheetsWorkbookWriter:
    def __init__(
        self,
        *,
        service: Any = None,
        app_config: dict | None = None,
        credentials: Any = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._service = service
        self._app_config = app_config or {}
        self._credentials = credentials
        self._retry_policy = retry_policy or RetryPolicy()

    @property
    def service(self):
        if self._service is None:
            self._service = build_google_sheets_service(self._app_config, self._credentials)
        return self._service

    def write_workbook(
        self,
        tables: Iterable[WorkbookTable],
        target: GoogleSheetsTarget,
    ) -> GoogleSheetsUploadResult:
        materialized_tables = list(tables)
        if not target.enabled:
            raise GoogleSheetsOutputError("Google Sheets output target is disabled.")
        if target.mode != "replace":
            raise GoogleSheetsOutputError("Google Sheets workbook writer only supports replace mode.")
        if not materialized_tables:
            raise GoogleSheetsOutputError("Google Sheets workbook writer requires at least one table.")

        spreadsheet_id = target.spreadsheet_id
        spreadsheet_url = ""
        if target.create_title:
            created = self._execute(
                self.service.spreadsheets().create(
                    body={"properties": {"title": target.create_title}}
                )
            )
            spreadsheet_id = str(created.get("spreadsheetId") or "").strip()
            spreadsheet_url = str(created.get("spreadsheetUrl") or "").strip()
        if not spreadsheet_id:
            raise GoogleSheetsOutputError("Google Sheets target did not resolve a spreadsheet ID.")

        metadata = self._execute(
            self.service.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="spreadsheetUrl,sheets.properties(sheetId,title)",
            )
        )
        spreadsheet_url = spreadsheet_url or str(metadata.get("spreadsheetUrl") or "").strip()

        existing_titles = _metadata_tab_titles(metadata)
        missing_tables = [table for table in materialized_tables if table.name not in existing_titles]
        if missing_tables:
            self._execute(
                self.service.spreadsheets().batchUpdate(
                    spreadsheetId=spreadsheet_id,
                    body={
                        "requests": [
                            {"addSheet": {"properties": {"title": table.name}}}
                            for table in missing_tables
                        ]
                    },
                )
            )

        self._execute(
            self.service.spreadsheets().values().batchClear(
                spreadsheetId=spreadsheet_id,
                body={"ranges": [_quote_sheet_name(table.name) for table in materialized_tables]},
            )
        )

        values_response = self._execute(
            self.service.spreadsheets().values().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "valueInputOption": target.value_input_option,
                    "data": [
                        {
                            "range": f"{_quote_sheet_name(table.name)}!A1",
                            "majorDimension": "ROWS",
                            "values": self._table_values(table),
                        }
                        for table in materialized_tables
                    ],
                },
            )
        )

        return GoogleSheetsUploadResult(
            spreadsheet_id=spreadsheet_id,
            spreadsheet_url=spreadsheet_url
            or f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
            managed_tabs=[table.name for table in materialized_tables],
            updated_ranges=len(materialized_tables),
            updated_cells=int(values_response.get("totalUpdatedCells") or self._cell_count(materialized_tables)),
        )

    def _execute(self, request):
        policy = self._retry_policy
        delay = policy.initial_delay_s
        for attempt in range(1, policy.max_attempts + 1):
            try:
                return request.execute()
            except Exception as exc:
                if attempt >= policy.max_attempts or not _is_retryable_error(exc):
                    raise
                policy.sleep(delay)
                delay *= policy.backoff_multiplier
        raise GoogleSheetsOutputError("Google Sheets request retry loop exited unexpectedly.")

    def _table_values(self, table: WorkbookTable) -> list[list[str]]:
        return [
            list(table.headers),
            *[
                [_stringify_cell(row.get(header, "")) for header in table.headers]
                for row in table.rows
            ],
        ]

    def _cell_count(self, tables: list[WorkbookTable]) -> int:
        return sum(len(table.headers) * (len(table.rows) + 1) for table in tables)
