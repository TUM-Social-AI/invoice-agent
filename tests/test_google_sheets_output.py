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
    ):
        self.metadata = metadata or {"sheets": []}
        self.failures = failures or {}
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
    assert disabled.mode == "replace"

    existing = parse_google_sheets_target(
        _config(enabled=True, spreadsheet_id="sheet-123", create_title="", mode="replace")
    )
    assert existing.enabled is True
    assert existing.spreadsheet_id == "sheet-123"
    assert existing.create_title is None

    created = parse_google_sheets_target(
        _config(enabled=True, spreadsheet_id="", create_title="Invoice Review", mode="replace")
    )
    assert created.spreadsheet_id is None
    assert created.create_title == "Invoice Review"

    with pytest.raises(GoogleSheetsOutputError, match="both spreadsheet_id and create_title"):
        parse_google_sheets_target(
            _config(enabled=True, spreadsheet_id="sheet-123", create_title="Invoice Review")
        )
    with pytest.raises(GoogleSheetsOutputError, match="requires spreadsheet_id or create_title"):
        parse_google_sheets_target(_config(enabled=True, spreadsheet_id="", create_title=""))
    with pytest.raises(GoogleSheetsOutputError, match="only supports replace"):
        parse_google_sheets_target(_config(enabled=True, spreadsheet_id="sheet-123", mode="append"))


def test_write_existing_spreadsheet_batches_structure_clear_and_values():
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
    ).write_workbook(tables, GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123"))

    assert result.spreadsheet_id == "sheet-123"
    assert result.spreadsheet_url == "https://docs.google.com/spreadsheets/d/sheet-123"
    assert result.managed_tabs == [table.name for table in tables]
    assert result.updated_ranges == len(tables)
    assert result.updated_cells == expected_cells

    assert service.get_calls == [
        {"spreadsheetId": "sheet-123", "fields": "spreadsheetUrl,sheets.properties(sheetId,title)"}
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
    ).write_workbook(tables, GoogleSheetsTarget(enabled=True, create_title="Invoice Review"))

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
    ).write_workbook(_tables()[:1], GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123"))

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
        ).write_workbook(_tables()[:1], GoogleSheetsTarget(enabled=True, spreadsheet_id="sheet-123"))

    assert service.executed.count("values.batchUpdate") == 0
    assert len(service.values_batch_update_calls) == 1


def test_default_config_exposes_disabled_google_sheets_output():
    from src.output.google_sheets import parse_google_sheets_target

    config = yaml.safe_load(Path("config/config.yaml").read_text(encoding="utf-8"))

    target = parse_google_sheets_target(config)

    assert target.enabled is False
    assert target.spreadsheet_id is None
    assert target.create_title is None
    assert target.mode == "replace"
    assert target.value_input_option == "RAW"
    assert target.include_generated_views is True
