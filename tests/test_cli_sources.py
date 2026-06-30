from pathlib import Path
from types import SimpleNamespace

import pytest

import main as cli

from src.agent.state import AgentState, AgentStatus
from src.sources.models import DocumentRef, MaterializedDocument, RunIdentity, SourceProvenance


def _patch_cli_runtime(monkeypatch):
    store = SimpleNamespace(invoice_types={})
    summary = cli.ConfigLoadSummary(
        source="local config/csv",
        path="config/csv",
        invoice_types=0,
        extraction_fields=0,
        compliance_rules=0,
        allowed_value_sets=0,
        denylist_phrases=0,
    )
    monkeypatch.setattr(cli, "_load_dotenv_files", lambda: None)
    monkeypatch.setattr(cli, "load_app_config", lambda path: {})
    monkeypatch.setattr(cli, "apply_configured_log_level", lambda config, **kwargs: None)
    monkeypatch.setattr(cli, "load_config_store", lambda config: (store, summary))
    monkeypatch.setattr(cli, "InvoiceAgent", lambda config, store, presenter=None: object())


def test_config_summary_counts_loaded_store_and_allowed_values(tmp_path: Path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "allowed_values.csv").write_text(
        "field_name,invoice_type_id,value\n"
        "currency,,EUR\n"
        "currency,,USD\n"
        "expense_category,VIAJES,hotel\n",
        encoding="utf-8",
    )
    store = SimpleNamespace(
        invoice_types={"VIAJES": object(), "EQUIPOS": object()},
        extraction_fields={"VIAJES": [object(), object()], "EQUIPOS": [object()]},
        compliance_rules={"VIAJES": [object()], "EQUIPOS": [object(), object()]},
        employee_name_role_denylist=["manager"],
    )

    summary = cli._config_summary("local config/csv", config_dir, store)

    assert summary.source == "local config/csv"
    assert summary.invoice_types == 2
    assert summary.extraction_fields == 3
    assert summary.compliance_rules == 3
    assert summary.allowed_value_sets == 2
    assert summary.denylist_phrases == 1


def test_load_config_store_local_returns_summary(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "csv"
    config_dir.mkdir()
    (config_dir / "allowed_values.csv").write_text(
        "field_name,invoice_type_id,value\ncurrency,,EUR\n",
        encoding="utf-8",
    )
    store = SimpleNamespace(
        invoice_types={"VIAJES": object()},
        extraction_fields={"VIAJES": [object()]},
        compliance_rules={"VIAJES": [object(), object()]},
        employee_name_role_denylist=[],
    )

    monkeypatch.setattr(cli, "google_drive_config_folder_enabled", lambda config: False)
    monkeypatch.setattr(cli, "load_config", lambda path: store)

    loaded, summary = cli.load_config_store({"config_dir": str(config_dir)})

    assert loaded is store
    assert summary.source == "local config/csv"
    assert summary.path == str(config_dir)
    assert summary.allowed_value_sets == 1


def test_load_config_store_google_drive_returns_summary(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "drive-csv"
    config_dir.mkdir()
    (config_dir / "allowed_values.csv").write_text(
        "field_name,invoice_type_id,value\ncurrency,,EUR\n",
        encoding="utf-8",
    )
    store = SimpleNamespace(
        invoice_types={"VIAJES": object()},
        extraction_fields={"VIAJES": [object(), object()]},
        compliance_rules={"VIAJES": [object(), object(), object()]},
        employee_name_role_denylist=["manager", "director"],
    )

    monkeypatch.setattr(cli, "google_drive_config_folder_enabled", lambda config: True)
    monkeypatch.setattr(cli, "materialize_google_drive_config_folder", lambda config: config_dir)
    monkeypatch.setattr(cli, "load_config", lambda path: store)

    loaded, summary = cli.load_config_store({})

    assert loaded is store
    assert summary.source == "Google Drive config folder"
    assert summary.path == str(config_dir)
    assert summary.invoice_types == 1
    assert summary.extraction_fields == 2
    assert summary.compliance_rules == 3
    assert summary.denylist_phrases == 2


def test_main_single_pdf_routes_through_sources_as_single_run(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    pdf = tmp_path / "example.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    calls = []

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        calls.append((pdf_path, output_dir, kwargs))
        return (
            SimpleNamespace(invoice_type_id=""),
            {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0},
            None,
        )

    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(
        cli,
        "_print_batch_summary",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("batch summary should not run")),
    )
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(pdf), "--output", str(output)])

    cli.main()

    assert len(calls) == 1
    assert calls[0][0] == str(pdf.resolve())
    assert calls[0][1] == str(output / "example")
    assert calls[0][2]["source_provenance"].source_type == "local"
    assert calls[0][2]["run_identity"].safe_document_stem == "example"


def test_main_one_pdf_folder_routes_as_batch_and_preserves_output_stem(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    folder = tmp_path / "invoices"
    folder.mkdir()
    pdf = folder / "Only.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    calls = []
    summaries = []

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        calls.append((pdf_path, output_dir, kwargs))
        return (
            SimpleNamespace(invoice_type_id=""),
            {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0},
            None,
        )

    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: summaries.append((args, kwargs)))
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(folder), "--output", str(output)])

    cli.main()

    assert len(calls) == 1
    assert calls[0][0] == str(pdf.resolve())
    assert calls[0][1] == str(output / "Only")
    assert calls[0][2]["source_provenance"].metadata["discovered_via"] == "folder"
    assert len(summaries) == 1


def test_process_invoice_uses_state_provenance_from_agent_fallback(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "example.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    provenance = SourceProvenance.from_local_path_minimal(str(pdf))
    captured = {}

    class FakeAgent:
        config = {}
        store = None

        def run(self, **kwargs):
            state = AgentState(
                pdf_path=kwargs["pdf_path"],
                output_dir=kwargs["output_dir"],
                source_provenance=provenance,
            )
            state.status = AgentStatus.PASSED
            return state

    def fake_load_ground_truth(pdf_path, **kwargs):
        captured["source_provenance"] = kwargs.get("source_provenance")
        return None

    monkeypatch.setattr(cli, "write_results", lambda state, output_dir: {"fields_csv": str(output / "fields.csv")})
    monkeypatch.setattr(cli, "load_ground_truth", fake_load_ground_truth)
    monkeypatch.setattr(cli, "ground_truth_csv_configured", lambda config: False)

    cli.process_invoice(FakeAgent(), str(pdf), str(output))

    assert captured["source_provenance"] == provenance


def _drive_doc(tmp_path: Path) -> MaterializedDocument:
    pdf = tmp_path / "materialized.pdf"
    pdf.write_bytes(b"%PDF")
    ref = DocumentRef(
        source_type="google_drive",
        display_name="Drive Invoice.pdf",
        uri="gdrive://drive-file-1",
        source_id="drive-file-1",
        revision_id="rev-1",
        mime_type="application/pdf",
    )
    provenance = SourceProvenance(
        source_type="google_drive",
        source_id="drive-file-1",
        source_uri="gdrive://drive-file-1",
        display_name="Drive Invoice.pdf",
        original_filename="Drive Invoice.pdf",
        revision_id="rev-1",
        source_hash="abc123def456",
        materialization_method="download",
    )
    run_identity = RunIdentity(
        run_id="20260608T000000000000Z-drive-invoice-abc123def456",
        created_at_utc=provenance.materialized_at_utc,
        safe_document_stem="drive-invoice",
        source_hash="abc123def456",
    )
    return MaterializedDocument(
        ref=ref,
        local_pdf_path=str(pdf),
        provenance=provenance,
        run_identity=run_identity,
    )


def test_main_drive_auth_saves_token_without_constructing_agent(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    calls = []

    monkeypatch.setattr(
        cli,
        "resolve_google_drive_credentials",
        lambda config, **kwargs: calls.append(kwargs) or SimpleNamespace(scopes=["scope-a"]),
    )
    monkeypatch.setattr(
        cli,
        "InvoiceAgent",
        lambda config, store: (_ for _ in ()).throw(AssertionError("agent should not be constructed")),
    )
    monkeypatch.setattr("sys.argv", ["main.py", "--drive-auth", "--drive-oauth-client-secret", str(tmp_path / "c.json")])

    cli.main()

    assert calls == [{"oauth_client_secret_path": str(tmp_path / "c.json"), "force_interactive": True}]


def test_main_google_drive_folder_routes_as_batch_and_cleans_download(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    output = tmp_path / "output"
    doc = _drive_doc(tmp_path)
    calls = []
    cleaned = []
    summaries = []

    monkeypatch.setattr(cli, "resolve_google_drive_credentials", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "build_google_drive_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "discover_google_drive_documents", lambda *args, **kwargs: [doc.ref])
    monkeypatch.setattr(cli, "materialize_google_drive_document", lambda *args, **kwargs: doc)

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        calls.append((pdf_path, output_dir, kwargs))
        return SimpleNamespace(invoice_type_id="VIAJES"), {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0}, {}

    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(cli, "cleanup_materialized_google_drive_document", lambda d: cleaned.append(d))
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: summaries.append((args, kwargs)))
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--google-drive-folder-id", "folder-1", "--output", str(output)],
    )

    cli.main()

    assert len(calls) == 1
    assert calls[0][0] == str(tmp_path / "materialized.pdf")
    assert calls[0][1] == str(output / "drive-invoice-abc123def456")
    assert calls[0][2]["source_provenance"].source_type == "google_drive"
    assert calls[0][2]["run_identity"].safe_document_stem == "drive-invoice"
    assert cleaned == [doc]
    assert len(summaries) == 1


def test_main_google_drive_folder_empty_exits_without_agent(tmp_path: Path, monkeypatch, capsys):
    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(cli, "resolve_google_drive_credentials", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "build_google_drive_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "discover_google_drive_documents", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        cli,
        "InvoiceAgent",
        lambda config, store: (_ for _ in ()).throw(AssertionError("agent should not be constructed")),
    )
    monkeypatch.setattr("sys.argv", ["main.py", "--google-drive-folder-id", "folder-1"])

    cli.main()

    assert "No PDF files found in Google Drive folder: folder-1" in capsys.readouterr().out


def test_main_uses_configured_google_drive_folder_when_pdf_is_omitted(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    config = {
        "sources": {
            "google_drive": {
                "folder_url": "https://drive.google.com/drive/folders/folder-from-config",
            }
        }
    }
    doc = _drive_doc(tmp_path)
    seen = {}

    monkeypatch.setattr(cli, "load_app_config", lambda path: config)
    monkeypatch.setattr(cli, "resolve_google_drive_credentials", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "build_google_drive_service", lambda *args, **kwargs: object())

    def fake_discover(folder_id, *args, **kwargs):
        seen["folder_id"] = folder_id
        return [doc.ref]

    monkeypatch.setattr(cli, "discover_google_drive_documents", fake_discover)
    monkeypatch.setattr(cli, "materialize_google_drive_document", lambda *args, **kwargs: doc)
    monkeypatch.setattr(cli, "process_invoice", lambda *args, **kwargs: (SimpleNamespace(invoice_type_id=""), None, None))
    monkeypatch.setattr(cli, "cleanup_materialized_google_drive_document", lambda d: None)
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["main.py"])

    cli.main()

    assert seen["folder_id"] == "folder-from-config"


def test_main_rejects_pdf_and_google_drive_folder_together(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--pdf", str(tmp_path / "a.pdf"), "--google-drive-folder-id", "folder-1"],
    )

    try:
        cli.main()
    except SystemExit as e:
        assert e.code == 2
    else:
        raise AssertionError("Expected parser SystemExit")


def test_main_uploads_workbook_fixture_csv_dir_without_invoice_processing(monkeypatch, capsys):
    from src.output.workbook import WorkbookTable

    fixture_dir = Path("tests/fixtures/output")
    config = {
        "output": {
            "google_sheets": {
                "enabled": False,
                "spreadsheet_id": "",
                "create_title": "",
                "mode": "replace",
                "value_input_option": "RAW",
            }
        }
    }
    tables = [
        WorkbookTable("Invoice Summary", ["invoice_id"], [{"invoice_id": "SECRET_ROW_VALUE"}]),
        WorkbookTable("Compliance Results", ["rule_id"], [{"rule_id": "R_TOTAL_PRESENT"}]),
    ]
    captured = {}

    class FakeWriter:
        def __init__(self, **kwargs):
            captured["writer_kwargs"] = kwargs

        def write_workbook(self, workbook_tables, target):
            captured["tables"] = list(workbook_tables)
            captured["target"] = target
            return SimpleNamespace(
                spreadsheet_id="sheet-123",
                spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-123",
                managed_tabs=[table.name for table in captured["tables"]],
                updated_ranges=len(captured["tables"]),
                updated_cells=42,
            )

    monkeypatch.setattr(cli, "_load_dotenv_files", lambda: None)
    monkeypatch.setattr(cli, "load_app_config", lambda path: config)
    monkeypatch.setattr(cli, "apply_configured_log_level", lambda config, **kwargs: None)
    monkeypatch.setattr(cli, "load_workbook_tables_from_csv_dir", lambda path: tables, raising=False)
    monkeypatch.setattr(cli, "GoogleSheetsWorkbookWriter", FakeWriter, raising=False)

    forbidden = AssertionError("fixture upload must bypass invoice processing")
    monkeypatch.setattr(cli, "load_config_store", lambda config: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "InvoiceAgent", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "process_invoice", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "materialize_local_input", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "resolve_google_drive_credentials", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "build_google_drive_service", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "discover_google_drive_documents", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(cli, "materialize_google_drive_document", lambda *args, **kwargs: (_ for _ in ()).throw(forbidden))
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "--upload-workbook-csv-dir",
            str(fixture_dir),
            "--sheets-spreadsheet-id",
            "sheet-123",
        ],
    )

    cli.main()

    assert captured["tables"] == tables
    assert captured["target"].enabled is True
    assert captured["target"].spreadsheet_id == "sheet-123"
    assert captured["target"].create_title is None
    assert captured["writer_kwargs"] == {"app_config": config}

    output = capsys.readouterr().out
    assert "sheet-123" in output
    assert "2 managed tab" in output
    assert "SECRET_ROW_VALUE" not in output
    assert "R_TOTAL_PRESENT" not in output


def test_main_upload_workbook_csv_dir_rejects_ambiguous_target_overrides(monkeypatch, capsys):
    fixture_dir = Path("tests/fixtures/output")
    config = {
        "output": {
            "google_sheets": {
                "enabled": False,
                "spreadsheet_id": "",
                "create_title": "",
                "mode": "replace",
            }
        }
    }

    monkeypatch.setattr(cli, "_load_dotenv_files", lambda: None)
    monkeypatch.setattr(cli, "load_app_config", lambda path: config)
    monkeypatch.setattr(cli, "apply_configured_log_level", lambda config, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "load_workbook_tables_from_csv_dir",
        lambda path: (_ for _ in ()).throw(AssertionError("ambiguous target should fail before loading")),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "GoogleSheetsWorkbookWriter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("writer should not be constructed")),
        raising=False,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "--upload-workbook-csv-dir",
            str(fixture_dir),
            "--sheets-spreadsheet-id",
            "sheet-123",
            "--sheets-create-title",
            "Invoice Review",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
    assert "both spreadsheet_id and create_title" in capsys.readouterr().out


def test_load_workbook_tables_from_csv_dir_reads_expected_fixture_tabs():
    from src.output.google_sheets import load_workbook_tables_from_csv_dir
    from src.output.workbook import (
        COMPLIANCE_MATRIX_TABLE,
        DASHBOARD_RULE_COUNTS_TABLE,
        DASHBOARD_SEVERITY_COUNTS_TABLE,
        DASHBOARD_STATUS_COUNTS_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        RAW_INVOICE_SUMMARY_TABLE,
        REVIEW_QUEUE_TABLE,
    )

    tables = load_workbook_tables_from_csv_dir("tests/fixtures/output")

    assert [table.name for table in tables] == [
        RAW_INVOICE_SUMMARY_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        REVIEW_QUEUE_TABLE,
        DASHBOARD_STATUS_COUNTS_TABLE,
        DASHBOARD_RULE_COUNTS_TABLE,
        DASHBOARD_SEVERITY_COUNTS_TABLE,
    ]
    assert tables[0].headers[:4] == ["schema_version", "run_id", "invoice_id", "invoice_file"]
    assert tables[0].rows[0]["invoice_id"] == "invoice-alpha"
    assert list(tables[0].rows[0]) == tables[0].headers
    assert tables[2].headers[-1] == "R_VAT_REQUIRED"
    alpha_matrix_row = next(row for row in tables[2].rows if row["invoice_id"] == "invoice-alpha")
    assert alpha_matrix_row["R_VAT_REQUIRED"] == "passed"


def test_main_upload_workbook_csv_dir_refuses_partial_fixture_before_writer(tmp_path: Path, monkeypatch):
    complete_fixture_dir = Path("tests/fixtures/output")
    partial_dir = tmp_path / "partial-workbook"
    partial_dir.mkdir()
    (partial_dir / "invoice_summary.csv").write_text(
        (complete_fixture_dir / "canonical_invoice_summary.csv").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    config = {
        "output": {
            "google_sheets": {
                "enabled": False,
                "spreadsheet_id": "",
                "create_title": "",
                "mode": "replace",
            }
        }
    }

    monkeypatch.setattr(cli, "_load_dotenv_files", lambda: None)
    monkeypatch.setattr(cli, "load_app_config", lambda path: config)
    monkeypatch.setattr(cli, "apply_configured_log_level", lambda config, **kwargs: None)
    monkeypatch.setattr(
        cli,
        "GoogleSheetsWorkbookWriter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("writer should not be constructed")),
        raising=False,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "--upload-workbook-csv-dir",
            str(partial_dir),
            "--sheets-spreadsheet-id",
            "sheet-123",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 1
