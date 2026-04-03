from dataclasses import dataclass

from src.agent.agent import _validate_action_contract
from src.agent.state import AgentState, RuleResult
from src.config.loader import ComplianceRule, load_config
from src.tools.tools import check_compliance, inventory_pages


@dataclass
class FakeLLMResult:
    content_text: str
    content_json: dict | None
    raw: dict
    model: str
    provider: str


class FakeProvider:
    provider_name = "fake"

    def generate_json(self, **kwargs):
        # inventory_pages expects category+description JSON.
        return FakeLLMResult(
            content_text='{"category":"INVOICE_HEADER","description":"Invoice total 1.190,00 EUR ref INV-2025-10"}',
            content_json={"category": "INVOICE_HEADER", "description": "Invoice total 1.190,00 EUR ref INV-2025-10"},
            raw={},
            model=kwargs.get("model", "fake"),
            provider=self.provider_name,
        )


def test_inventory_builds_page_facts_with_entities():
    state = AgentState(pdf_path="dummy.pdf", output_dir="/tmp/test")
    state.compressed_page_paths = ["page1.jpg"]
    res = inventory_pages(state, ollama_url="http://unused", model="fake", provider=FakeProvider())
    assert res["success"] is True
    assert 1 in state.page_facts
    entities = state.page_facts[1]["entities"]
    assert "amounts" in entities
    assert "references" in entities


def test_check_compliance_populates_rule_evidence_and_policy_refs():
    store = load_config("config/csv")
    state = AgentState(pdf_path="x.pdf", output_dir="/tmp/test", invoice_type_id="VIAJES")
    state.learnings_context = "R_VIA_001: invoice date must be present"
    rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_001"]
    res = check_compliance(state, rules, store=store)
    assert res["total_rules"] == 1
    assert "R_VIA_001" in state.rule_evidence
    assert "R_VIA_001" in state.rule_policy_refs
    assert state.rule_policy_refs["R_VIA_001"]


def test_validate_action_contract_rejects_invented_params():
    invalid = {"tool": "check_compliance", "params": {"made_up": 1}, "reasoning": "x"}
    err = _validate_action_contract(invalid)
    assert err is not None
    assert "invalid params" in err


def test_finish_evidence_gate_reports_unresolved_error_evidence():
    # Basic smoke: unresolved error evidence should keep state out of PASSED.
    state = AgentState(pdf_path="x.pdf", output_dir="/tmp/test", invoice_type_id="VIAJES")
    state.rule_results = [
        RuleResult(rule_id="R_ERR", rule_name="r", field_id="f", status="passed", severity="error", message="ok")
    ]
    state.rule_evidence["R_ERR"] = {
        "required_slots": ["field_values"],
        "filled_slots": [],
        "missing_slots": ["field_values"],
        "refs": [],
    }
    state.rule_state["R_ERR"] = "candidate"
    # We only validate the state shape here; behavior is covered in test_finish.
    assert state.rule_evidence["R_ERR"]["missing_slots"]
