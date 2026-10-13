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


def test_presenter_finish_summary_has_decision_and_named_rules():
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
            "status_explanation": "No blocking error-severity failures; warning-level rules may still be non-pass "
            "(listed in warning_failures).",
        },
        5,
    )
    result = hub.events_after(0)[-1]
    assert result["primary"] == "Decision: NEEDS REVIEW"
    assert [(r["rule_id"], r["outcome"]) for r in result["rules"]] == [("R1", "blocking"), ("R2", "warning")]
    # Internal jargon from the finish tool is rewritten for readers.
    assert "warning_failures" not in result["details"][0]["text"]


def test_presenter_run_complete_reports_verdict():
    hub = RunHub("r1", "a.pdf")
    p = WebPresenter()
    p.attach(hub)
    state = AgentState(
        pdf_path="a.pdf", output_dir="out", status=AgentStatus.PASSED, turn=7,
        finish_reason="compliance_passed",
    )
    p.run_complete(state, paths={"fields_csv": "out/fields.csv"})
    ev = hub.events_after(0)[-1]
    assert ev["type"] == "run_complete"
    assert ev["status"] == "passed"
    assert ev["turns"] == 7
    assert ev["finish_reason"] == "Compliance passed"  # no snake_case identifiers on screen
    assert ev["rules"] == {"passed": 0, "blocking": [], "warnings": [], "not_checked": []}
    assert ev["outputs"] == {"fields_csv": "out/fields.csv"}


# --- API ---------------------------------------------------------------------------


class StubAgent:
    """Drives the presenter like the real agent would, without any models."""

    def __init__(
        self, presenter: WebPresenter, *, fail: bool = False, delay: float = 0.0,
        status: AgentStatus = AgentStatus.PASSED,
    ) -> None:
        self.presenter = presenter
        self.fail = fail
        self.delay = delay
        self.status = status

    def run(self, pdf_path, output_dir, source_provenance=None, run_identity=None):
        self.presenter.run_start(pdf_path)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("model exploded")
        self.presenter.phase_change("SCAN")
        self.presenter.tool_start(1, "inspect_file", "", {})
        self.presenter.tool_result("inspect_file", {"success": True, "page_count": 1, "size_mb": 0.1, "format": "PDF"}, 5)
        return AgentState(pdf_path=pdf_path, output_dir=output_dir, status=self.status, turn=1)


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
    assert "Impendo" in res.text and "entreculturas-logo.svg" in res.text
    # The browser must revalidate the UI files so an updated app.js is picked up.
    assert res.headers["cache-control"] == "no-cache"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"


# --- Results view (flagged PDF) ----------------------------------------------------


def _flagged_outputs(out_dir: str) -> dict[str, str]:
    """What write_results returns for a traced run: a flagged PDF plus a trace with its layout."""
    from pathlib import Path

    from src.trace.pdf_writer import render_flagged_pdf
    from tests.test_trace_flagged_layout import sample_trace, write_source_pdf

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    trace = sample_trace()
    pdf = out / "flagged_inv.pdf"
    trace["flagged_pdf"] = render_flagged_pdf(trace, write_source_pdf(out / "src.pdf"), str(pdf))
    (out / "trace_inv.json").write_text(json.dumps(trace), encoding="utf-8")
    return {"trace_json": str(out / "trace_inv.json"), "flagged_pdf": str(pdf)}


def test_result_view_scales_boxes_to_the_page_image():
    from src.trace.pdf_writer import MARGIN
    from src.ui.result_view import build_result_view
    from tests.test_trace_flagged_layout import sample_trace

    trace = sample_trace()
    trace["flagged_pdf"] = {"summary_pages": 1, "pages": [
        {"page": 1, "pdf_index": 1, "scan_w": 595.0, "scan_h": 842.0, "margin": MARGIN,
         "comments": [{"ref": "F1", "rule_id": "R_F1", "status": "failed", "text": "t", "rect": [601, 100, 617, 116]}]},
        {"page": 2, "pdf_index": 2, "scan_w": 595.0, "scan_h": 842.0, "margin": 0.0, "comments": []},
    ]}
    # An entry the visual check wrote under its own name is not a field of the type: hidden.
    trace["fields"].append({**trace["fields"][-1], "field_name": "AEXCID_stamp", "in_schema": False})
    view = build_result_view(trace, "r1")
    frac = 595.0 / (595.0 + MARGIN)
    assert [f["field_name"] for f in view["fields"]] == ["vendor_name", "total_amount", "vat_amount"]

    p1, p2 = view["pages"]
    assert p1["image_url"] == "/api/runs/r1/pages/1.jpg"
    assert p1["aspect"] == pytest.approx(842.0 / (595.0 + MARGIN))
    assert p2["aspect"] == pytest.approx(842.0 / 595.0)
    assert p1["comments"][0]["box"] == pytest.approx([601 / 745, 100 / 842, 617 / 745, 116 / 842])

    fields = {f["field_name"]: f for f in view["fields"]}
    assert fields["vendor_name"]["target"] == {"page": 1, "box": pytest.approx([0.1 * frac, 0.1, 0.4 * frac, 0.15])}
    assert fields["total_amount"]["target"] == {"page": 1, "box": None}  # value read, no box: page only
    assert fields["total_amount"]["display"] == "425 000"
    assert fields["vat_amount"]["target"] is None and fields["vat_amount"]["display"] == "not found"

    # Failed before warnings, passed left out; a missing item points at its expected spot.
    assert [f["ref"] for f in view["findings"]] == ["F1", "F2"]
    f1, f2 = view["findings"]
    assert f1["target"]["box"] == pytest.approx([0.5 * frac, 0.5, 0.7 * frac, 0.55])
    assert f2["target"]["box"] == pytest.approx([0.2 * frac, 0.8, 0.4 * frac, 0.85])
    assert f1["title"] == "Rule"  # English name from rule_summary (unknown names are humanized)
    assert f1["comment"].endswith("Rule R_F1")


def test_issues_are_shown_in_english_not_the_configs_spanish_message():
    from src.trace.pdf_writer import MARGIN
    from src.ui.result_view import build_result_view
    from tests.test_trace_flagged_layout import sample_trace

    trace = sample_trace()
    visual, field_rule = trace["findings"][0], trace["findings"][1]
    visual.update(
        rule_name="sanctions_fines_not_subsidized", check_type="visual_check",
        message="The document includes parking tickets, which are fines.",
        error_message="El documento parece ser una sanción o recargo, no subvencionable.",
        requirement="This document does not represent fines", visual={"confidence": 0.95},
    )
    field_rule.update(
        rule_name="fecha_requerida", check_type="required", field_names=["vendor_name"],
        message="Falta la fecha del documento de viaje. Campo obligatorio.",
        error_message="Falta la fecha del documento de viaje. Campo obligatorio.",
    )
    trace["flagged_pdf"] = {"summary_pages": 0, "pages": [
        {"page": 1, "pdf_index": 0, "scan_w": 595.0, "scan_h": 842.0, "margin": MARGIN,
         "comments": [{"ref": "F1", "rule_id": "R_F1", "status": "failed", "rect": [601, 100, 617, 116],
                       "text": "F1 [FAILED] El documento parece ser una sanción"}]},
    ]}
    view = build_result_view(trace, "r1")
    f1, f2 = view["findings"]
    assert (f1["title"], f1["reason"]) == ("No sanctions or fines", "The document includes parking tickets, which are fines.")
    assert "Requirement: This document does not represent fines" in f1["comment"]
    assert "Model confidence: 0.95" in f1["comment"]
    assert (f2["title"], f2["reason"]) == ("Document date present", "Not found in the document")
    assert "Fields: Supplier" in f2["comment"]

    pin = view["pages"][0]["comments"][0]
    assert pin["title"] == "F1 · No sanctions or fines"
    shown = " ".join([f1["title"], f1["reason"], f1["comment"], f2["title"], f2["reason"], f2["comment"], pin["text"]])
    assert "El documento" not in shown and "Falta" not in shown


def test_finished_run_serves_results_pages_and_pdf(tmp_path):
    presenter = WebPresenter()
    manager = RunManager(
        StubAgent(presenter), presenter, tmp_path,
        materialize=lambda path: [SimpleNamespace(local_pdf_path=path, provenance=None, run_identity=None)],
        write_results=lambda state, out_dir: _flagged_outputs(out_dir),
        page_render_scale=0.5,
    )
    client = TestClient(create_app(manager))
    run_id = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    complete = _read_sse(client, run_id)[-1]
    assert complete["type"] == "run_complete" and complete["has_result"] is True

    view = client.get(f"/api/runs/{run_id}/result").json()
    assert [p["page"] for p in view["pages"]] == [1, 2]  # summary pages are not shown
    img = client.get(view["pages"][0]["image_url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert img.content[:2] == b"\xff\xd8"
    assert client.get(f"/api/runs/{run_id}/pages/3.jpg").status_code == 404
    assert client.get(f"/api/runs/{run_id}/pages/x.jpg").status_code == 422

    pdf = client.get(view["pdf_url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-disposition"].startswith("inline")


def test_run_without_trace_has_no_results(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    run_id = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    assert _read_sse(client, run_id)[-1]["has_result"] is False
    assert client.get(f"/api/runs/{run_id}/result").status_code == 404
    assert client.get(f"/api/runs/{run_id}/pages/1.jpg").status_code == 404
    assert client.get(f"/api/runs/{run_id}/flagged.pdf").status_code == 404


def test_broken_results_view_does_not_fail_the_run(tmp_path):
    presenter = WebPresenter()
    manager = RunManager(
        StubAgent(presenter), presenter, tmp_path,
        materialize=lambda path: [SimpleNamespace(local_pdf_path=path, provenance=None, run_identity=None)],
        write_results=lambda state, out_dir: {"trace_json": f"{out_dir}/missing.json", "flagged_pdf": "x.pdf"},
    )
    client = TestClient(create_app(manager))
    run_id = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    complete = _read_sse(client, run_id)[-1]
    assert complete["type"] == "run_complete" and complete["has_result"] is False
    assert client.get(f"/api/runs/{run_id}").json()["state"] == "done"


# --- Dashboard: workflow statuses, saved runs, reviewer actions --------------------


def _upload(client: TestClient, name: str = "a.pdf", data: bytes = PDF_BYTES) -> str:
    run_id = client.post("/api/runs", files={"file": (name, data, "application/pdf")}).json()["run_id"]
    _read_sse(client, run_id)  # returns once the run has finished
    return run_id


def test_status_catalog_is_human_readable(tmp_path):
    from src.ui import statuses

    catalog = TestClient(create_app(_manager(tmp_path))).get("/api/statuses").json()
    ids = [s["id"] for s in catalog["statuses"]]
    assert ids == ["new", "processing", "needs_review", "returned", "finalized", "error"]
    labels = [s["label"] for s in catalog["statuses"]] + list(catalog["verdicts"].values())
    labels += [a["label"] for a in catalog["actions"].values()]
    assert all("_" not in label for label in labels)
    assert statuses.label("needs_review") == "Needs review"
    assert statuses.label("on_hold") == "On hold"  # an unknown id still reads as words


@pytest.mark.parametrize(
    ("verdict", "status"),
    [
        (AgentStatus.PASSED, "needs_review"),
        (AgentStatus.NEEDS_REVIEW, "needs_review"),
        (AgentStatus.FAILED, "returned"),
        (AgentStatus.ERROR, "error"),
        (AgentStatus.INTERRUPTED, "error"),
    ],
)
def test_agent_verdict_sets_the_workflow_status(tmp_path, verdict, status):
    client = TestClient(create_app(_manager(tmp_path, status=verdict)))
    run = client.get(f"/api/runs/{_upload(client)}").json()
    assert run["verdict"] == verdict.value
    assert run["status"] == status
    assert [h["status"] for h in run["history"]] == ["new", "processing", status]
    assert run["history"][-1]["by"] == "agent"


def test_agent_exception_sets_error_status(tmp_path):
    client = TestClient(create_app(_manager(tmp_path, fail=True)))
    run = client.get(f"/api/runs/{_upload(client)}").json()
    assert run["status"] == "error"
    assert "model exploded" in run["history"][-1]["note"]


def test_runs_survive_a_restart_with_their_timeline(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    run_id = _upload(client)
    events = _read_sse(client, run_id)

    reloaded = TestClient(create_app(_manager(tmp_path)))
    run = reloaded.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "needs_review" and run["state"] == "done"
    assert run["summary"]["turns"] == 1
    assert run["content_sha256"]
    assert _read_sse(reloaded, run_id) == events


def test_run_cut_off_by_a_restart_becomes_error(tmp_path):
    from src.ui.run_store import STOPPED_NOTE, load_all

    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    (run_dir / "a.pdf").write_bytes(PDF_BYTES)
    hub = RunHub("r1", "a.pdf", run_dir=run_dir)
    hub.set_status("processing", by="system")
    hub.publish("run_started", filename="a.pdf")

    (loaded,) = load_all(tmp_path)
    assert loaded.status == "error" and loaded.finished
    assert loaded.history[-1]["note"] == STOPPED_NOTE
    # The stream ends with an error event, so the browser stops waiting for it.
    assert [e["type"] for e in loaded.events_after(0)] == ["run_started", "run_error"]


def test_folder_from_before_the_dashboard_is_backfilled_from_its_trace(tmp_path):
    from src.ui.run_store import load_all

    old = tmp_path / "oldrun000001"
    (old / "result").mkdir(parents=True)
    (old / "inv.pdf").write_bytes(PDF_BYTES)
    trace = {
        "document": {"invoice_type_name": "Services"},
        "run": {"status": "failed", "turns": 4},
        "fields": [{"value": "ACME"}, {"value": None}],
        "findings": [
            {"status": "failed", "severity": "error"},
            {"status": "warning", "severity": "warning"},
            {"status": "passed", "severity": "error"},
            {"status": "not_evaluated", "severity": "warning"},
        ],
    }
    (old / "result" / "trace_inv.json").write_text(json.dumps(trace), encoding="utf-8")
    no_results = tmp_path / "oldrun000002"
    no_results.mkdir()
    (no_results / "inv.pdf").write_bytes(PDF_BYTES)
    (tmp_path / "not-a-run").mkdir()

    hubs = {h.run_id: h for h in load_all(tmp_path)}
    assert set(hubs) == {"oldrun000001", "oldrun000002"}
    h = hubs["oldrun000001"]
    assert (h.status, h.verdict, h.invoice_type) == ("returned", "failed", "Services")
    assert h.summary["blocking"] == 1 and h.summary["warnings"] == 1 and h.summary["fields_extracted"] == 1
    assert h.paths == {"trace_json": "result/trace_inv.json"}
    assert (old / "run.json").is_file()  # saved, so the next start reads it directly
    assert hubs["oldrun000002"].status == "error"
    assert h.content_sha256 == hubs["oldrun000002"].content_sha256


def test_invoice_list_has_one_entry_per_pdf_shown_by_its_latest_run(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    _upload(client, "a.pdf")
    other = _upload(client, "b.pdf", PDF_BYTES + b"other")
    latest = _upload(client, "a copy.pdf")  # same content as a.pdf: same invoice
    invoices = client.get("/api/invoices").json()
    assert [(i["run_id"], i["run_count"]) for i in invoices] == [(latest, 2), (other, 1)]
    assert invoices[0]["filename"] == "a_copy.pdf"
    assert len(client.get("/api/runs").json()) == 3  # every run is still kept


def test_confirm_finalizes_only_a_run_that_needs_review(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    run_id = _upload(client)
    res = client.post(f"/api/runs/{run_id}/actions/confirm")
    assert res.status_code == 200
    assert res.json()["status"] == "finalized"
    assert res.json()["history"][-1]["by"] == "reviewer"

    again = client.post(f"/api/runs/{run_id}/actions/confirm")
    assert again.status_code == 409 and "Finalized" in again.json()["detail"]
    assert client.post(f"/api/runs/{run_id}/actions/delete").status_code == 404
    assert client.post("/api/runs/nope/actions/confirm").status_code == 404


def test_reevaluate_starts_a_linked_run_of_the_same_pdf(tmp_path):
    client = TestClient(create_app(_manager(tmp_path, status=AgentStatus.ERROR)))
    run_id = _upload(client, "My Invoice.pdf")
    res = client.post(f"/api/runs/{run_id}/actions/reevaluate")
    assert res.status_code == 200
    new = res.json()
    assert new["run_id"] != run_id and new["reevaluated_from"] == run_id
    assert new["filename"] == "My_Invoice.pdf"
    assert client.get(f"/api/runs/{run_id}").json()["reevaluated_to"] == new["run_id"]
    assert _read_sse(client, new["run_id"])[-1]["type"] == "run_complete"
    # The invoice stays one entry, now showing the new run.
    assert [i["run_id"] for i in client.get("/api/invoices").json()] == [new["run_id"]]


def test_reevaluate_is_refused_while_the_run_is_still_going(tmp_path):
    client = TestClient(create_app(_manager(tmp_path, delay=0.5)))
    run_id = client.post("/api/runs", files={"file": ("a.pdf", PDF_BYTES, "application/pdf")}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/actions/reevaluate").status_code == 409
    _read_sse(client, run_id)


def test_reloaded_run_serves_its_results_view(tmp_path):
    def manager() -> RunManager:
        presenter = WebPresenter()
        return RunManager(
            StubAgent(presenter), presenter, tmp_path,
            materialize=lambda path: [SimpleNamespace(local_pdf_path=path, provenance=None, run_identity=None)],
            write_results=lambda state, out_dir: _flagged_outputs(out_dir),
            page_render_scale=0.5,
        )

    client = TestClient(create_app(manager()))
    run_id = _upload(client)
    assert client.get(f"/api/runs/{run_id}").json()["has_result"] is True

    reloaded = TestClient(create_app(manager()))
    view = reloaded.get(f"/api/runs/{run_id}/result").json()
    assert [p["page"] for p in view["pages"]] == [1, 2]
    assert reloaded.get(view["pages"][0]["image_url"]).status_code == 200
    assert reloaded.get(view["pdf_url"]).content.startswith(b"%PDF")


# --- Reviewer corrections of extracted fields ---------------------------------------


def _traced_manager(tmp_path) -> RunManager:
    presenter = WebPresenter()
    return RunManager(
        StubAgent(presenter), presenter, tmp_path,
        materialize=lambda path: [SimpleNamespace(local_pdf_path=path, provenance=None, run_identity=None)],
        write_results=lambda state, out_dir: _flagged_outputs(out_dir),
        page_render_scale=0.5,
    )


def _fields(client: TestClient, run_id: str) -> dict[str, dict]:
    return {f["field_name"]: f for f in client.get(f"/api/runs/{run_id}/result").json()["fields"]}


def test_reviewer_edits_a_field_and_it_is_kept_after_a_restart(tmp_path):
    client = TestClient(create_app(_traced_manager(tmp_path)))
    run_id = _upload(client)

    res = client.put(f"/api/runs/{run_id}/fields/vendor_name", json={"value": "  ACME Logistics S.L. "})
    assert res.status_code == 200
    field = res.json()
    assert field["display"] == "ACME Logistics S.L."
    assert field["edit"]["original"] == "ACME" and field["edit"]["by"] == "reviewer"
    assert field["target"] is not None  # still points where the AI read it

    # A field the AI did not find can be filled in too.
    client.put(f"/api/runs/{run_id}/fields/vat_amount", json={"value": "89 250"})

    reloaded = TestClient(create_app(_traced_manager(tmp_path)))
    fields = _fields(reloaded, run_id)
    assert fields["vendor_name"]["display"] == "ACME Logistics S.L."
    assert fields["vat_amount"]["display"] == "89 250" and fields["vat_amount"]["edit"]["original"] == "not found"
    assert "edit" not in fields["total_amount"]

    undo = reloaded.delete(f"/api/runs/{run_id}/fields/vendor_name")
    assert undo.status_code == 200 and undo.json()["display"] == "ACME" and "edit" not in undo.json()
    assert "edit" not in _fields(reloaded, run_id)["vendor_name"]


def test_fields_are_locked_once_the_run_is_finalized(tmp_path):
    client = TestClient(create_app(_traced_manager(tmp_path)))
    run_id = _upload(client)
    client.post(f"/api/runs/{run_id}/actions/confirm")
    res = client.put(f"/api/runs/{run_id}/fields/vendor_name", json={"value": "X"})
    assert res.status_code == 409 and "Finalized" in res.json()["detail"]
    assert "edit" not in _fields(client, run_id)["vendor_name"]


def test_field_edit_rejects_unknown_fields_runs_and_bad_bodies(tmp_path):
    client = TestClient(create_app(_traced_manager(tmp_path)))
    run_id = _upload(client)
    assert client.put(f"/api/runs/{run_id}/fields/not_a_field", json={"value": "X"}).status_code == 404
    assert client.put("/api/runs/nope/fields/vendor_name", json={"value": "X"}).status_code == 404
    assert client.put(f"/api/runs/{run_id}/fields/vendor_name", json={}).status_code == 422
    assert client.put(f"/api/runs/{run_id}/fields/vendor_name", json={"value": "x" * 2001}).status_code == 422

    no_results = TestClient(create_app(_manager(tmp_path / "other")))
    plain = _upload(no_results)
    assert no_results.put(f"/api/runs/{plain}/fields/vendor_name", json={"value": "X"}).status_code == 404


def test_return_is_a_page_preview_the_server_refuses(tmp_path):
    client = TestClient(create_app(_manager(tmp_path)))
    action = client.get("/api/statuses").json()["actions"]["return"]
    assert (action["label"], action["from"], action["preview"]) == ("Return", ["needs_review"], True)

    run_id = _upload(client)
    res = client.post(f"/api/runs/{run_id}/actions/return")
    assert res.status_code == 409 and "not connected" in res.json()["detail"]
    run = client.get(f"/api/runs/{run_id}").json()
    assert run["status"] == "needs_review" and len(run["history"]) == 3  # nothing changed


def test_status_catalog_says_which_statuses_allow_edits(tmp_path):
    statuses = TestClient(create_app(_manager(tmp_path))).get("/api/statuses").json()["statuses"]
    assert [s["id"] for s in statuses if s["editable"]] == ["needs_review", "returned"]
