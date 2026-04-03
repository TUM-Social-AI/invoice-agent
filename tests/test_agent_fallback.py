from src.agent.agent import _fallback_action_after_llm_failure
from src.agent.state import AgentState


def test_fallback_runs_required_scan_step_first():
    state = AgentState(pdf_path="dummy.pdf", output_dir="out")
    action = _fallback_action_after_llm_failure(state)
    assert action is not None
    assert action["tool"] == "compress_pages"
    assert action["params"]["dpi"] == 48


def test_fallback_reduces_field_subset_after_extract_timeout():
    state = AgentState(
        pdf_path="dummy.pdf",
        output_dir="out",
        invoice_type_id="PERS_LOCAL",
        page_image_paths=["page1.jpg"],
        compressed_page_paths=["thumb1.jpg"],
    )
    state.record_action(
        "extract_fields_vision",
        {
            "page_num": 1,
            "region": "line_items",
            "field_subset": [
                "employee_name",
                "pay_period",
                "gross_salary",
                "payment_method",
                "net_salary",
                "role",
            ],
        },
        {
            "success": False,
            "fallback_fields": ["employee_name", "pay_period", "gross_salary"],
            "error": "Vision model timed out (>240s).",
        },
        "test timeout",
    )
    action = _fallback_action_after_llm_failure(state)
    assert action is not None
    assert action["tool"] == "extract_fields_vision"
    assert action["params"]["page_num"] == 1
    assert len(action["params"]["field_subset"]) <= 5


def test_fallback_skipped_when_backend_unreachable_404():
    state = AgentState(pdf_path="dummy.pdf", output_dir="out")
    err = "404 Client Error: Not Found for url: http://localhost:11434/api/chat"
    assert _fallback_action_after_llm_failure(state, err) is None


def test_fallback_recovers_from_action_contract_error_in_extract_phase():
    state = AgentState(
        pdf_path="dummy.pdf",
        output_dir="out",
        invoice_type_id="PERS_LOCAL",
        page_image_paths=["page1.jpg"],
        compressed_page_paths=["thumb1.jpg"],
    )
    action = _fallback_action_after_llm_failure(
        state,
        "Action contract invalid after repair retry: Tool 'extract_fields_vision' has invalid params: ['category', 'field_name']",
    )
    assert action is not None
    assert action["tool"] == "check_compliance"
