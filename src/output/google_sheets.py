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
    DASHBOARD_TABLE,
    INVOICE_SUMMARY_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    REVIEW_QUEUE_TABLE,
    RULE_GUIDE_TABLE,
    TECHNICAL_RUN_DATA_TABLE,
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
    INVOICE_SUMMARY_TABLE,
    REVIEW_QUEUE_TABLE,
    COMPLIANCE_MATRIX_TABLE,
    DASHBOARD_TABLE,
    RULE_GUIDE_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    TECHNICAL_RUN_DATA_TABLE,
]

_RAW_FIXTURE_FILENAME_FALLBACKS = {
    INVOICE_SUMMARY_TABLE: "canonical_invoice_summary.csv",
    RAW_COMPLIANCE_RESULTS_TABLE: "canonical_compliance_results.csv",
    TECHNICAL_RUN_DATA_TABLE: "canonical_invoice_summary.csv",
    COMPLIANCE_MATRIX_TABLE: "workbook_compliance_matrix.csv",
    REVIEW_QUEUE_TABLE: "workbook_review_queue.csv",
    DASHBOARD_TABLE: "workbook_dashboard_status_counts.csv",
}

_HIDDEN_RAW_TABLES = {RAW_COMPLIANCE_RESULTS_TABLE, TECHNICAL_RUN_DATA_TABLE}


@dataclass(frozen=True)
class GoogleSheetsTarget:
    enabled: bool = True
    spreadsheet_id: str | None = None
    create_title: str | None = None
    mode: str = "replace"
    value_input_option: str = "RAW"
    include_generated_views: bool = True
    hide_raw_tabs: bool = True
    formatting: bool = True
    include_native_pivots: bool = True
    include_charts: bool = True


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
    hide_raw_tabs = bool(cfg.get("hide_raw_tabs", True))
    formatting = bool(cfg.get("formatting", True))
    include_native_pivots = bool(cfg.get("include_native_pivots", True))
    include_charts = bool(cfg.get("include_charts", True))

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
        hide_raw_tabs=hide_raw_tabs,
        formatting=formatting,
        include_native_pivots=include_native_pivots,
        include_charts=include_charts,
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
    optional_tables = {RULE_GUIDE_TABLE}
    for table_name in WORKBOOK_TABLE_ORDER:
        candidates = [fixture_dir / _workbook_filename(table_name)]
        fallback = _RAW_FIXTURE_FILENAME_FALLBACKS.get(table_name)
        if fallback:
            candidates.append(fixture_dir / fallback)
        csv_path = next((candidate for candidate in candidates if candidate.exists()), None)
        if csv_path is None:
            if table_name not in optional_tables:
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
        if table_name in resolved_paths
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


def _metadata_tab_ids(metadata: dict[str, Any]) -> dict[str, int]:
    ids: dict[str, int] = {}
    for sheet in metadata.get("sheets", []) or []:
        props = sheet.get("properties") or {}
        title = str(props.get("title") or "").strip()
        sheet_id = props.get("sheetId")
        if title and sheet_id is not None:
            ids[title] = int(sheet_id)
    return ids


def _col_index(headers: list[str], name: str) -> int | None:
    try:
        return headers.index(name)
    except ValueError:
        return None


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
            metadata = self._execute(
                self.service.spreadsheets().get(
                    spreadsheetId=spreadsheet_id,
                    fields="spreadsheetUrl,sheets.properties(sheetId,title)",
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

        if target.formatting:
            requests = self._formatting_requests(materialized_tables, metadata, target)
            if requests:
                self._execute(
                    self.service.spreadsheets().batchUpdate(
                        spreadsheetId=spreadsheet_id,
                        body={"requests": requests},
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

    def _formatting_requests(
        self,
        tables: list[WorkbookTable],
        metadata: dict[str, Any],
        target: GoogleSheetsTarget,
    ) -> list[dict[str, Any]]:
        sheet_ids = _metadata_tab_ids(metadata)
        requests: list[dict[str, Any]] = []
        tables_by_name = {table.name: table for table in tables}

        for index, table in enumerate(tables):
            sheet_id = sheet_ids.get(table.name)
            if sheet_id is None:
                continue
            row_count = max(len(table.rows) + 1, 1)
            col_count = max(len(table.headers), 1)
            hidden = target.hide_raw_tabs and table.name in _HIDDEN_RAW_TABLES
            frozen_column_count = 0
            if table.name == REVIEW_QUEUE_TABLE:
                frozen_column_count = 2
            elif table.name == COMPLIANCE_MATRIX_TABLE:
                frozen_column_count = 3

            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "index": index,
                            "hidden": hidden,
                            "gridProperties": {
                                "frozenRowCount": 1,
                                "frozenColumnCount": frozen_column_count,
                            },
                        },
                        "fields": "index,hidden,gridProperties(frozenRowCount,frozenColumnCount)",
                    }
                }
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 0,
                            "endRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": col_count,
                        },
                        "cell": {
                            "userEnteredFormat": {
                                "backgroundColor": {"red": 0.9, "green": 0.93, "blue": 0.97},
                                "textFormat": {"bold": True},
                                "wrapStrategy": "WRAP",
                            }
                        },
                        "fields": "userEnteredFormat(backgroundColor,textFormat,wrapStrategy)",
                    }
                }
            )
            requests.append(
                {
                    "repeatCell": {
                        "range": {
                            "sheetId": sheet_id,
                            "startRowIndex": 1,
                            "endRowIndex": row_count,
                            "startColumnIndex": 0,
                            "endColumnIndex": col_count,
                        },
                        "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
                        "fields": "userEnteredFormat.wrapStrategy",
                    }
                }
            )
            requests.append(
                {
                    "setBasicFilter": {
                        "filter": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": row_count,
                                "startColumnIndex": 0,
                                "endColumnIndex": col_count,
                            }
                        }
                    }
                }
            )
            requests.append(
                {
                    "autoResizeDimensions": {
                        "dimensions": {
                            "sheetId": sheet_id,
                            "dimension": "COLUMNS",
                            "startIndex": 0,
                            "endIndex": col_count,
                        }
                    }
                }
            )
            requests.extend(self._status_conditional_format_requests(table, sheet_id, row_count, col_count))

        requests.extend(self._matrix_header_note_requests(tables_by_name, sheet_ids))
        if target.include_native_pivots:
            requests.extend(self._native_pivot_requests(tables_by_name, sheet_ids))
        if target.include_charts:
            requests.extend(self._chart_requests(tables_by_name, sheet_ids))
        return requests

    def _status_conditional_format_requests(
        self,
        table: WorkbookTable,
        sheet_id: int,
        row_count: int,
        col_count: int,
    ) -> list[dict[str, Any]]:
        if row_count <= 1:
            return []
        colors = {
            "failed": {"red": 0.96, "green": 0.8, "blue": 0.8},
            "flagged": {"red": 0.96, "green": 0.8, "blue": 0.8},
            "warning": {"red": 1.0, "green": 0.91, "blue": 0.64},
            "passed": {"red": 0.82, "green": 0.93, "blue": 0.78},
            "skipped": {"red": 0.88, "green": 0.88, "blue": 0.88},
            "unknown": {"red": 0.9, "green": 0.86, "blue": 0.96},
            "needs_review": {"red": 1.0, "green": 0.91, "blue": 0.64},
            "error": {"red": 0.96, "green": 0.8, "blue": 0.8},
            "not_checked": {"red": 0.9, "green": 0.86, "blue": 0.96},
            "not_applicable": {"red": 0.88, "green": 0.88, "blue": 0.88},
        }
        requests: list[dict[str, Any]] = []
        for status, color in colors.items():
            requests.append(
                {
                    "addConditionalFormatRule": {
                        "rule": {
                            "ranges": [
                                {
                                    "sheetId": sheet_id,
                                    "startRowIndex": 1,
                                    "endRowIndex": row_count,
                                    "startColumnIndex": 0,
                                    "endColumnIndex": col_count,
                                }
                            ],
                            "booleanRule": {
                                "condition": {
                                    "type": "TEXT_EQ",
                                    "values": [{"userEnteredValue": status}],
                                },
                                "format": {"backgroundColor": color},
                            },
                        },
                        "index": 0,
                    }
                }
            )
        return requests

    def _matrix_header_note_requests(
        self,
        tables_by_name: dict[str, WorkbookTable],
        sheet_ids: dict[str, int],
    ) -> list[dict[str, Any]]:
        matrix = tables_by_name.get(COMPLIANCE_MATRIX_TABLE)
        guide = tables_by_name.get(RULE_GUIDE_TABLE)
        matrix_id = sheet_ids.get(COMPLIANCE_MATRIX_TABLE)
        if not matrix or not guide or matrix_id is None:
            return []

        guide_by_rule = {_stringify_cell(row.get("rule")): row for row in guide.rows}
        values = []
        for header in matrix.headers:
            guide_row = guide_by_rule.get(header)
            if not guide_row:
                values.append({})
                continue
            parts = [
                _stringify_cell(guide_row.get("severity")),
                _stringify_cell(guide_row.get("condition")),
                _stringify_cell(guide_row.get("guidance")),
                _stringify_cell(guide_row.get("failure_message")),
            ]
            values.append({"note": "\n".join(part for part in parts if part)})

        if not any(value.get("note") for value in values):
            return []
        return [
            {
                "updateCells": {
                    "start": {"sheetId": matrix_id, "rowIndex": 0, "columnIndex": 0},
                    "rows": [{"values": values}],
                    "fields": "note",
                }
            }
        ]

    def _native_pivot_requests(
        self,
        tables_by_name: dict[str, WorkbookTable],
        sheet_ids: dict[str, int],
    ) -> list[dict[str, Any]]:
        compliance = tables_by_name.get(RAW_COMPLIANCE_RESULTS_TABLE)
        dashboard = tables_by_name.get(DASHBOARD_TABLE)
        source_id = sheet_ids.get(RAW_COMPLIANCE_RESULTS_TABLE)
        dashboard_id = sheet_ids.get(DASHBOARD_TABLE)
        if not compliance or not dashboard or source_id is None or dashboard_id is None:
            return []

        rule_col = _col_index(compliance.headers, "rule_name")
        status_col = _col_index(compliance.headers, "normalized_status")
        invoice_col = _col_index(compliance.headers, "invoice_id")
        if rule_col is None or status_col is None or invoice_col is None:
            return []

        start_row = max(len(dashboard.rows) + 3, 3)
        return [
            {
                "updateCells": {
                    "start": {"sheetId": dashboard_id, "rowIndex": start_row, "columnIndex": 0},
                    "rows": [
                        {
                            "values": [
                                {
                                    "pivotTable": {
                                        "source": {
                                            "sheetId": source_id,
                                            "startRowIndex": 0,
                                            "startColumnIndex": 0,
                                            "endRowIndex": len(compliance.rows) + 1,
                                            "endColumnIndex": len(compliance.headers),
                                        },
                                        "rows": [
                                            {
                                                "sourceColumnOffset": rule_col,
                                                "showTotals": True,
                                                "sortOrder": "ASCENDING",
                                            }
                                        ],
                                        "columns": [
                                            {
                                                "sourceColumnOffset": status_col,
                                                "showTotals": True,
                                                "sortOrder": "ASCENDING",
                                            }
                                        ],
                                        "values": [
                                            {
                                                "summarizeFunction": "COUNTA",
                                                "sourceColumnOffset": invoice_col,
                                                "name": "Invoices",
                                            }
                                        ],
                                        "valueLayout": "HORIZONTAL",
                                    }
                                }
                            ]
                        }
                    ],
                    "fields": "pivotTable",
                }
            }
        ]

    def _chart_requests(
        self,
        tables_by_name: dict[str, WorkbookTable],
        sheet_ids: dict[str, int],
    ) -> list[dict[str, Any]]:
        dashboard = tables_by_name.get(DASHBOARD_TABLE)
        dashboard_id = sheet_ids.get(DASHBOARD_TABLE)
        if not dashboard or dashboard_id is None or not dashboard.rows:
            return []

        return [
            {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": "Dashboard counts",
                            "basicChart": {
                                "chartType": "COLUMN",
                                "legendPosition": "NO_LEGEND",
                                "axis": [
                                    {"position": "BOTTOM_AXIS", "title": "Value"},
                                    {"position": "LEFT_AXIS", "title": "Count"},
                                ],
                                "domains": [
                                    {
                                        "domain": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": dashboard_id,
                                                        "startRowIndex": 1,
                                                        "endRowIndex": len(dashboard.rows) + 1,
                                                        "startColumnIndex": 2,
                                                        "endColumnIndex": 3,
                                                    }
                                                ]
                                            }
                                        }
                                    }
                                ],
                                "series": [
                                    {
                                        "series": {
                                            "sourceRange": {
                                                "sources": [
                                                    {
                                                        "sheetId": dashboard_id,
                                                        "startRowIndex": 1,
                                                        "endRowIndex": len(dashboard.rows) + 1,
                                                        "startColumnIndex": 3,
                                                        "endColumnIndex": 4,
                                                    }
                                                ]
                                            }
                                        },
                                        "targetAxis": "LEFT_AXIS",
                                    }
                                ],
                                "headerCount": 0,
                            },
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId": dashboard_id,
                                    "rowIndex": 0,
                                    "columnIndex": 5,
                                },
                                "widthPixels": 640,
                                "heightPixels": 360,
                            }
                        },
                    }
                }
            }
        ]
