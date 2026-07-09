"""OpenAI Chat Completions via openai SDK — matches LLMProvider protocol."""

from __future__ import annotations

import logging
import random
import time
from typing import Any

try:
    from openai import OpenAI, RateLimitError  # type: ignore
except ModuleNotFoundError:  # pragma: no cover
    OpenAI = None  # type: ignore
    RateLimitError = Exception  # type: ignore

from src.llm.base import LLMResult
from src.llm.config_resolve import openai_api_key, openai_base_url
from src.llm.response_format import clean_json_text, parse_json_result, to_openai_response_format

logger = logging.getLogger(__name__)

_RETRY_BASE_S = 2.0
_RETRY_MAX_S = 60.0
_RETRY_ATTEMPTS = 5

# o-series models (o1, o3, o4, …) reject the temperature parameter.
def _is_o_series(model: str) -> bool:
    return bool(model) and model.split("-")[0].lower() in ("o1", "o3", "o4")


def _call_with_backoff(fn, *args, **kwargs):
    """Call fn(*args, **kwargs), retrying on RateLimitError with exponential + jitter backoff."""
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except RateLimitError as e:
            if attempt == _RETRY_ATTEMPTS - 1:
                raise
            wait = min(_RETRY_BASE_S * (2 ** attempt) + random.uniform(0, 1), _RETRY_MAX_S)
            logger.warning("OpenAI rate limit hit (attempt %d/%d) — retrying in %.1fs", attempt + 1, _RETRY_ATTEMPTS, wait)
            time.sleep(wait)


def _response_to_raw(response: Any) -> Any:
    if hasattr(response, "model_dump"):
        return response.model_dump()
    return response


def _extract_message_content(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    return str(content or "").strip()


class OpenAIProvider:
    """OpenAI API (API key) via official openai SDK."""

    provider_name = "openai"

    def __init__(self, api_key: str, *, base_url: str | None = None):
        if OpenAI is None:  # pragma: no cover
            raise RuntimeError(
                "openai package is not installed. Install it via `pip install openai` "
                "(or `pip install -r requirements.txt`)."
            )
        kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self._client = OpenAI(**kwargs)

    @classmethod
    def from_config(cls, config: dict) -> OpenAIProvider:
        return cls(openai_api_key(config), base_url=openai_base_url(config))

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict],
        response_format: Any = None,
        temperature: float = 0.2,
        timeout_s: int = 120,
    ) -> LLMResult:
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "timeout": float(timeout_s),
        }
        if not _is_o_series(model):
            kwargs["temperature"] = temperature
        rf = to_openai_response_format(response_format)
        if rf is not None:
            kwargs["response_format"] = rf
        raw_response = _call_with_backoff(self._client.chat.completions.create, **kwargs)
        text = clean_json_text(_extract_message_content(raw_response))
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
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for b64 in images_b64 or []:
            if not b64:
                continue
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                }
            )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "timeout": float(timeout_s),
        }
        if not _is_o_series(model):
            kwargs["temperature"] = temperature
        rf = to_openai_response_format(response_format)
        if rf is not None:
            kwargs["response_format"] = rf
        raw_response = _call_with_backoff(self._client.chat.completions.create, **kwargs)
        text = clean_json_text(_extract_message_content(raw_response))
        return LLMResult(
            content_text=text,
            content_json=parse_json_result(text),
            raw=_response_to_raw(raw_response),
            model=model,
            provider=self.provider_name,
        )
