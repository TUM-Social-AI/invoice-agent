from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class LLMResult:
    content_text: str
    content_json: dict | None
    raw: Any
    model: str
    provider: str


class LLMProvider(Protocol):
    provider_name: str

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: Any = None,
        temperature: float = 0.2,
        timeout_s: int = 120,
    ) -> LLMResult:
        ...

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
        ...
