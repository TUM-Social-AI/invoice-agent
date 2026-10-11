"""Tests for the browser UI: web presenter events, run hub replay, and the HTTP/SSE API."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.agent.state import AgentState, AgentStatus
from src.ui.app import create_app
from src.ui.events import RunHub
from src.ui.runner import MAX_UPLOAD_BYTES, RunManager
from src.ui.web_presenter import WebPresenter

PDF_BYTES = b"%PDF-1.4\n%fake\n"


def _types(hub: RunHub) -> list[str]:
    return [e["type"] for e in hub.events_after(0)]


# --- RunHub ------------------------------------------------------------------------


def test_hub_assigns_sequential_ids_and_replays_from_any_point():
    hub = RunHub("r1", "a.pdf")
    hub.publish("a")
    hub.publish("b", x=1)
    hub.publish("c")
    assert [e["seq"] for e in hub.events_after(0)] == [1, 2, 3]
    assert [e["type"] for e in hub.events_after(2)] == ["c"]
    assert hub.events_after(3) == []


# --- WebPresenter ------------------------------------------------------------------


def test_presenter_drops_events_when_no_run_attached():
    p = WebPresenter()
    p.phase_change("SCAN")  # must not raise
    p.tool_start(1, "inspect_file", "", {})


def test_presenter_emits_phase_tool_and_result_events():
    hub = RunHub("r1", "a.pdf")
    p = WebPresenter()
    p.attach(hub)

    p.run_start("C:/x/a.pdf")
    p.phase_change("SCAN")
    p.phase_change("SCAN")  # duplicate phase is ignored
    p.tool_start(1, "inspect_file", "why", {})
    p.tool_result("inspect_file", {"success": True, "page_count": 2, "size_mb": 1.0, "format": "PDF"}, 120)

    assert _types(hub) == ["run_started", "phase", "tool_start", "tool_result"]
    start, result = hub.events_after(2)
    assert start["label"] == "Inspecting file"
    assert result["id"] == start["id"]
    assert result["success"] is True
    assert "2 pages" in result["primary"]
    assert result["elapsed_ms"] == 120


def test_presenter_forces_validate_phase_and_counts_retries():
    hub = RunHub("r1", "a.pdf")
    p = WebPresenter()
    p.attach(hub)

    p.tool_start(5, "check_compliance", "", {})
    p.tool_result("check_compliance", {"success": True}, 10)
    p.tool_start(6, "check_compliance", "", {})

    events = hub.events_after(0)
    assert events[0]["type"] == "phase" and events[0]["phase"] == "VALIDATE"
    starts = [e for e in events if e["type"] == "tool_start"]
    assert [s["attempt"] for s in starts] == [1, 2]
    assert starts[0]["id"] != starts[1]["id"]


def test_presenter_finish_summary_has_decision_and_levelled_details():
    hub = RunHub("r1", "a.pdf")
    p = WebPresenter()
    p.attach(hub)
    p.tool_start(9, "finish", "", {})
    p.tool_result(
        "finish",
        {
            "status": "needs_review",
            "error_failures": ["R1"],
            "warning_failures": ["R2"],
            "status_explanation": "One rule failed.",
        },
        5,
    )
    result = hub.events_after(0)[-1]
    assert result["primary"] == "Decision: NEEDS_REVIEW"
    levels = {d["level"] for d in result["details"]}
    assert levels == {"error", "warn", "info"}


def test_presenter_run_complete_reports_verdict():
    hub = RunHub("r1", "a.pdf")
    p = WebPresenter()
    p.attach(hub)
    state = AgentState(pdf_path="a.pdf", output_dir="out", status=AgentStatus.PASSED, turn=7)
    p.run_complete(state, paths={"fields_csv": "out/fields.csv"})
    ev = hub.events_after(0)[-1]
    assert ev["type"] == "run_complete"
    assert ev["status"] == "passed"
    assert ev["turns"] == 7
    assert ev["outputs"] == {"fields_csv": "out/fields.csv"}


# --- API ---------------------------------------------------------------------------


class StubAgent:
    """Drives the presenter like the real agent would, without any models."""

    def __init__(self, presenter: WebPresenter, *, fail: bool = False, delay: float = 0.0) -> None:
        self.presenter = presenter
        self.fail = fail
        self.delay = delay

    def run(self, pdf_path, output_dir, source_provenance=None, run_identity=None):
        self.presenter.run_start(pdf_path)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model exploded")
        self.presenter.phase_change("SCAN")
        self.presenter.tool_start(1, "inspect_file", "", {})
        self.presenter.tool_result("inspect_file", {"success": True, "page_count": 1, "size_mb": 0.1, "format": "PDF"}, 5)
        return AgentState(pdf_path=pdf_path, output_dir=output_dir, status=AgentStatus.PASSED, turn=1)


def _manager(tmp_path, **stub_kwargs) -> RunManager:
    presenter = WebPresenter()
    return RunManager(
        StubAgent(presenter, **stub_kwargs),
        presenter,
        tmp_path,
        materialize=lambda path: [SimpleNamespace(local_pdf_path=path, provenance=None, run_identity=None)],
        write_results=lambda state, out_dir: {"fields_csv": f"{out_dir}/fields.csv"},
    )


def _read_sse(client: TestClient, run_id: str) -> list[dict]:
    events = []
    with client.stream("GET", f"/api/runs/{run_id}/events") as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def test_upload_runs_agent_and_streams_events_then_replays(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    res = client.post("/api/runs", files={"file": ("My Invoice.pdf", PDF_BYTES, "application/pdf")})
    assert res.status_code == 201
    run = res.json()
    assert run["filename"] == "My_Invoice.pdf"

    events = _read_sse(client, run["run_id"])
    assert [e["type"] for e in events] == [
        "run_queued", "run_started", "phase", "tool_start", "tool_result", "run_complete",
    ]
    assert events[-1]["status"] == "passed"

    # A refresh after completion replays the same history.
    assert [e["seq"] for e in _read_sse(client, run["run_id"])] == [e["seq"] for e in events]
    assert client.get(f"/api/runs/{run['run_id']}").json()["state"] == "done"


def test_sse_resumes_after_last_event_id(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    run_id = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    _read_sse(client, run_id)
    with client.stream("GET", f"/api/runs/{run_id}/events", headers={"Last-Event-ID": "5"}) as resp:
        data = [json.loads(l[6:]) for l in resp.iter_lines() if l.startswith("data: ")]
    assert [e["type"] for e in data] == ["run_complete"]


def test_agent_exception_becomes_run_error_and_worker_survives(tmp_path):
    manager = _manager(tmp_path, fail=True)
    client = TestClient(create_app(manager))
    run_id = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    events = _read_sse(client, run_id)
    assert events[-1]["type"] == "run_error"
    assert "model exploded" in events[-1]["message"]
    assert client.get(f"/api/runs/{run_id}").json()["state"] == "error"

    # The worker thread must still accept the next run.
    manager.agent.fail = False
    second = client.post("/api/runs", files={"file": ("b.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    assert _read_sse(client, second)[-1]["type"] == "run_complete"


def test_second_upload_waits_for_the_first(tmp_path):
    manager = _manager(tmp_path, delay=0.4)
    client = TestClient(create_app(manager))
    first = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    second = client.post("/api/runs", files={"file": ("b.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    queued = manager.get(second).events_after(0)[0]
    assert queued["type"] == "run_queued" and queued["ahead"] >= 1
    assert _read_sse(client, first)[-1]["type"] == "run_complete"
    assert _read_sse(client, second)[-1]["type"] == "run_complete"


@pytest.mark.parametrize(
    "make_data",
    [
        pytest.param(lambda: b"", id="empty"),
        pytest.param(lambda: b"not a pdf at all", id="not-a-pdf"),
        pytest.param(lambda: b"%PDF" + b"0" * MAX_UPLOAD_BYTES, id="too-large"),
    ],
)
def test_bad_uploads_are_rejected_with_400(tmp_path, make_data):
    client = TestClient(create_app(_manager(tmp_path)))
    res = client.post("/api/runs", files={"file": ("a.pdf", make_data(), "application/pdf")})
    assert res.status_code == 400
    assert res.json()["detail"]


def test_unknown_run_is_404(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    assert client.get("/api/runs/nope").status_code == 404
    assert client.get("/api/runs/nope/events").status_code == 404


def test_index_page_is_served(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    res = client.get("/")
    assert res.status_code == 200
    assert "Invoice Compliance Agent" in res.text
