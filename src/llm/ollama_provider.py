from __future__ import annotations

import json
import re
from typing import Any

import requests

from src.llm.base import LLMProvider, LLMResult


def _clean_json_text(raw: str) -> str:
    return re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()


class OllamaProvider(LLMProvider):
    provider_name = "ollama"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: Any = None,
        temperature: float = 0.2,
        timeout_s: int = 120,
    ) -> LLMResult:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
        }
        if response_format is not None:
            payload["format"] = response_format

        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=timeout_s)
        resp.raise_for_status()
        raw_obj = resp.json()
        text = _clean_json_text(raw_obj.get("message", {}).get("content", ""))
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
        payload = {
            "model": model,
            "prompt": prompt,
            "images": images_b64 or [],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if response_format is not None:
            payload["format"] = response_format

        resp = requests.post(f"{self.base_url}/api/generate", json=payload, timeout=timeout_s)
        resp.raise_for_status()
        raw_obj = resp.json()
        text = _clean_json_text(raw_obj.get("response", ""))
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
