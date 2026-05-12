"""Google Gemini (Generative Language API) via HTTPS — matches LLMProvider protocol."""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from src.llm.base import LLMProvider, LLMResult
from src.llm.config_resolve import (
    extract_gemini_text,
    gemini_api_key,
    messages_to_gemini_body,
)


def _clean_json_text(raw: str) -> str:
    return re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()


def _normalize_model_id(model: str) -> str:
    m = model.strip()
    if m.startswith("models/"):
        m = m[len("models/") :]
    return m


class GeminiProvider:
    """Gemini Developer API (API key)."""

    provider_name = "gemini"

    GEMINI_GENERATE_URL = (
        "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    )

    def __init__(self, api_key: str):
        self._api_key = api_key

    @classmethod
    def from_config(cls, config: dict) -> GeminiProvider:
        return cls(gemini_api_key(config))

    def _post(self, model: str, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        mid = _normalize_model_id(model)
        url = self.GEMINI_GENERATE_URL.format(model=mid)
        # Use x-goog-api-key so the key never appears in the URL (urllib3 DEBUG logs the URL).
        resp = requests.post(
            url,
            headers={"x-goog-api-key": self._api_key},
            json=body,
            timeout=timeout_s,
        )
        resp.raise_for_status()
        return resp.json()

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: Any = None,
        temperature: float = 0.2,
        timeout_s: int = 120,
    ) -> LLMResult:
        json_mode = response_format is not None
        body = messages_to_gemini_body(
            messages,
            json_mode=json_mode,
            temperature=temperature,
        )
        raw_obj = self._post(model, body, float(timeout_s))
        text = _clean_json_text(extract_gemini_text(raw_obj))
        parsed = None
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
        return LLMResult(
            content_text=text,
            content_json=parsed,
            raw=raw_obj,
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
        parts: list[dict[str, Any]] = [{"text": prompt}]
        for b64 in images_b64 or []:
            if not b64:
                continue
            parts.append(
                {
                    "inline_data": {
                        "mime_type": "image/jpeg",
                        "data": b64,
                    }
                }
            )

        gen_cfg: dict[str, Any] = {"temperature": temperature}
        if response_format is not None:
            gen_cfg["responseMimeType"] = "application/json"
            if isinstance(response_format, dict):
                gen_cfg["responseSchema"] = response_format

        body: dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_cfg,
        }
        raw_obj = self._post(model, body, float(timeout_s))
        text = _clean_json_text(extract_gemini_text(raw_obj))
        parsed = None
        if text:
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
        return LLMResult(
            content_text=text,
            content_json=parsed,
            raw=raw_obj,
            model=model,
            provider=self.provider_name,
        )
