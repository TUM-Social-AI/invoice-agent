"""Offline tests for PERS_LOCAL backfill from visual observations."""

from src.agent.state import AgentState, FieldResult
from src.config.loader import load_config
from src.models.tool_io_models import VisualVerdictModel
from src.tools.compliance_visual import (
    _is_short_date_fragment,
    _merge_visual_field_updates,
    _parse_employee_name_from_visual_observation,
    _parse_payment_phrase_from_visual_observation,
)


def test_parse_employee_name_quotes():
    store = load_config("config/csv")
    assert (
        _parse_employee_name_from_visual_observation(
            "The employee name 'Ada Lovelace', role 'Relais communautaire'",
            store,
        )
        == "Ada Lovelace"
    )


def test_parse_employee_name_rejects_role_only():
    store = load_config("config/csv")
    assert _parse_employee_name_from_visual_observation("name 'Relais Communautaire' only", store) is None


def test_parse_payment_phrase():
    t = "Payment is evidenced by signed Payé par (Paid by) section on page 1."
    assert _parse_payment_phrase_from_visual_observation(t) and "Payé par" in _parse_payment_phrase_from_visual_observation(t)


def test_is_date_fragment():
    assert _is_short_date_fragment("11/1")
    assert not _is_short_date_fragment("Paid by Ing. X")


def test_visual_verdict_field_updates_coercion():
    v = VisualVerdictModel.model_validate(
        {"passes": True, "confidence": 0.9, "observation": "ok", "field_updates": {"employee_name": "  Ada L.  "}}
    )
    assert v.field_updates == {"employee_name": "Ada L."}


def test_merge_visual_field_updates_prefers_schema_keys():
    store = load_config("config/csv")
    state = AgentState(
        pdf_path="/tmp/x.pdf",
        invoice_type_id="PERS_LOCAL",
        output_dir="/tmp/out",
    )
    merged = _merge_visual_field_updates(
        state,
        store,
        "R_PL_011",
        1,
        {"employee_name": "FATI ALHADJI KOUMBOU", "unknown_field": "x"},
    )
    assert merged == ["employee_name"]
    assert state.extracted_fields["employee_name"].extracted_value == "FATI ALHADJI KOUMBOU"
    assert "unknown_field" not in state.extracted_fields


def test_config_loads_visual_fallback_and_denylist():
    store = load_config("config/csv")
    assert store.employee_name_role_denylist
    pl = store.observation_fallbacks_for("PERS_LOCAL")
    assert any(f.source_rule_id == "R_PL_011" and f.parser_kind == "employee_name_quote" for f in pl)


def test_merge_visual_field_updates_skips_when_already_set():
    store = load_config("config/csv")
    state = AgentState(
        pdf_path="/tmp/x.pdf",
        invoice_type_id="PERS_LOCAL",
        output_dir="/tmp/out",
    )
    state.extracted_fields["employee_name"] = FieldResult(
        field_id="PL_001",
        field_name="employee_name",
        extracted_value="Existing",
        confidence=0.9,
        source_page=1,
        source_region="extract",
    )
    assert _merge_visual_field_updates(state, store, "R_PL_011", 1, {"employee_name": "Other"}) == []
    assert state.extracted_fields["employee_name"].extracted_value == "Existing"
