"""Guards against repeated identical check_compliance calls."""

from src.agent.agent import _phase_tool_names
from src.agent.state import AgentState, RuleResult


def test_validate_phase_removes_check_compliance_after_same_result_streak():
    state = AgentState(
        pdf_path="x.pdf",
        output_dir="/tmp/out",
        invoice_type_id="PERS_LOCAL",
    )
    state.rule_results = [
        RuleResult(
            rule_id="R1",
            rule_name="r",
            field_id="F",
            status="passed",
            severity="error",
            message="ok",
        )
    ]
    state.page_image_paths = ["/full/page1.jpg"]
    state.compressed_page_paths = ["/tmp/thumb1.jpg"]
    state.compliance_same_result_streak = 2

    all_tools = sorted(
        [
            "check_compliance",
            "check_compliance_visual",
            "extract_fields_vision",
            "finish",
            "note",
        ]
    )
    allowed = _phase_tool_names(state, all_tools)
    assert "check_compliance" not in allowed
    assert "finish" in allowed
    assert "extract_fields_vision" in allowed


def test_validate_phase_allows_check_compliance_when_streak_low():
    state = AgentState(
        pdf_path="x.pdf",
        output_dir="/tmp/out",
        invoice_type_id="PERS_LOCAL",
    )
    state.rule_results = [
        RuleResult(
            rule_id="R1",
            rule_name="r",
            field_id="F",
            status="passed",
            severity="error",
            message="ok",
        )
    ]
    state.page_image_paths = ["/full/page1.jpg"]
    state.compressed_page_paths = ["/tmp/thumb1.jpg"]
    state.compliance_same_result_streak = 1

    all_tools = sorted(["check_compliance", "finish"])
    allowed = _phase_tool_names(state, all_tools)
    assert "check_compliance" in allowed
