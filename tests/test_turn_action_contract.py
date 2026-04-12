from src.agent.state import AgentState
from src.agent.turn import agent_turn
from src.config.loader import ConfigStore
from src.llm.base import LLMResult


class FakeProvider:
    provider_name = "fake"

    def __init__(self, action: dict):
        self._action = action

    def chat_json(self, **kwargs):
        return LLMResult(
            content_text="{}",
            content_json=self._action,
            raw={},
            model=kwargs.get("model", "fake"),
            provider=self.provider_name,
        )


def test_agent_turn_normalizes_page_num_and_field_subset():
    state = AgentState(
        pdf_path="dummy.pdf",
        output_dir="out",
        invoice_type_id="PERS_LOCAL",
        page_image_paths=["page1.jpg"],
        compressed_page_paths=["thumb1.jpg"],
    )
    store = ConfigStore()
    cfg = {
        "ollama": {"reasoning_model": "fake", "base_url": "http://unused"},
        "agent": {"history_preview_chars": 300, "learnings_inject_enabled": False},
    }
    provider = FakeProvider(
        {
            "tool": "extract_fields_vision",
            "params": {
                "page_num": "page 1",
                "field_subset": "employee_name",
            },
            "reasoning": "extract field from first page",
        }
    )

    action = agent_turn(
        state=state,
        store=store,
        config=cfg,
        tool_names=["extract_fields_vision", "check_compliance"],
        provider=provider,
        chat_timeout_s=5,
    )

    assert action["tool"] == "extract_fields_vision"
    assert action["params"]["page_num"] == 1
    assert action["params"]["field_subset"] == ["employee_name"]
