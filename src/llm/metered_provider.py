"""Optional per-run metering and limits for remote LLM providers."""

from __future__ import annotations

import logging
from typing import Any

from src.llm.base import LLMProvider, LLMResult

logger = logging.getLogger(__name__)


def _usage_total_tokens(raw: Any) -> int:
    if not isinstance(raw, dict):
        return 0
    um = raw.get("usageMetadata") or raw.get("usage_metadata")
    if not isinstance(um, dict):
        return 0
    for key in ("totalTokenCount", "total_token_count"):
        if um.get(key) is not None:
            try:
                return int(um[key])
            except (TypeError, ValueError):
                pass
    try:
        pt = int(um.get("promptTokenCount") or 0)
        ct = int(um.get("candidatesTokenCount") or 0)
        return pt + ct
    except (TypeError, ValueError):
        return 0


def remote_guard_is_active(guard: dict[str, Any]) -> bool:
    if not guard:
        return False
    keys = (
        "max_llm_requests_per_run",
        "max_chat_requests_per_run",
        "max_generate_requests_per_run",
        "max_total_token_count_per_run",
    )
    for k in keys:
        v = guard.get(k)
        if v is not None and int(v) > 0:
            return True
    w = guard.get("warn_token_threshold")
    return w is not None and int(w) > 0


class MeteredLLMProvider:
    """Wraps an LLMProvider with optional per-session counters and hard caps."""

    def __init__(self, inner: LLMProvider, guard: dict[str, Any]):
        self._inner = inner
        self._guard = dict(guard)
        self.provider_name = getattr(inner, "provider_name", "metered")
        self._chat_calls = 0
        self._generate_calls = 0
        self._cumulative_tokens = 0
        self._warned_tokens = False

    def reset_meters(self) -> None:
        self._chat_calls = 0
        self._generate_calls = 0
        self._cumulative_tokens = 0
        self._warned_tokens = False

    def _check_before_chat(self) -> None:
        g = self._guard
        m = g.get("max_llm_requests_per_run")
        if m is not None and int(m) > 0:
            if self._chat_calls + self._generate_calls >= int(m):
                raise RuntimeError(
                    f"LLM guard: max_llm_requests_per_run ({m}) reached — increase llm.remote_guard "
                    "or fix retry loops to avoid runaway API cost."
                )
        mc = g.get("max_chat_requests_per_run")
        if mc is not None and int(mc) > 0 and self._chat_calls >= int(mc):
            raise RuntimeError(
                f"LLM guard: max_chat_requests_per_run ({mc}) reached."
            )

    def _check_before_generate(self) -> None:
        g = self._guard
        m = g.get("max_llm_requests_per_run")
        if m is not None and int(m) > 0:
            if self._chat_calls + self._generate_calls >= int(m):
                raise RuntimeError(
                    f"LLM guard: max_llm_requests_per_run ({m}) reached — increase llm.remote_guard "
                    "or fix retry loops to avoid runaway API cost."
                )
        mg = g.get("max_generate_requests_per_run")
        if mg is not None and int(mg) > 0 and self._generate_calls >= int(mg):
            raise RuntimeError(
                f"LLM guard: max_generate_requests_per_run ({mg}) reached."
            )

    def _after_result(self, result: LLMResult) -> None:
        g = self._guard
        add = _usage_total_tokens(result.raw)
        if add:
            self._cumulative_tokens += add
        cap = g.get("max_total_token_count_per_run")
        if cap is not None and int(cap) > 0 and self._cumulative_tokens > int(cap):
            raise RuntimeError(
                f"LLM guard: max_total_token_count_per_run ({cap}) exceeded "
                f"(observed ~{self._cumulative_tokens})."
            )
        warn = g.get("warn_token_threshold")
        if (
            not self._warned_tokens
            and warn is not None
            and int(warn) > 0
            and self._cumulative_tokens >= int(warn)
        ):
            self._warned_tokens = True
            logger.warning(
                "LLM token use crossed warn_token_threshold=%s (cumulative ~%s)",
                warn,
                self._cumulative_tokens,
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
        self._check_before_chat()
        self._chat_calls += 1
        r = self._inner.chat_json(
            model=model,
            messages=messages,
            response_format=response_format,
            temperature=temperature,
            timeout_s=timeout_s,
        )
        self._after_result(r)
        return r

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
        self._check_before_generate()
        self._generate_calls += 1
        r = self._inner.generate_json(
            model=model,
            prompt=prompt,
            images_b64=images_b64,
            temperature=temperature,
            timeout_s=timeout_s,
            response_format=response_format,
        )
        self._after_result(r)
        return r
