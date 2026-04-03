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


def test_disabled_type_not_loaded(store):
    # All types in the CSV with enabled=false should not appear
    for tid, t in store.invoice_types.items():
        assert t.enabled is True


def test_unknown_type_returns_empty(store):
    assert store.get_fields("NONEXISTENT") == []
    assert store.get_rules("NONEXISTENT") == []
    assert store.get_type("NONEXISTENT") is None
