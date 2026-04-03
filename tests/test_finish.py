"""
Tests for deterministic finish() behavior.

The agent calls finish() with an LLM-provided all_errors_resolved flag, but
finish() must derive its own decision from state.rule_results + visual pending.
"""

from src.agent.agent import build_tool_registry
from src.agent.state import AgentState, AgentStatus, FieldResult, RuleResult
from src.config.loader import load_config


def _make_tools():
    store = load_config("config/csv")
    # Minimal config needed to construct the tool registry.
    config = {
        "ollama": {
            "base_url": "http://localhost:11434",
            "vision_model": "qwen2.5vl:latest",
            "reasoning_model": "qwen3:1.7b",
        },
        "ocr": {"langs": ["es", "en"]},
    }
    return build_tool_registry(config=config, store=store, surya_models=None)


def test_finish_rejects_when_visual_checks_pending():
    tools = _make_tools()
    finish = tools["finish"]

    state = AgentState(pdf_path="test.pdf", output_dir="/tmp/test", invoice_type_id="VIAJES")
    state.visual_checks_pending = ["R_VIA_009"]
    state.rule_results = []

    res = finish(state, reason="done", all_errors_resolved=True)
    assert res["finished"] is False
    assert "visual checks" in res["error"].lower()


def test_finish_warning_failures_still_pass():
    tools = _make_tools()
    finish = tools["finish"]

    state = AgentState(pdf_path="test.pdf", output_dir="/tmp/test", invoice_type_id="VIAJES")
    state.visual_checks_pending = []
    state.rule_results = [
        RuleResult(
            rule_id="R_WARN_001",
            rule_name="warn_rule",
            field_id="F_001",
            status="failed",
            severity="warning",
            message="warning only",
        )
    ]

    res1 = finish(state, reason="done", all_errors_resolved=False)
    res2 = finish(state, reason="done", all_errors_resolved=True)
    assert res1["finished"] is True
    assert res2["finished"] is True
    assert res1["status"] == AgentStatus.PASSED.value
    assert res2["status"] == AgentStatus.PASSED.value


def test_finish_error_failures_mark_failed():
    tools = _make_tools()
    finish = tools["finish"]

    state = AgentState(pdf_path="test.pdf", output_dir="/tmp/test", invoice_type_id="VIAJES")
    state.visual_checks_pending = []
    state.rule_results = [
        RuleResult(
            rule_id="R_ERR_001",
            rule_name="error_rule",
            field_id="F_001",
            status="failed",
            severity="error",
            message="error only",
        )
    ]

    res = finish(state, reason="done", all_errors_resolved=False)
    assert res["finished"] is True
    assert res["status"] == AgentStatus.FAILED.value


def test_finish_sets_needs_review_when_any_field_is_flagged():
    tools = _make_tools()
    finish = tools["finish"]

    state = AgentState(pdf_path="test.pdf", output_dir="/tmp/test", invoice_type_id="VIAJES")
    state.visual_checks_pending = []
    state.extracted_fields["vendor_name"] = FieldResult(
        field_id="VIA_001",
        field_name="vendor_name",
        extracted_value=None,
        confidence=0.0,
        source_page=None,
        source_region=None,
        flagged_for_review=True,
        review_reason="model couldn't read vendor name",
    )
    state.rule_results = [
        RuleResult(
            rule_id="R_ERR_001",
            rule_name="error_rule",
            field_id="F_001",
            status="failed",
            severity="error",
            message="error only",
        )
    ]

    res = finish(state, reason="done", all_errors_resolved=False)
    assert res["finished"] is True
    assert res["status"] == AgentStatus.NEEDS_REVIEW.value

