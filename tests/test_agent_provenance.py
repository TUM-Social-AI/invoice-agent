from pathlib import Path
from types import SimpleNamespace

from src.agent.agent import InvoiceAgent
from src.agent.state import AgentStatus


def test_invoice_agent_run_legacy_call_creates_local_provenance(tmp_path: Path, monkeypatch):
    pdf = tmp_path / "legacy.pdf"
    pdf.write_bytes(b"%PDF")
    output = tmp_path / "output"

    agent = InvoiceAgent.__new__(InvoiceAgent)
    agent.config = {"agent": {"page_dpi": 150}}
    agent.confidence_threshold = 0.65
    agent.batch_review_threshold = 0.85
    agent.tools = {}
    agent.store = SimpleNamespace(get_type=lambda invoice_type_id: True)
    agent.provider = object()
    agent.orchestration = "loop"
    agent.max_turns = 1

    def fake_run_loop(self, state, log_handle, log_path):
        state.status = AgentStatus.PASSED
        log_handle.close()

    monkeypatch.setattr(InvoiceAgent, "_run_agent_loop", fake_run_loop)

    state = agent.run(str(pdf), str(output))

    assert state.pdf_path == str(pdf)
    assert state.source_provenance is not None
    assert state.source_provenance.source_type == "local"
    assert state.source_provenance.source_uri == str(pdf.resolve())
    assert state.run_identity is not None
    assert state.run_id == state.run_identity.run_id
