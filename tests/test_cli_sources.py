from pathlib import Path
from types import SimpleNamespace

import main as cli

from src.agent.state import AgentState, AgentStatus
from src.sources.models import DocumentRef, MaterializedDocument, RunIdentity, SourceProvenance


def _patch_cli_runtime(monkeypatch):
    monkeypatch.setattr(cli, "_load_dotenv_files", lambda: None)
    monkeypatch.setattr(cli, "load_app_config", lambda path: {})
    monkeypatch.setattr(cli, "apply_configured_log_level", lambda config: None)
    monkeypatch.setattr(cli, "load_config", lambda config_dir: SimpleNamespace(invoice_types={}))
    monkeypatch.setattr(cli, "InvoiceAgent", lambda config, store: object())


def test_main_single_pdf_routes_through_sources_as_single_run(tmp_path: Path, monkeypatch):
    _patch_cli_runtime(monkeypatch)
    pdf = tmp_path / "example.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"
    calls = []

    def fake_process_invoice(agent, pdf_path, output_dir, **kwargs):
        calls.append((pdf_path, output_dir, kwargs))
        return SimpleNamespace(invoice_type_id=""), None, None

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
        return SimpleNamespace(invoice_type_id=""), None, None

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
