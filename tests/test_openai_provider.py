"""Unit tests for OpenAIProvider (mocked openai SDK)."""

from unittest.mock import MagicMock, patch

import pytest

from src.agent.response_schema import build_response_schema
from src.agent.state import AgentState
from src.config.loader import ConfigStore
from src.llm.openai_provider import OpenAIProvider


def _fake_chat_response(content: str, total_tokens: int = 99) -> MagicMock:
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    resp = MagicMock()
    resp.choices = [choice]
    resp.model_dump.return_value = {
        "choices": [{"message": {"content": content}}],
        "usage": {"total_tokens": total_tokens},
    }
    return resp


@patch("src.llm.openai_provider.OpenAI")
def test_chat_json_uses_json_schema(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _fake_chat_response(
        '{"tool": "finish", "params": {}, "reasoning": "done"}'
    )

    provider = OpenAIProvider("sk-test")
    schema = {
        "type": "object",
        "properties": {"tool": {"type": "string"}},
        "required": ["tool"],
        "additionalProperties": False,
    }
    result = provider.chat_json(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "next?"}],
        response_format=schema,
        timeout_s=60,
    )

    assert result.content_json["tool"] == "finish"
    kwargs = mock_client.chat.completions.create.call_args.kwargs
    rf = kwargs["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"] == schema
    assert kwargs["timeout"] == 60.0


@patch("src.llm.openai_provider.OpenAI")
def test_chat_json_falls_back_to_json_object_for_agent_schema(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _fake_chat_response('{"tool": "finish"}')

    state = AgentState(
        pdf_path="dummy.pdf",
        output_dir="out",
        invoice_type_id="PERS_LOCAL",
        page_image_paths=["page1.jpg"],
    )
    schema = build_response_schema(state, ConfigStore(), tool_names=["finish", "note"])

    provider = OpenAIProvider("sk-test")
    provider.chat_json(
        model="gpt-4.1-mini",
        messages=[{"role": "user", "content": "next? Respond with JSON only."}],
        response_format=schema,
    )

    rf = mock_client.chat.completions.create.call_args.kwargs["response_format"]
    assert rf == {"type": "json_object"}


@patch("src.llm.openai_provider.OpenAI")
def test_generate_json_multimodal(mock_openai_cls):
    mock_client = MagicMock()
    mock_openai_cls.return_value = mock_client
    mock_client.chat.completions.create.return_value = _fake_chat_response('{"ok": true}')

    provider = OpenAIProvider("sk-test")
    provider.generate_json(
        model="gpt-4.1",
        prompt="read invoice",
        images_b64=["YWJj"],
        response_format={"type": "json_object"},
        timeout_s=180,
    )

    kwargs = mock_client.chat.completions.create.call_args.kwargs
    content = kwargs["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert "data:image/jpeg;base64,YWJj" in content[1]["image_url"]["url"]
    assert kwargs["response_format"] == {"type": "json_object"}


@patch("src.llm.openai_provider.OpenAI")
def test_from_config_with_base_url(mock_openai_cls, monkeypatch):
    mock_openai_cls.return_value = MagicMock()
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    cfg = {
        "openai": {
            "api_key_env": "OPENAI_API_KEY",
            "base_url": "https://example.com/v1",
        }
    }
    OpenAIProvider.from_config(cfg)
    mock_openai_cls.assert_called_once_with(
        api_key="secret",
        base_url="https://example.com/v1",
    )


def test_from_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        OpenAIProvider.from_config({"openai": {"api_key_env": "OPENAI_API_KEY"}})
