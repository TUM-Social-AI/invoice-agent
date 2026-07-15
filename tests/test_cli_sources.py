from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest

import main as cli

from src.agent.state import AgentState, AgentStatus
from src.output.workbook import (
    COMPLIANCE_MATRIX_TABLE,
    RAW_COMPLIANCE_RESULTS_TABLE,
    RAW_INVOICE_SUMMARY_TABLE,
    WorkbookTable,
)
from src.sources.models import DocumentRef, MaterializedDocument, RunIdentity, SourceProvenance
from tests.test_canonical_output import make_passed_state, make_needs_review_state


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
    monkeypatch.setattr(cli, "load_config_store", lambda config, **kwargs: (store, summary))
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


def test_load_config_store_force_local_ignores_enabled_google_drive_config(tmp_path: Path, monkeypatch):
    config_dir = tmp_path / "csv"
    config_dir.mkdir()
    (config_dir / "allowed_values.csv").write_text(
        "field_name,invoice_type_id,value\ncurrency,,EUR\n",
        encoding="utf-8",
    )
    store = SimpleNamespace(
        invoice_types={"VIAJES": object()},
        extraction_fields={"VIAJES": [object()]},
        compliance_rules={"VIAJES": [object()]},
        employee_name_role_denylist=[],
    )

    monkeypatch.setattr(cli, "google_drive_config_folder_enabled", lambda config: True)
    monkeypatch.setattr(
        cli,
        "materialize_google_drive_config_folder",
        lambda config: (_ for _ in ()).throw(AssertionError("Drive config should not be loaded")),
    )
    monkeypatch.setattr(cli, "load_config", lambda path: store)

    loaded, summary = cli.load_config_store({"config_dir": str(config_dir)}, force_local=True)

    assert loaded is store
    assert summary.source == "local config/csv"
    assert summary.path == str(config_dir)


def test_main_local_config_flag_bypasses_drive_config_for_local_pdf(tmp_path: Path, monkeypatch):
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
    config = {
        "config_dir": "config/csv",
        "sources": {
            "google_drive": {
                "config_folder": {"enabled": True, "folder_id": "drive-config-folder"},
            }
        },
    }
    pdf = tmp_path / "example.pdf"
    pdf.write_bytes(b"%PDF")
    calls = []

    monkeypatch.setattr(cli, "_load_dotenv_files", lambda: None)
    monkeypatch.setattr(cli, "load_app_config", lambda path: config)
    monkeypatch.setattr(cli, "apply_configured_log_level", lambda config, **kwargs: None)
    monkeypatch.setattr(cli, "load_config_store", lambda app_config, **kwargs: calls.append(kwargs) or (store, summary))
    monkeypatch.setattr(cli, "InvoiceAgent", lambda config, store, presenter=None: object())
    monkeypatch.setattr(
        cli,
        "process_invoice",
        lambda *args, **kwargs: (
            SimpleNamespace(invoice_type_id=""),
            {"fields_total": 0, "fields_exact": 0, "fields_partial": 0, "fields_wrong": 0},
            None,
        ),
    )
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(pdf), "--local-config"])

    cli.main()

    assert calls == [{"force_local": True}]


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
    monkeypatch.setattr(cli, "_write_batch_workbook_outputs", lambda *args, **kwargs: None)
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(folder), "--output", str(output)])

    cli.main()

    assert len(calls) == 1
    assert calls[0][0] == str(pdf.resolve())
    assert calls[0][1] == str(output / "Only")
    assert calls[0][2]["source_provenance"].metadata["discovered_via"] == "folder"
    assert len(summaries) == 1


def test_main_local_folder_batch_writes_canonical_workbook_after_legacy_outputs(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    folder = tmp_path / "invoices"
    folder.mkdir()
    first_pdf = folder / "Alpha.pdf"
    second_pdf = folder / "Beta.pdf"
    first_pdf.write_bytes(b"%PDF")
    second_pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    process_calls = []
    workbook_calls = []
    csv_calls = []
    states = [make_passed_state(), make_needs_review_state()]

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        process_calls.append((pdf_path, output_dir, kwargs))
        state = states[len(process_calls) - 1]
        state.pdf_path = pdf_path
        state.output_dir = output_dir
        state.source_provenance = kwargs["source_provenance"]
        state.run_identity = kwargs["run_identity"]
        return state, {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0}, None

    def fake_build_workbook_from_states(successful_states):
        captured_states = list(successful_states)
        workbook_calls.append(captured_states)
        return [
            WorkbookTable(
                RAW_INVOICE_SUMMARY_TABLE,
                ["invoice_id", "source_type", "run_id"],
                [
                    {
                        "invoice_id": state.run_identity.safe_document_stem,
                        "source_type": state.source_provenance.source_type,
                        "run_id": state.run_identity.run_id,
                    }
                    for state in captured_states
                ],
            )
        ]

    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "build_workbook_from_states", fake_build_workbook_from_states, raising=False)
    monkeypatch.setattr(
        cli,
        "write_workbook_csvs",
        lambda tables, output_dir: csv_calls.append((list(tables), Path(output_dir))) or {"Invoice Summary": str(Path(output_dir) / "invoice_summary.csv")},
        raising=False,
    )
    monkeypatch.setattr(cli, "parse_google_sheets_target", lambda config: SimpleNamespace(enabled=False), raising=False)
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(folder), "--output", str(output)])

    cli.main()

    assert [Path(call[1]).name for call in process_calls] == ["Alpha", "Beta"]
    assert workbook_calls == [states]
    assert csv_calls[0][1] == output / "canonical_workbook"
    assert csv_calls[0][0][0].rows == [
        {
            "invoice_id": states[0].run_identity.safe_document_stem,
            "source_type": "local",
            "run_id": states[0].run_identity.run_id,
        },
        {
            "invoice_id": states[1].run_identity.safe_document_stem,
            "source_type": "local",
            "run_id": states[1].run_identity.run_id,
        },
    ]


def test_main_local_folder_sheets_sync_filters_generated_views_when_disabled(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    folder = tmp_path / "invoices"
    folder.mkdir()
    pdf = folder / "Alpha.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    config = {"output": {"google_sheets": {"enabled": True}}}
    state = make_passed_state()
    uploaded = {}
    all_tables = [
        WorkbookTable(RAW_INVOICE_SUMMARY_TABLE, ["invoice_id"], [{"invoice_id": "alpha"}]),
        WorkbookTable(RAW_COMPLIANCE_RESULTS_TABLE, ["rule_id"], [{"rule_id": "R1"}]),
        WorkbookTable(COMPLIANCE_MATRIX_TABLE, ["invoice_id", "R1"], [{"invoice_id": "alpha", "R1": "passed"}]),
    ]

    class FakeWriter:
        def __init__(self, **kwargs):
            uploaded["writer_kwargs"] = kwargs

        def write_workbook(self, tables, target):
            uploaded["tables"] = list(tables)
            uploaded["target"] = target
            return SimpleNamespace(
                spreadsheet_id="sheet-123",
                spreadsheet_url="https://docs.google.com/spreadsheets/d/sheet-123",
                managed_tabs=[table.name for table in uploaded["tables"]],
                updated_ranges=len(uploaded["tables"]),
                updated_cells=8,
            )

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        state.pdf_path = pdf_path
        state.output_dir = output_dir
        state.source_provenance = kwargs["source_provenance"]
        state.run_identity = kwargs["run_identity"]
        return state, {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0}, None

    monkeypatch.setattr(cli, "load_app_config", lambda path: config)
    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "build_workbook_from_states", lambda states: all_tables, raising=False)
    monkeypatch.setattr(cli, "write_workbook_csvs", lambda tables, output_dir: {"ok": str(Path(output_dir) / "ok.csv")}, raising=False)
    monkeypatch.setattr(
        cli,
        "parse_google_sheets_target",
        lambda cfg: SimpleNamespace(enabled=True, include_generated_views=False),
        raising=False,
    )
    monkeypatch.setattr(cli, "GoogleSheetsWorkbookWriter", FakeWriter, raising=False)
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(folder), "--output", str(output)])

    cli.main()

    assert [table.name for table in uploaded["tables"]] == [
        RAW_INVOICE_SUMMARY_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
    ]
    assert uploaded["writer_kwargs"] == {"app_config": config}


def test_main_single_pdf_does_not_write_batch_workbook_or_sheets(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    pdf = tmp_path / "single.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    state = make_passed_state()
    calls = []

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        calls.append((pdf_path, output_dir, kwargs))
        return state, {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0}, None

    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(
        cli,
        "build_workbook_from_states",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("single-file run should not build batch workbook")),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "write_workbook_csvs",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("single-file run should not write batch workbook")),
        raising=False,
    )
    monkeypatch.setattr(
        cli,
        "GoogleSheetsWorkbookWriter",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("single-file run should not sync Sheets")),
        raising=False,
    )
    monkeypatch.setattr("sys.argv", ["main.py", "--pdf", str(pdf), "--output", str(output)])

    cli.main()

    assert len(calls) == 1


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


def _drive_doc_named(tmp_path: Path, name: str, source_id: str, revision_id: str, source_hash: str) -> MaterializedDocument:
    pdf = tmp_path / f"{Path(name).stem}.pdf"
    pdf.write_bytes(b"%PDF")
    ref = DocumentRef(
        source_type="google_drive",
        display_name=name,
        uri=f"gdrive://{source_id}",
        source_id=source_id,
        revision_id=revision_id,
        mime_type="application/pdf",
    )
    provenance = SourceProvenance(
        source_type="google_drive",
        source_id=source_id,
        source_uri=f"gdrive://{source_id}",
        display_name=name,
        original_filename=name,
        revision_id=revision_id,
        source_hash=source_hash,
        materialization_method="download",
    )
    run_identity = RunIdentity(
        run_id=f"20260608T000000000000Z-{Path(name).stem.lower()}-{source_hash}",
        created_at_utc=provenance.materialized_at_utc,
        safe_document_stem=Path(name).stem.lower().replace(" ", "-"),
        source_hash=source_hash,
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
    monkeypatch.setattr(cli, "_write_batch_workbook_outputs", lambda *args, **kwargs: None)
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


def test_main_google_drive_batch_writes_workbook_from_successes_and_keeps_failure_summary(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    output = tmp_path / "output"
    success_doc = _drive_doc_named(tmp_path, "Drive Success.pdf", "drive-success", "rev-1", "hashsuccess")
    failed_doc = _drive_doc_named(tmp_path, "Drive Failure.pdf", "drive-failure", "rev-2", "hashfailure")
    docs_by_id = {
        success_doc.ref.source_id: success_doc,
        failed_doc.ref.source_id: failed_doc,
    }
    success_state = make_needs_review_state()
    workbook_states = []
    csv_calls = []
    summaries = []

    monkeypatch.setattr(cli, "resolve_google_drive_credentials", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "build_google_drive_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(cli, "discover_google_drive_documents", lambda *args, **kwargs: [success_doc.ref, failed_doc.ref])
    monkeypatch.setattr(cli, "materialize_google_drive_document", lambda ref, *args, **kwargs: docs_by_id[ref.source_id])
    monkeypatch.setattr(cli, "cleanup_materialized_google_drive_document", lambda doc: None)

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        if Path(pdf_path).name == "Drive Failure.pdf":
            raise RuntimeError("processing failed")
        success_state.pdf_path = pdf_path
        success_state.output_dir = output_dir
        success_state.source_provenance = kwargs["source_provenance"]
        success_state.run_identity = kwargs["run_identity"]
        return success_state, {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0}, None

    def fake_build_workbook_from_states(states):
        captured = list(states)
        workbook_states.append(captured)
        return [
            WorkbookTable(
                RAW_INVOICE_SUMMARY_TABLE,
                ["invoice_id", "source_type", "source_id", "revision_id", "run_id"],
                [
                    {
                        "invoice_id": state.run_identity.safe_document_stem,
                        "source_type": state.source_provenance.source_type,
                        "source_id": state.source_provenance.source_id,
                        "revision_id": state.source_provenance.revision_id,
                        "run_id": state.run_identity.run_id,
                    }
                    for state in captured
                ],
            )
        ]

    monkeypatch.setattr(cli, "process_invoice", fake_process_invoice)
    monkeypatch.setattr(cli, "build_workbook_from_states", fake_build_workbook_from_states, raising=False)
    monkeypatch.setattr(
        cli,
        "write_workbook_csvs",
        lambda tables, output_dir: csv_calls.append((list(tables), Path(output_dir))) or {"Invoice Summary": str(Path(output_dir) / "invoice_summary.csv")},
        raising=False,
    )
    monkeypatch.setattr(cli, "parse_google_sheets_target", lambda config: SimpleNamespace(enabled=False), raising=False)
    monkeypatch.setattr(cli, "_print_batch_summary", lambda *args, **kwargs: summaries.append((args, kwargs)))
    monkeypatch.setattr(
        "sys.argv",
        ["main.py", "--google-drive-folder-id", "folder-1", "--output", str(output)],
    )

    cli.main()

    assert workbook_states == [[success_state]]
    assert csv_calls[0][1] == output / "canonical_workbook"
    assert csv_calls[0][0][0].rows == [
        {
            "invoice_id": "drive-success",
            "source_type": "google_drive",
            "source_id": "drive-success",
            "revision_id": "rev-1",
            "run_id": success_state.run_identity.run_id,
        }
    ]
    summary_results = summaries[0][0][0]
    assert summary_results == [
        ("Drive Success.pdf", success_state.invoice_type_id, {"fields_total": 1, "fields_exact": 1, "fields_partial": 0, "fields_wrong": 0}),
        ("Drive Failure.pdf", "", None),
    ]


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
    monkeypatch.setattr(cli, "_write_batch_workbook_outputs", lambda *args, **kwargs: None)
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
        COMPLIANCE_MATRIX_COLUMNS,
        COMPLIANCE_MATRIX_TABLE,
        DASHBOARD_TABLE,
        INVOICE_SUMMARY_REVIEWER_COLUMNS,
        INVOICE_SUMMARY_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        RAW_INVOICE_SUMMARY_TABLE,
        REVIEW_ISSUES_TABLE,
        REVIEW_QUEUE_TABLE,
        RULE_GUIDE_TABLE,
    )

    tables = load_workbook_tables_from_csv_dir("tests/fixtures/output")

    assert [table.name for table in tables] == [
        DASHBOARD_TABLE,
        REVIEW_QUEUE_TABLE,
        REVIEW_ISSUES_TABLE,
        INVOICE_SUMMARY_TABLE,
        COMPLIANCE_MATRIX_TABLE,
        RULE_GUIDE_TABLE,
        RAW_COMPLIANCE_RESULTS_TABLE,
        RAW_INVOICE_SUMMARY_TABLE,
    ]
    by_name = {table.name: table for table in tables}
    assert by_name[INVOICE_SUMMARY_TABLE].headers == INVOICE_SUMMARY_REVIEWER_COLUMNS
    assert by_name[RAW_INVOICE_SUMMARY_TABLE].rows[0]["invoice_id"] == "invoice-alpha"
    assert list(by_name[INVOICE_SUMMARY_TABLE].rows[0]) == by_name[INVOICE_SUMMARY_TABLE].headers
    assert by_name[REVIEW_ISSUES_TABLE].headers[:4] == [
        "priority",
        "issue_type",
        "severity",
        "recommended_action",
    ]
    assert by_name[COMPLIANCE_MATRIX_TABLE].headers[:3] == COMPLIANCE_MATRIX_COLUMNS
    assert not any(header.startswith("R_") for header in by_name[COMPLIANCE_MATRIX_TABLE].headers)
    alpha_matrix_row = next(
        row for row in by_name[COMPLIANCE_MATRIX_TABLE].rows if row["invoice_file"] == "invoice-alpha.pdf"
    )
    assert alpha_matrix_row["Vendor vat required"] == "passed"


def test_load_workbook_tables_from_csv_dir_rejects_stale_visible_generated_schema(tmp_path: Path):
    from src.output.google_sheets import GoogleSheetsOutputError, load_workbook_tables_from_csv_dir

    stale_dir = tmp_path / "stale-workbook"
    shutil.copytree(Path("tests/fixtures/output"), stale_dir)
    (stale_dir / "review.csv").write_text(
        "priority,invoice_id,invoice_file,blocking_rule_ids\n"
        "1,invoice-alpha,invoice-alpha.pdf,R_NET_REQUIRED\n",
        encoding="utf-8",
    )

    with pytest.raises(GoogleSheetsOutputError, match="stale or invalid headers"):
        load_workbook_tables_from_csv_dir(stale_dir)


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
