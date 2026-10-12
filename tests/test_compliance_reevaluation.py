"""Field-based rules must be re-evaluated on every check_compliance call."""

from src.agent.state import AgentState, FieldResult
from src.config.loader import load_config
from src.tools.tools import check_compliance


def _rule(rules, rule_id):
    return next(r for r in rules if r.rule_id == rule_id)


def test_required_rule_flips_to_passed_after_field_is_extracted():
    store = load_config("config/csv")
    rules = store.get_rules("VIAJES", ["general"])
    rule = _rule(rules, "R_VIA_001")
    field = next(f for f in store.get_fields("VIAJES") if f.field_id == rule.field_id)

    state = AgentState(pdf_path="test.pdf", output_dir="/tmp/test_output", invoice_type_id="VIAJES")
    check_compliance(state, rules, store=store)
    first = next(r for r in state.rule_results if r.rule_id == "R_VIA_001")
    assert first.status == "failed"

    state.extracted_fields[field.field_name] = FieldResult(
        field_id=field.field_id,
        field_name=field.field_name,
        extracted_value="2025-03-05",
        confidence=0.95,
        source_page=1,
        source_region="header",
    )
    check_compliance(state, rules, store=store)
    second = next(r for r in state.rule_results if r.rule_id == "R_VIA_001")
    assert second.status == "passed"
    assert "R_VIA_001" not in state.failed_rules
