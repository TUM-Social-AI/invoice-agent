"""
Tests for the config loader.
Run with: pytest tests/test_config.py -v
"""

import pytest
from src.config.loader import load_config


@pytest.fixture(scope="module")
def store():
    return load_config("config/csv")


def test_invoice_types_loaded(store):
    assert len(store.invoice_types) > 0
    assert "VIAJES" in store.invoice_types
    assert "EQUIPOS" in store.invoice_types


def test_extraction_fields_loaded(store):
    fields = store.get_fields("VIAJES")
    assert len(fields) > 0
    names = [f.field_name for f in fields]
    assert "vendor_name" in names
    assert "total_amount" in names


def test_field_aliases_parsed(store):
    fields = store.get_fields("VIAJES")
    vendor_field = next(f for f in fields if f.field_name == "vendor_name")
    assert len(vendor_field.aliases) > 0
    assert "Hotel" in vendor_field.aliases


def test_compliance_rules_loaded(store):
    rules = store.get_rules("VIAJES")
    assert len(rules) > 0
    rule_ids = [r.rule_id for r in rules]
    assert "R_VIA_001" in rule_ids
    assert "R_VIA_007" in rule_ids  # cross-field math check


def test_rule_group_column_loaded(store):
    via = store.get_rules("VIAJES")
    r009 = next(r for r in via if r.rule_id == "R_VIA_009")
    assert r009.rule_group == "xunta_galicia"
    r001 = next(r for r in via if r.rule_id == "R_VIA_001")
    assert r001.rule_group == "general"


def test_get_rules_respects_active_rule_groups(store):
    all_via = store.get_rules("VIAJES", None)
    gen_only = store.get_rules("VIAJES", ["general"])
    assert len(gen_only) < len(all_via)
    gen_ids = {r.rule_id for r in gen_only}
    assert "R_VIA_001" in gen_ids
    assert "R_VIA_009" not in gen_ids
    xunta_ids = {r.rule_id for r in store.get_rules("VIAJES", ["xunta_galicia"])}
    assert "R_VIA_009" in xunta_ids
    assert "R_VIA_001" not in xunta_ids


def test_build_extraction_schema(store):
    schema = store.build_extraction_schema("VIAJES")
    assert "vendor_name" in schema
    assert "total_amount" in schema
    # Each field has hint and aliases
    assert "hint" in schema["vendor_name"]
    assert "aliases" in schema["vendor_name"]


def test_build_agent_context(store):
    context = store.build_agent_context("VIAJES")
    assert "Viajes" in context
    assert "vendor_name" in context
    assert "R_VIA_001" in context
    assert "No clasificar como VIAJES" in context
    assert "procurement" in context


def test_procurement_type_contexts_cover_payment_voucher_packets(store):
    equipment_context = store.build_agent_context("EQUIPOS")
    consumables_context = store.build_agent_context("CONSUMIBLES")

    assert "payment voucher" in equipment_context
    assert "supplier quotation" in equipment_context
    assert "wheat flour" in consumables_context
    assert "payment vouchers" in consumables_context


def test_total_and_currency_field_hints_cover_payment_and_birr(store):
    via_fields = {field.field_name: field for field in store.get_fields("VIAJES")}
    consumable_fields = {field.field_name: field for field in store.get_fields("CONSUMIBLES")}

    assert "Cheque Amount" in via_fields["total_amount"].aliases
    assert "ETB" in via_fields["currency"].aliases
    assert "Birr" in via_fields["currency"].extraction_hint
    assert "Amount Paid" in consumable_fields["total_amount"].aliases


def test_disabled_type_not_loaded(store):
    # All types in the CSV with enabled=false should not appear
    for tid, t in store.invoice_types.items():
        assert t.enabled is True


def test_unknown_type_returns_empty(store):
    assert store.get_fields("NONEXISTENT") == []
    assert store.get_rules("NONEXISTENT") == []
    assert store.get_type("NONEXISTENT") is None


def test_aexcid_types_loaded_with_budget_lines(store):
    lines = {tid: t.budget_line for tid, t in store.invoice_types.items()}
    assert lines == {
        "VIAJES": "A.6",
        "PERS_LOCAL": "A.5.a",
        "PERS_SEDE": "A.5.b",
        "EQUIPOS": "A.4.a",
        "CONSUMIBLES": "A.4.c",
        "VOLUNTARIOS": "A.5.d",
        "SERV_TECNICOS": "A.7",
        "FUNCIONAMIENTO": "A.8",
    }
    for t in store.invoice_types.values():
        assert t.display_name.startswith(t.budget_line + " ")


def test_new_types_have_fields_and_required_rules(store):
    for type_id, prefix in (("VOLUNTARIOS", "VOL_"), ("SERV_TECNICOS", "SRV_"), ("FUNCIONAMIENTO", "FUN_")):
        fields = store.get_fields(type_id)
        assert fields and all(f.field_id.startswith(prefix) for f in fields)
        field_ids = {f.field_id for f in fields}
        rules = store.get_rules(type_id, ["general"])
        assert rules
        for r in rules:
            if r.check_type == "visual_check":
                continue  # visual rules (aexcid rule set) point at VISUAL, not a field
            assert r.check_type in ("required", "range", "enum")
            assert r.field_id in field_ids, f"{r.rule_id} points at a field of another type"


def test_service_and_operating_fields_cover_supplier_and_client(store):
    for type_id in ("SERV_TECNICOS", "FUNCIONAMIENTO", "EQUIPOS", "CONSUMIBLES"):
        names = {f.field_name for f in store.get_fields(type_id)}
        assert {"vendor_name", "vendor_tax_id", "beneficiary", "total_amount", "currency"} <= names, type_id


def test_volunteer_fields_reuse_payroll_names_without_salary_fields(store):
    names = {f.field_name for f in store.get_fields("VOLUNTARIOS")}
    assert {"employee_name", "pay_period", "role", "total_amount", "payment_method"} <= names
    assert not names & {"gross_salary", "net_salary", "irpf_retention", "social_security_employee"}
    rule_fields = {
        store.get_field_by_id(r.field_id).field_name
        for r in store.get_rules("VOLUNTARIOS")
        if r.check_type != "visual_check"
    }
    assert not rule_fields & {"gross_salary", "net_salary", "irpf_retention"}


def test_expense_category_allowed_values_per_type(store):
    def cats(type_id):
        return next(f.allowed_values for f in store.get_fields(type_id) if f.field_name == "expense_category")

    assert cats("VOLUNTARIOS") == ["personal_voluntario"]
    assert "personal_voluntario" not in cats("PERS_LOCAL")
    assert cats("SERV_TECNICOS") == ["repair_maintenance", "professional_services", "other_services"]
    assert cats("FUNCIONAMIENTO") == ["utilities", "communications", "rent", "other_operating"]
    assert "consulting" not in cats("EQUIPOS")


def test_descriptions_route_repairs_away_from_goods_types(store):
    assert "repair" in store.get_type("SERV_TECNICOS").description.lower()
    for type_id in ("EQUIPOS", "CONSUMIBLES"):
        assert "SERV_TECNICOS" in store.get_type(type_id).description
    assert "groupe électrogène" in store.get_type("FUNCIONAMIENTO").description
    assert "relais communautaire" in store.get_type("VOLUNTARIOS").description


def test_budget_line_column_is_optional(tmp_path):
    (tmp_path / "invoice_types.csv").write_text(
        "invoice_type_id,display_name,description,agent_context,enabled\nX,X,x,x,true\n", encoding="utf-8"
    )
    (tmp_path / "extraction_fields.csv").write_text(
        "field_id,invoice_type_id,field_name,field_label,data_type,required,extraction_hint,page_region,aliases\n",
        encoding="utf-8",
    )
    (tmp_path / "compliance_rules.csv").write_text(
        "rule_id,invoice_type_id,rule_name,field_id,check_type,check_value,severity,agent_hint,error_message,"
        "page_region,enabled,rule_group\n",
        encoding="utf-8",
    )
    assert load_config(str(tmp_path)).get_type("X").budget_line == ""
