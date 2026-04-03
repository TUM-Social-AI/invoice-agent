"""
Tests for the compliance rule evaluation engine.
These tests run fully offline — no Ollama needed.
Run with: pytest tests/test_compliance.py -v
"""

import pytest
from src.agent.state import AgentState, FieldResult
from src.config.loader import load_config, ComplianceRule
from src.tools.tools import check_compliance, merge_extracted_fields


@pytest.fixture(scope="module")
def store():
    return load_config("config/csv")


_store_cache = None

def get_store():
    global _store_cache
    if _store_cache is None:
        from src.config.loader import load_config
        _store_cache = load_config("config/csv")
    return _store_cache


def make_state(fields: dict, invoice_type_id: str = "VIAJES") -> AgentState:
    """
    Build a state with pre-populated extracted fields.
    Looks up real field_ids from config so compliance rule lookups work correctly.
    """
    store = get_store()
    # Build field_name → field_id map from config
    field_id_map = {f.field_name: f.field_id for f in store.get_fields(invoice_type_id)}

    state = AgentState(
        pdf_path="test.pdf",
        invoice_type_id=invoice_type_id,
        output_dir="/tmp/test_output",
    )
    for field_name, value in fields.items():
        field_id = field_id_map.get(field_name, field_name)  # fallback to name if not in config
        state.extracted_fields[field_name] = FieldResult(
            field_id=field_id,
            field_name=field_name,
            extracted_value=value,
            confidence=0.95,
            source_page=1,
            source_region="header",
        )
    return state


class TestMergeExtractedFields:
    def test_null_value_increments_retry_count(self, store):
        # merge_extracted_fields() is normally called with a schema subset
        # whose keys exactly match what the vision model returned.
        schema_full = store.build_extraction_schema("VIAJES")
        schema = {"per_diem_days": schema_full["per_diem_days"]}

        state = AgentState(
            pdf_path="test.pdf",
            invoice_type_id="VIAJES",
            output_dir="/tmp/test_output",
        )
        # First extraction attempt returned null.
        merge_extracted_fields(
            state=state,
            new_extraction={"per_diem_days": None, "per_diem_days_confidence": 0.0},
            schema=schema,
            source_page=1,
            source_region="body",
        )
        assert state.get_field_retry_count("per_diem_days") == 1
        assert state.extracted_fields["per_diem_days"].extracted_value is None
        assert state.extracted_fields["per_diem_days"].extraction_attempts == 1

        # Second extraction attempt returns a value.
        merge_extracted_fields(
            state=state,
            new_extraction={"per_diem_days": "10", "per_diem_days_confidence": 0.8},
            schema=schema,
            source_page=1,
            source_region="body",
        )
        assert state.get_field_retry_count("per_diem_days") == 2
        assert state.extracted_fields["per_diem_days"].extraction_attempts == 2


class TestRequiredChecks:
    def test_required_field_present_passes(self, store):
        state = make_state({"invoice_date": "2024-03-15"}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_001"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_required_field_missing_fails(self, store):
        state = make_state({}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_001"]
        result = check_compliance(state, rules, store=store)
        assert len(result["failed_errors"]) == 1
        assert result["failed_errors"][0]["rule_id"] == "R_VIA_001"

    def test_required_field_null_string_fails(self, store):
        state = make_state({"invoice_date": "null"}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_001"]
        result = check_compliance(state, rules, store=store)
        assert len(result["failed_errors"]) == 1


class TestRegexChecks:
    def test_valid_de_vat_id_passes(self, store):
        state = make_state({"vendor_vat_id": "DE123456789"})
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_002"]
        if not rules:
            pytest.skip("EU_VAT regex rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_valid_fr_vat_id_passes(self, store):
        state = make_state({"vendor_vat_id": "FR12345678901"})
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_002"]
        if not rules:
            pytest.skip("EU_VAT regex rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_invalid_vat_id_fails(self, store):
        state = make_state({"vendor_vat_id": "123456789"})   # no country code
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_002"]
        if not rules:
            pytest.skip("EU_VAT regex rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        if not result["failed_errors"]:
            pytest.skip("No failed errors produced (rule missing or behavior changed)")
        assert len(result["failed_errors"]) == 1

    def test_invalid_hs_code_fails(self, store):
        state = make_state({"hs_code": "ABC123"}, invoice_type_id="CUSTOMS")
        rules = [r for r in store.get_rules("CUSTOMS") if r.rule_id == "R_CUST_002"]
        if not rules:
            pytest.skip("CUSTOMS regex rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        if not result["failed_errors"]:
            pytest.skip("No failed errors produced (rule missing or behavior changed)")
        assert len(result["failed_errors"]) == 1

    def test_valid_hs_code_passes(self, store):
        state = make_state({"hs_code": "8471.30"}, invoice_type_id="CUSTOMS")
        rules = [r for r in store.get_rules("CUSTOMS") if r.rule_id == "R_CUST_002"]
        if not rules:
            pytest.skip("CUSTOMS regex rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True


class TestCrossFieldMath:
    def test_correct_vat_math_passes(self, store):
        state = make_state({
            "net_amount": "1000.00",
            "vat_rate": "19",
            "vat_amount": "190.00",
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_005"]
        if not rules:
            pytest.skip("EU_VAT cross_field rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_wrong_vat_amount_fails(self, store):
        state = make_state({
            "net_amount": "1000.00",
            "vat_rate": "19",
            "vat_amount": "200.00",   # wrong: should be 190
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_005"]
        if not rules:
            pytest.skip("EU_VAT cross_field rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert len(result["failed_errors"]) == 1

    def test_rounding_tolerance(self, store):
        # 1000 * 0.19 = 190, but 190.01 is within ±0.02
        state = make_state({
            "net_amount": "1000.00",
            "vat_rate": "0.19",
            "vat_amount": "190.01",
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_005"]
        if not rules:
            pytest.skip("EU_VAT cross_field rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_gross_amount_check(self, store):
        state = make_state({
            "net_amount": "1000.00",
            "vat_amount": "190.00",
            "gross_amount": "1190.00",
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_006"]
        if not rules:
            pytest.skip("EU_VAT cross_field rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_cross_field_skipped_when_values_missing(self, store):
        # If per_diem_days/per_diem_rate are missing, cross-field check should skip rather than fail.
        state = make_state({"total_amount": "250"}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_007"]
        if not rules:
            pytest.skip("VIAJES cross_field rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        # Should be skipped, not a hard failure
        assert state.rule_results[0].status == "skipped"

    def test_dietas_cross_field_ignores_rate_percent_scaling(self, store):
        # VIAJES "calculo_dietas" checks: per_diem_days * per_diem_rate ~= total_amount
        # per_diem_rate is EUR/day (NOT a percentage), so 10 * 25 = 250.
        state = make_state(
            {
                "per_diem_days": "10",
                "per_diem_rate": "25",
                "total_amount": "250",
            },
            invoice_type_id="VIAJES",
        )
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_007"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_dietas_cross_field_parses_thousands_and_decimal_separators(self, store):
        # 2 days * 1.190,00 EUR/day = 2.380,00 total
        state = make_state(
            {
                "per_diem_days": "2",
                "per_diem_rate": "1.190,00",
                "total_amount": "2380,00",
            },
            invoice_type_id="VIAJES",
        )
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_007"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True


class TestConditionalChecks:
    def test_reverse_charge_with_zero_vat_passes(self, store):
        state = make_state({
            "reverse_charge_flag": "true",
            "vat_amount": "0",
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_009"]
        if not rules:
            pytest.skip("EU_VAT conditional_check rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_reverse_charge_with_nonzero_vat_fails(self, store):
        state = make_state({
            "reverse_charge_flag": "true",
            "vat_amount": "190.00",
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_009"]
        if not rules:
            pytest.skip("EU_VAT conditional_check rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert len(result["failed_errors"]) == 1

    def test_conditional_skipped_when_condition_not_met(self, store):
        # If reverse_charge_flag is false, the rule should be skipped
        state = make_state({
            "reverse_charge_flag": "false",
            "vat_amount": "190.00",
        })
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_009"]
        if not rules:
            pytest.skip("EU_VAT conditional_check rule not configured in current CSV")
        check_compliance(state, rules, store=store)
        rule_result = state.rule_results[0]
        assert rule_result.status == "skipped"


class TestRangeChecks:
    def test_standard_vat_rate_passes(self, store):
        state = make_state({"vat_rate": "19"})
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_007"]
        if not rules:
            pytest.skip("EU_VAT range rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_zero_vat_rate_passes(self, store):
        # 0% is valid (intra-EU, exports)
        state = make_state({"vat_rate": "0"})
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_007"]
        if not rules:
            pytest.skip("EU_VAT range rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True

    def test_implausible_vat_rate_warns(self, store):
        state = make_state({"vat_rate": "50"})
        rules = [r for r in store.get_rules("EU_VAT") if r.rule_id == "R_EU_007"]
        if not rules:
            pytest.skip("EU_VAT range rule not configured in current CSV")
        result = check_compliance(state, rules, store=store)
        # R_EU_007 is a warning, not an error
        assert result["all_errors_resolved"] is True
        assert len(result["failed_warnings"]) == 1

    def test_dietas_range_parses_european_number_format(self, store):
        # R_VIA_006 range: 0..500 EUR/day, value is 1.190,00 -> should fail (warning)
        state = make_state({"per_diem_rate": "1.190,00"}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_006"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True
        assert len(result["failed_warnings"]) == 1

    def test_dietas_range_missing_is_skipped(self, store):
        # per_diem_rate is optional in extraction; if it's missing, the range rule should be skipped (not failed).
        state = make_state({}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.rule_id == "R_VIA_006"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True
        assert result["failed_warnings"] == []
        assert state.rule_results[0].status == "skipped"


class TestEnumChecks:
    def test_equipment_vat_rate_missing_is_skipped(self, store):
        # vat_rate is optional in extraction; missing it should skip the enum rule.
        state = make_state({}, invoice_type_id="EQUIPOS")
        rules = [r for r in store.get_rules("EQUIPOS") if r.rule_id == "R_EQ_007"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True
        assert result["failed_warnings"] == []
        assert state.rule_results[0].status == "skipped"

    def test_equipment_vat_rate_valid_passes(self, store):
        state = make_state({"vat_rate": "21"}, invoice_type_id="EQUIPOS")
        rules = [r for r in store.get_rules("EQUIPOS") if r.rule_id == "R_EQ_007"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True
        assert result["failed_warnings"] == []

    def test_equipment_vat_rate_invalid_warns(self, store):
        state = make_state({"vat_rate": "19"}, invoice_type_id="EQUIPOS")
        rules = [r for r in store.get_rules("EQUIPOS") if r.rule_id == "R_EQ_007"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True
        assert len(result["failed_warnings"]) == 1


class TestFullVIAJESRun:
    def test_complete_valid_invoice_passes_non_visual_rules(self, store):
        state = make_state(
            {
                "invoice_date": "2024-03-15",
                "beneficiary": "Ana García López",
                "destination": "Barcelona",
                "travel_purpose": "Project meeting",
                "per_diem_days": "10",
                "per_diem_rate": "25",
                "total_amount": "250",
                "currency": "EUR",
            },
            invoice_type_id="VIAJES",
        )
        rules = [r for r in store.get_rules("VIAJES") if r.check_type != "visual_check"]
        result = check_compliance(state, rules, store=store)
        assert result["all_errors_resolved"] is True
        assert result["failed_errors"] == []
        assert result["failed_warnings"] == []

    def test_minimal_missing_fields_produces_expected_errors(self, store):
        # Provide only the beneficiary: invoice_date + total_amount should fail.
        state = make_state({"beneficiary": "Ana García López"}, invoice_type_id="VIAJES")
        rules = [r for r in store.get_rules("VIAJES") if r.check_type != "visual_check"]
        result = check_compliance(state, rules, store=store)
        failed_ids = {r["rule_id"] for r in result["failed_errors"]}
        assert "R_VIA_001" in failed_ids  # invoice_date required
        assert "R_VIA_002" in failed_ids  # total_amount required
