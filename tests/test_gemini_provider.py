"""Unit tests for GeminiProvider (mocked google-genai SDK)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.llm.gemini_provider import GeminiProvider


def _fake_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.model_dump.return_value = {
        "candidates": [{"content": {"parts": [{"text": text}]}}],
        "usageMetadata": {"totalTokenCount": 42},
    }
    return resp


@patch("src.llm.gemini_provider.types")
@patch("src.llm.gemini_provider.genai")
def test_chat_json_passes_response_json_schema(mock_genai, mock_types):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_client.models.generate_content.return_value = _fake_response('{"tool": "finish"}')

    mock_types.GenerateContentConfig.return_value = MagicMock()
    mock_types.Part.from_text.return_value = MagicMock()
    mock_types.Content.side_effect = lambda role, parts: SimpleNamespace(parts=parts)

    provider = GeminiProvider("test-key")
    schema = {"type": "object", "properties": {"tool": {"type": "string"}}}
    result = provider.chat_json(
        model="gemini-2.5-flash",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "go"},
        ],
        response_format=schema,
        timeout_s=90,
    )

    assert result.content_json == {"tool": "finish"}
    assert result.provider == "gemini"

    assert mock_types.GenerateContentConfig.call_args.kwargs["response_mime_type"] == "application/json"
    assert mock_types.GenerateContentConfig.call_args.kwargs["response_json_schema"] == schema
    assert mock_types.GenerateContentConfig.call_args.kwargs["system_instruction"] == "sys"


@patch("src.llm.gemini_provider.types")
@patch("src.llm.gemini_provider.genai")
def test_generate_json_passes_gemini_native_schema(mock_genai, mock_types):
    mock_client = MagicMock()
    mock_genai.Client.return_value = mock_client
    mock_client.models.generate_content.return_value = _fake_response('{"a": 1}')

    mock_types.GenerateContentConfig.return_value = MagicMock()
    mock_types.Part.from_text.return_value = MagicMock()
    mock_types.Part.from_bytes.return_value = MagicMock()
    mock_types.Content.side_effect = lambda role, parts: SimpleNamespace(parts=parts)

    provider = GeminiProvider("test-key")
    schema = {"type": "OBJECT", "properties": {"a": {"type": "NUMBER"}}}
    provider.generate_json(
        model="gemini-2.5-pro",
        prompt="extract",
        images_b64=["aGVsbG8="],  # base64 "hello"
        response_format=schema,
        timeout_s=120,
    )

    assert mock_types.GenerateContentConfig.call_args.kwargs["response_schema"] == schema
    contents = mock_client.models.generate_content.call_args.kwargs["contents"]
    assert len(contents) == 1
    assert len(contents[0].parts) == 2


@patch("src.llm.gemini_provider.types")
@patch("src.llm.gemini_provider.genai")
def test_generate_json_invalid_base64_raises_value_error(mock_genai, mock_types):
    mock_genai.Client.return_value = MagicMock()
    mock_types.Part.from_text.return_value = MagicMock()
    mock_types.Content.side_effect = lambda role, parts: SimpleNamespace(parts=parts)

    provider = GeminiProvider("test-key")
    with pytest.raises(ValueError, match="Invalid base64 image data"):
        provider.generate_json(
            model="gemini-2.5-pro",
            prompt="extract",
            images_b64=["not-valid-base64!!!"],
        )


def test_from_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        GeminiProvider.from_config({"gemini": {"api_key_env": "GOOGLE_API_KEY"}})
