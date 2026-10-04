"""Classify-only pipeline: stops once the document type is known."""

import io

from src.agent.pipeline import run_fixed_pipeline
from src.agent.state import AgentState, AgentStatus


ALL_TOOLS = [
    "inspect_file",
    "compress_pages",
    "inventory_pages",
    "classify_document_type",
    "convert_pdf_to_images",
    "extract_fields_vision",
    "check_compliance",
    "check_compliance_visual",
    "finish",
]


def _build_tools(called: list[str], *, classify_sets_type: bool = True) -> dict:
    """Stub every pipeline tool, recording call order."""

    def _stub(name: str):
        def _call(state: AgentState, **kwargs):
            called.append(name)
            if name == "classify_document_type" and classify_sets_type:
                state.invoice_type_id = "VIAJES"
                state.invoice_type_confidence = 0.91
                state.invoice_type_reasoning = "boarding pass visible"
            if name == "convert_pdf_to_images":
                state.page_image_paths = ["page_001.jpg"]
            return {"success": True}

        return _call

    return {name: _stub(name) for name in ALL_TOOLS}


def _run(classify_only: bool, **kwargs) -> tuple[AgentState, list[str]]:
    state = AgentState(pdf_path="invoice.pdf", output_dir="/tmp/classify-only-test")
    called: list[str] = []
    run_fixed_pipeline(
        state=state,
        tools=_build_tools(called, **kwargs),
        log_handle=io.StringIO(),
        log_line_max_chars=0,
        planning_enabled=False,
        generate_plan_fn=None,
        classify_only=classify_only,
    )
    return state, called


def test_classify_only_stops_after_classification():
    state, called = _run(classify_only=True)

    assert called == ["inspect_file", "compress_pages", "classify_document_type"]
    assert state.status == AgentStatus.CLASSIFIED
    assert state.invoice_type_id == "VIAJES"
    assert "VIAJES" in state.finish_reason
    assert "0.91" in state.finish_reason


def test_classify_only_skips_inventory_extraction_and_compliance():
    _, called = _run(classify_only=True)

    for skipped in (
        "inventory_pages",
        "convert_pdf_to_images",
        "extract_fields_vision",
        "check_compliance",
        "check_compliance_visual",
        "finish",
    ):
        assert skipped not in called


def test_classify_only_errors_when_type_undetermined():
    state, _ = _run(classify_only=True, classify_sets_type=False)

    assert state.status == AgentStatus.ERROR
    assert "no document type" in state.finish_reason


def test_full_pipeline_still_runs_every_stage():
    _, called = _run(classify_only=False)

    assert "inventory_pages" in called
    assert "convert_pdf_to_images" in called
    assert "check_compliance" in called
