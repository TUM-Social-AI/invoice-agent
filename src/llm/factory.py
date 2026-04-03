"""Construct LLMProvider from config."""

from __future__ import annotations

from src.llm.base import LLMProvider
from src.llm.config_resolve import llm_provider_name, ollama_base_url, remote_guard_config
from src.llm.gemini_provider import GeminiProvider
from src.llm.metered_provider import MeteredLLMProvider, remote_guard_is_active
from src.llm.ollama_provider import OllamaProvider


def build_llm_provider(config: dict) -> LLMProvider:
    name = llm_provider_name(config)
    if name in ("ollama", "", "local"):
        return OllamaProvider(ollama_base_url(config))
    if name == "gemini":
        inner: LLMProvider = GeminiProvider.from_config(config)
        guard = remote_guard_config(config)
        if remote_guard_is_active(guard):
            return MeteredLLMProvider(inner, guard)
        return inner
    raise ValueError(
        f"Unsupported llm.provider {name!r}. Use 'ollama' or 'gemini', or extend build_llm_provider."
    )
