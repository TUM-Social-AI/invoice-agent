from pathlib import Path
from types import SimpleNamespace

import main as cli

from src.agent.state import AgentState, AgentStatus
from src.sources.models import SourceProvenance


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
