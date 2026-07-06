from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.output.canonical import _stringify_cell
from src.output.workbook import build_workbook_from_states
from tests.test_canonical_output import canonical_states


class FakeGoogleRequestError(Exception):
    def __init__(self, status: int):
        super().__init__(f"fake google request failed with {status}")
        self.resp = type("FakeResponse", (), {"status": status})()


class FakeExecuteRequest:
    def __init__(self, service: "FakeSheetsService", operation: str, response: dict):
        self.service = service
        self.operation = operation
        self.response = response

    def execute(self):
        failures = self.service.failures.get(self.operation, [])
        if failures:
            raise failures.pop(0)
        self.service.executed.append(self.operation)
        return self.response


class FakeValuesResource:
    def __init__(self, service: "FakeSheetsService"):
        self.service = service

    def get(self, **kwargs):
        self.service.values_get_calls.append(kwargs)
        return FakeExecuteRequest(
            self.service,
            "values.get",
            {"values": self.service.existing_values_by_range.get(kwargs.get("range"), [])},
        )

    def batchClear(self, **kwargs):
        self.service.batch_clear_calls.append(kwargs)
        return FakeExecuteRequest(self.service, "values.batchClear", {})

    def batchUpdate(self, **kwargs):
        self.service.values_batch_update_calls.append(kwargs)
        return FakeExecuteRequest(
            self.service,
            "values.batchUpdate",
            {
                "totalUpdatedCells": self.service.updated_cells,
                "totalUpdatedRows": self.service.updated_rows,
                "totalUpdatedSheets": self.service.updated_sheets,
            },
        )

    def append(self, **kwargs):
        self.service.values_append_calls.append(kwargs)
        values = (kwargs.get("body") or {}).get("values") or []
        return FakeExecuteRequest(
            self.service,
            "values.append",
            {
                "updates": {
                    "updatedCells": sum(len(row) for row in values),
                    "updatedRows": len(values),
                    "updatedRange": kwargs.get("range", ""),
                }
            },
        )


class FakeSpreadsheetsResource:
    def __init__(self, service: "FakeSheetsService"):
        self.service = service
        self._values = FakeValuesResource(service)

    def values(self):
        return self._values

    def get(self, **kwargs):
        self.service.get_calls.append(kwargs)
        return FakeExecuteRequest(self.service, "spreadsheets.get", self.service.metadata)

    def create(self, **kwargs):
        self.service.create_calls.append(kwargs)
        return FakeExecuteRequest(
            self.service,
            "spreadsheets.create",
            {
                "spreadsheetId": self.service.created_spreadsheet_id,
                "spreadsheetUrl": self.service.created_spreadsheet_url,
            },
        )

    def batchUpdate(self, **kwargs):
        self.service.structural_batch_update_calls.append(kwargs)
        return FakeExecuteRequest(self.service, "spreadsheets.batchUpdate", {})


class FakeSheetsService:
    def __init__(
        self,
        *,
        metadata: dict | None = None,
        failures: dict[str, list[Exception]] | None = None,
        existing_values_by_range: dict[str, list[list[str]]] | None = None,
    ):
        self.metadata = metadata or {"sheets": []}
        self.failures = failures or {}
        self.existing_values_by_range = existing_values_by_range or {}
        self.created_spreadsheet_id = "created-sheet-123"
        self.created_spreadsheet_url = "https://docs.google.com/spreadsheets/d/created-sheet-123"
        self.updated_cells = 0
        self.updated_rows = 0
        self.updated_sheets = 0
        self.get_calls = []
        self.create_calls = []
        self.structural_batch_update_calls = []
        self.batch_clear_calls = []
        self.values_batch_update_calls = []
        self.values_get_calls = []
        self.values_append_calls = []
        self.executed = []
        self._spreadsheets = FakeSpreadsheetsResource(self)

    def spreadsheets(self):
        return self._spreadsheets


def _tables():
    return build_workbook_from_states(canonical_states())


def _config(**google_sheets):
    return {"output": {"google_sheets": google_sheets}}


def test_parse_google_sheets_target_accepts_disabled_and_exactly_one_target():
    from src.output.google_sheets import (
        GoogleSheetsOutputError,
        parse_google_sheets_target,
    )

    disabled = parse_google_sheets_target(_config(enabled=False))
    assert disabled.enabled is False
    assert disabled.mode == "append"
    assert disabled.hide_raw_tabs is True
    assert disabled.formatting is True
    assert disabled.include_native_pivots is True

    existing = parse_google_sheets_target(
        _config(enabled=True, spreadsheet_id="sheet-123", create_title="", mode="replace")
    )
    assert existing.enabled is True
    assert existing.spreadsheet_id == "sheet-123"
    assert existing.create_title is None

    created = parse_google_sheets_target(
        _config(enabled=True, spreadsheet_id="", create_title="Invoice Review")
    )
    assert created.spreadsheet_id is None
    assert created.create_title == "Invoice Review"
    assert created.mode == "append"

    with pytest.raises(GoogleSheetsOutputError, match="both spreadsheet_id and create_title"):
        parse_google_sheets_target(
            _config(enabled=True, spreadsheet_id="sheet-123", create_title="Invoice Review")
        )
    with pytest.raises(GoogleSheetsOutputError, match="requires spreadsheet_id or create_title"):
        parse_google_sheets_target(_config(enabled=True, spreadsheet_id="", create_title=""))
    with pytest.raises(GoogleSheetsOutputError, match="only supports replace or append"):
        parse_google_sheets_target(_config(enabled=True, spreadsheet_id="sheet-123", mode="merge"))


def test_write_existing_spreadsheet_replace_batches_structure_clear_and_values():
    from src.output.google_sheets import (
        GoogleSheetsTarget,
        GoogleSheetsWorkbookWriter,
        RetryPolicy,
    )

    tables = _tables()
    service = FakeSheetsService(
        metadata={
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-123",
            "sheets": [
                {"properties": {"sheetId": 10, "title": tables[0].name}},
                {"properties": {"sheetId": 11, "title": "Unmanaged Manual Tab"}},
            ],
        }
    )
    expected_cells = sum(len(table.headers) * (len(table.rows) + 1) for table in tables)
    service.updated_cells = expected_cells
    service.updated_rows = sum(len(table.rows) + 1 for table in tables)
    service.updated_sheets = len(tables)

    result = GoogleSheetsWorkbookWriter(
        service=service,
        retry_policy=RetryPolicy(max_attempts=1),
    ).write_workbook(
        tables,
        GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123", mode="replace", formatting=False),
    )

    assert result.spreadsheet_id == "sheet-123"
    assert result.spreadsheet_url == "https://docs.google.com/spreadsheets/d/sheet-123"
    assert result.managed_tabs == [table.name for table in tables]
    assert result.updated_ranges == len(tables)
    assert result.updated_cells == expected_cells

    assert service.get_calls == [
        {"spreadsheetId": "sheet-123", "fields": "spreadsheetUrl,sheets.properties(sheetId,title)"},
        {"spreadsheetId": "sheet-123", "fields": "spreadsheetUrl,sheets.properties(sheetId,title)"},
    ]
    assert len(service.structural_batch_update_calls) == 1
    add_sheet_requests = service.structural_batch_update_calls[0]["body"]["requests"]
    assert add_sheet_requests == [
        {"addSheet": {"properties": {"title": table.name}}}
        for table in tables[1:]
    ]

    assert len(service.batch_clear_calls) == 1
    assert service.batch_clear_calls[0] == {
        "spreadsheetId": "sheet-123",
        "body": {"ranges": [f"'{table.name}'" for table in tables]},
    }

    assert len(service.values_batch_update_calls) == 1
    values_call = service.values_batch_update_calls[0]
    assert values_call["spreadsheetId"] == "sheet-123"
    assert values_call["body"]["valueInputOption"] == "RAW"
    assert values_call["body"]["data"] == [
        {
            "range": f"'{table.name}'!A1",
            "majorDimension": "ROWS",
            "values": [
                table.headers,
                *[
                    [_stringify_cell(row.get(header, "")) for header in table.headers]
                    for row in table.rows
                ],
            ],
        }
        for table in tables
    ]


def test_write_existing_spreadsheet_append_preserves_existing_rows_and_adds_new_rows():
    from src.output.google_sheets import (
        GoogleSheetsTarget,
        GoogleSheetsWorkbookWriter,
        RetryPolicy,
    )

    tables = _tables()[:2]
    service = FakeSheetsService(
        metadata={
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-123",
            "sheets": [
                {"properties": {"sheetId": index + 10, "title": table.name}}
                for index, table in enumerate(tables)
            ],
        },
        existing_values_by_range={
            f"'{tables[0].name}'!A:A": [["section", "existing"]],
        },
    )

    result = GoogleSheetsWorkbookWriter(
        service=service,
        retry_policy=RetryPolicy(max_attempts=1),
    ).write_workbook(
        tables,
        GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123", mode="append", formatting=False),
    )

    assert result.spreadsheet_id == "sheet-123"
    assert result.managed_tabs == [table.name for table in tables]
    assert service.batch_clear_calls == []
    assert service.values_batch_update_calls == []
    assert service.values_get_calls == [
        {
            "spreadsheetId": "sheet-123",
            "range": f"'{table.name}'!A:A",
            "majorDimension": "COLUMNS",
        }
        for table in tables
    ]

    assert len(service.values_append_calls) == 2
    existing_tab_append = service.values_append_calls[0]
    empty_tab_append = service.values_append_calls[1]
    assert existing_tab_append["body"]["values"] == [
        [_stringify_cell(row.get(header, "")) for header in tables[0].headers]
        for row in tables[0].rows
    ]
    assert empty_tab_append["body"]["values"] == [
        tables[1].headers,
        *[
            [_stringify_cell(row.get(header, "")) for header in tables[1].headers]
            for row in tables[1].rows
        ],
    ]


def test_write_created_spreadsheet_creates_then_writes_managed_tabs():
    from src.output.google_sheets import (
        GoogleSheetsTarget,
        GoogleSheetsWorkbookWriter,
        RetryPolicy,
    )

    tables = _tables()[:2]
    service = FakeSheetsService(metadata={"sheets": []})

    result = GoogleSheetsWorkbookWriter(
        service=service,
        retry_policy=RetryPolicy(max_attempts=1),
    ).write_workbook(
        tables,
        GoogleSheetsTarget(enabled=True, create_title="Invoice Review", mode="replace", formatting=False),
    )

    assert service.create_calls == [{"body": {"properties": {"title": "Invoice Review"}}}]
    assert service.get_calls[0]["spreadsheetId"] == "created-sheet-123"
    assert service.batch_clear_calls[0]["spreadsheetId"] == "created-sheet-123"
    assert service.values_batch_update_calls[0]["spreadsheetId"] == "created-sheet-123"
    assert result.spreadsheet_id == "created-sheet-123"
    assert result.managed_tabs == [table.name for table in tables]


@pytest.mark.parametrize(
    "operation",
    ["spreadsheets.batchUpdate", "values.batchClear", "values.batchUpdate"],
)
def test_retry_handling_retries_transient_execute_failures(operation):
    from src.output.google_sheets import (
        GoogleSheetsTarget,
        GoogleSheetsWorkbookWriter,
        RetryPolicy,
    )

    service = FakeSheetsService(
        failures={operation: [FakeGoogleRequestError(429), FakeGoogleRequestError(503)]}
    )
    sleeps = []

    result = GoogleSheetsWorkbookWriter(
        service=service,
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_s=0.25,
            backoff_multiplier=2,
            sleep=sleeps.append,
        ),
    ).write_workbook(
        _tables()[:1],
        GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123", mode="replace", formatting=False),
    )

    assert result.spreadsheet_id == "sheet-123"
    assert sleeps == [0.25, 0.5]


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_retry_handling_does_not_retry_permanent_request_failures(status):
    from src.output.google_sheets import (
        GoogleSheetsTarget,
        GoogleSheetsWorkbookWriter,
        RetryPolicy,
    )

    service = FakeSheetsService(
        failures={"values.batchUpdate": [FakeGoogleRequestError(status)]}
    )

    with pytest.raises(FakeGoogleRequestError):
        GoogleSheetsWorkbookWriter(
            service=service,
            retry_policy=RetryPolicy(max_attempts=3, sleep=lambda _: None),
        ).write_workbook(
            _tables()[:1],
            GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123", mode="replace", formatting=False),
        )

    assert service.executed.count("values.batchUpdate") == 0
    assert len(service.values_batch_update_calls) == 1


def test_google_sheets_formatting_hides_raw_tabs_and_adds_pivot_requests():
    from src.output.google_sheets import (
        GoogleSheetsTarget,
        GoogleSheetsWorkbookWriter,
        RetryPolicy,
    )
    from src.output.workbook import (
        COMPLIANCE_MATRIX_TABLE,
        DASHBOARD_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        REVIEW_ISSUES_TABLE,
        REVIEW_QUEUE_TABLE,
        TECHNICAL_RUN_DATA_TABLE,
    )

    tables = _tables()
    service = FakeSheetsService(
        metadata={
            "spreadsheetUrl": "https://docs.google.com/spreadsheets/d/sheet-123",
            "sheets": [
                {"properties": {"sheetId": index + 10, "title": table.name}}
                for index, table in enumerate(tables)
            ],
        }
    )

    GoogleSheetsWorkbookWriter(
        service=service,
        retry_policy=RetryPolicy(max_attempts=1),
    ).write_workbook(tables, GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123", mode="replace"))

    assert len(service.structural_batch_update_calls) == 1
    requests = service.structural_batch_update_calls[0]["body"]["requests"]

    hidden_updates = [
        request["updateSheetProperties"]["properties"]
        for request in requests
        if "updateSheetProperties" in request
        and request["updateSheetProperties"]["properties"].get("hidden") is True
    ]
    hidden_sheet_ids = {props["sheetId"] for props in hidden_updates}
    sheet_ids = {
        table.name: index + 10
        for index, table in enumerate(tables)
    }
    assert hidden_sheet_ids == {
        sheet_ids[RAW_COMPLIANCE_RESULTS_TABLE],
        sheet_ids[TECHNICAL_RUN_DATA_TABLE],
    }
    frozen_updates = [
        request["updateSheetProperties"]["properties"]
        for request in requests
        if "updateSheetProperties" in request
    ]
    frozen_by_sheet = {
        props["sheetId"]: props["gridProperties"]["frozenColumnCount"]
        for props in frozen_updates
    }
    assert frozen_by_sheet[sheet_ids[REVIEW_QUEUE_TABLE]] == 6
    assert frozen_by_sheet[sheet_ids[REVIEW_ISSUES_TABLE]] == 7
    assert frozen_by_sheet[sheet_ids[COMPLIANCE_MATRIX_TABLE]] == 3

    assert any("setBasicFilter" in request for request in requests)
    assert any("autoResizeDimensions" in request for request in requests)
    assert any("updateDimensionProperties" in request for request in requests)
    validation_requests = [request for request in requests if "setDataValidation" in request]
    assert len(validation_requests) == 2
    validation_values = {
        value["userEnteredValue"]
        for request in validation_requests
        for value in request["setDataValidation"]["rule"]["condition"]["values"]
    }
    assert {"needs_info", "approved", "rejected", "escalated"}.issubset(validation_values)
    assert any("addConditionalFormatRule" in request for request in requests)
    conditional_values = {
        value["userEnteredValue"]
        for request in requests
        if "addConditionalFormatRule" in request
        for value in request["addConditionalFormatRule"]["rule"]["booleanRule"]["condition"]["values"]
    }
    assert {
        "not_checked",
        "not_applicable",
        "missing_critical_field",
        "internal_contradiction",
    }.issubset(conditional_values)
    assert any(
        request.get("updateCells", {}).get("fields") == "note"
        for request in requests
        if "updateCells" in request
    )
    assert any(
        request.get("updateCells", {}).get("fields") == "pivotTable"
        and request["updateCells"]["start"]["sheetId"] == sheet_ids[DASHBOARD_TABLE]
        for request in requests
        if "updateCells" in request
    )
    assert any(
        request.get("addChart", {})
        .get("chart", {})
        .get("position", {})
        .get("overlayPosition", {})
        .get("anchorCell", {})
        .get("sheetId")
        == sheet_ids[DASHBOARD_TABLE]
        for request in requests
    )
    assert sheet_ids[COMPLIANCE_MATRIX_TABLE]


def test_parse_google_sheets_target_reads_usability_options():
    from src.output.google_sheets import parse_google_sheets_target

    target = parse_google_sheets_target(
        _config(
            enabled=True,
            spreadsheet_id="sheet-123",
            hide_raw_tabs=False,
            formatting=False,
            include_native_pivots=False,
            include_charts=False,
        )
    )

    assert target.hide_raw_tabs is False
    assert target.formatting is False
    assert target.include_native_pivots is False
    assert target.include_charts is False


def test_default_config_parses_google_sheets_output():
    from src.output.google_sheets import parse_google_sheets_target

    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))

    target = parse_google_sheets_target(config)

    assert target.mode == "append"
    assert target.value_input_option == "RAW"
    assert target.include_generated_views is True
