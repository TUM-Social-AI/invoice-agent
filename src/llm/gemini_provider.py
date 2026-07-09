"""Google Gemini via google-genai SDK — matches LLMProvider protocol."""

from __future__ import annotations

import base64
import binascii
from typing import Any

try:
    from google import genai  # type: ignore
    from google.genai import types  # type: ignore
except (ModuleNotFoundError, ImportError):  # pragma: no cover
    genai = None  # type: ignore
    types = None  # type: ignore

from src.llm.base import LLMResult
from src.llm.config_resolve import gemini_api_key
from src.llm.response_format import clean_json_text, gemini_generation_config_kwargs, parse_json_result


def _normalize_model_id(model: str) -> str:
    m = model.strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    return m


def _messages_to_gemini_contents(messages: list[dict]) -> tuple[list[types.Content], str | None]:
    """Map OpenAI-style messages to Gemini contents + optional system instruction."""
    system_parts: list[str] = []
    contents: list[types.Content] = []
    for m in messages:
        role = str(m.get("role", "")).strip().lower()
        text = m.get("content")
        if not isinstance(text, str):
            text = str(text or "")
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        elif role == "assistant":
            contents.append(types.Content(role="model", parts=[types.Part.from_text(text=text)]))
    system_instruction = "\n\n".join(system_parts) if system_parts else None
    return contents, system_instruction


def _extract_response_text(response: types.GenerateContentResponse) -> str:
    text = getattr(response, "text", None)
    if text:
        return str(text).strip()
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return ""
    content = getattr(candidates[0], "content", None)
    parts = getattr(content, "parts", None) or []
    chunks: list[str] = []
    for part in parts:
        t = getattr(part, "text", None)
        if t:
            chunks.append(str(t))
    return "".join(chunks).strip()


def _response_to_raw(response: types.GenerateContentResponse) -> Any:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return response


class GeminiProvider:
    """Gemini Developer API (API key) via google-genai SDK."""

    provider_name = "gemini"

    def __init__(self, api_key: str):
        if genai is None or types is None:  # pragma: no cover
            raise RuntimeError(
                "google-genai package is not installed. Install it via `pip install google-genai` "
                "(or `pip install -r requirements.txt`)."
            )
        self._client = genai.Client(api_key=api_key)

    @classmethod
    def from_config(cls, config: dict) -> GeminiProvider:
        return cls(gemini_api_key(config))

    def _generate(
        self,
        *,
        model: str,
        contents: list[types.Content],
        response_format: Any,
        temperature: float,
        timeout_s: float,
        system_instruction: str | None = None,
    ) -> types.GenerateContentResponse:
        cfg_kwargs = gemini_generation_config_kwargs(
            response_format=response_format,
            temperature=temperature,
            timeout_s=timeout_s,
        )
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        config = types.GenerateContentConfig(**cfg_kwargs)
        return self._client.models.generate_content(
            model=_normalize_model_id(model),
            contents=contents,
            config=config,
        )

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: Any = None,
        temperature: float = 0.2,
        timeout_s: int = 120,
    ) -> LLMResult:
        contents, system_instruction = _messages_to_gemini_contents(messages)
        raw_response = self._generate(
            model=model,
            contents=contents,
            response_format=response_format,
            temperature=temperature,
            timeout_s=float(timeout_s),
            system_instruction=system_instruction,
        )
        text = clean_json_text(_extract_response_text(raw_response))
        return LLMResult(
            content_text=text,
            content_json=parse_json_result(text),
            raw=_response_to_raw(raw_response),
            model=model,
            provider=self.provider_name,
        )

    def generate_json(
        self,
        *,
        model: str,
        prompt: str,
        images_b64: list[str] | None = None,
        temperature: float = 0.1,
        timeout_s: int = 240,
        response_format: Any = None,
    ) -> LLMResult:
        parts: list[types.Part] = [types.Part.from_text(text=prompt)]
        for b64 in images_b64 or []:
            if not b64:
                continue
            try:
                image_bytes = base64.b64decode(b64)
            except binascii.Error as e:
                raise ValueError(f"Invalid base64 image data: {e}") from e
            parts.append(
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type="image/jpeg",
                )
            )
        raw_response = self._generate(
            model=model,
            contents=[types.Content(role="user", parts=parts)],
            response_format=response_format,
            temperature=temperature,
            timeout_s=float(timeout_s),
        )
        text = clean_json_text(_extract_response_text(raw_response))
        return LLMResult(
            content_text=text,
            content_json=parse_json_result(text),
            raw=_response_to_raw(raw_response),
            model=model,
            provider=self.provider_name,
        )
