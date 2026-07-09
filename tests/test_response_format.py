"""Tests for shared response_format helpers."""

import pytest

from src.agent.response_schema import build_response_schema
from src.agent.state import AgentState
from src.config.loader import ConfigStore
from src.llm.config_resolve import (
    openai_api_key,
    reasoning_model_for_config,
    vision_model_for_config,
)
from src.llm.response_format import (
    gemini_generation_config_kwargs,
    normalize_response_format,
    provider_json_mode,
    to_openai_response_format,
)
from src.tools.vision_llm import _build_extraction_response_schema


def test_normalize_none():
    assert normalize_response_format(None) == ("none", None)


def test_normalize_json_string():
    assert normalize_response_format("json") == ("json", None)
    assert normalize_response_format("application/json") == ("json", None)


def test_normalize_schema_dict():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    mode, s = normalize_response_format(schema)
    assert mode == "schema"
    assert s == schema


def test_to_openai_json_object():
    assert to_openai_response_format("json") == {"type": "json_object"}


def test_to_openai_falls_back_for_incomplete_object_schema():
    schema = {"type": "object", "properties": {"tool": {"type": "string"}}}
    assert to_openai_response_format(schema) == {"type": "json_object"}


def test_to_openai_strict_schema_when_compatible():
    schema = {
        "type": "object",
        "properties": {"tool": {"type": "string"}},
        "required": ["tool"],
        "additionalProperties": False,
    }
    out = to_openai_response_format(schema)
    assert out["type"] == "json_schema"
    assert out["json_schema"]["strict"] is True
    assert out["json_schema"]["schema"] == schema


def test_to_openai_falls_back_for_agent_one_of_schema():
    state = AgentState(
        pdf_path="dummy.pdf",
        output_dir="out",
        invoice_type_id="PERS_LOCAL",
        page_image_paths=["page1.jpg"],
    )
    store = ConfigStore()
    schema = build_response_schema(state, store, tool_names=["finish", "note"])
    assert "oneOf" in schema
    assert to_openai_response_format(schema) == {"type": "json_object"}


def test_to_openai_falls_back_for_vision_extraction_schema():
    field_schema = {
        "employee_name": {
            "type": "string",
            "label": "Employee",
            "hint": "name",
            "required": True,
        }
    }
    schema = _build_extraction_response_schema(field_schema, "openai")
    assert schema is not None
    assert to_openai_response_format(schema) == {"type": "json_object"}


def test_gemini_chat_uses_response_json_schema():
    schema = {"type": "object", "properties": {"tool": {"type": "string"}}}
    cfg = gemini_generation_config_kwargs(
        response_format=schema,
        temperature=0.2,
        timeout_s=60,
    )
    assert cfg["response_mime_type"] == "application/json"
    assert cfg["response_json_schema"] == schema
    assert "response_schema" not in cfg


def test_gemini_falls_back_for_agent_one_of_schema():
    state = AgentState(
        pdf_path="dummy.pdf",
        output_dir="out",
        invoice_type_id="PERS_LOCAL",
        page_image_paths=["page1.jpg"],
    )
    store = ConfigStore()
    schema = build_response_schema(state, store, tool_names=["finish", "note"])
    cfg = gemini_generation_config_kwargs(
        response_format=schema,
        temperature=0.2,
        timeout_s=60,
    )
    assert cfg["response_mime_type"] == "application/json"
    assert "response_json_schema" not in cfg
    assert "response_schema" not in cfg


def test_gemini_vision_uses_response_schema():
    schema = {"type": "OBJECT", "properties": {"field": {"type": "STRING"}}}
    cfg = gemini_generation_config_kwargs(
        response_format=schema,
        temperature=0.1,
        timeout_s=120,
    )
    assert cfg["response_schema"] == schema
    assert "response_json_schema" not in cfg


def test_openai_model_resolution(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    cfg = {
        "llm": {"provider": "openai"},
        "openai": {
            "reasoning_model": "gpt-4.1-mini",
            "vision_model": "gpt-4.1",
            "api_key_env": "OPENAI_API_KEY",
        },
    }
    assert reasoning_model_for_config(cfg) == "gpt-4.1-mini"
    assert vision_model_for_config(cfg) == "gpt-4.1"
    assert openai_api_key(cfg) == "sk-test"


def test_openai_api_key_missing(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        openai_api_key({"openai": {"api_key_env": "OPENAI_API_KEY"}})


def test_provider_json_mode_by_provider():
    assert provider_json_mode("ollama") == "json"
    assert provider_json_mode("gemini") == "application/json"
    assert provider_json_mode("openai") == {"type": "json_object"}
