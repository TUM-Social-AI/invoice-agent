"""Construct LLMProvider from config."""

from __future__ import annotations

from src.llm.base import LLMProvider
from src.llm.config_resolve import (
    llm_provider_name,
    ollama_base_url,
    ollama_num_ctx_chat,
    ollama_num_ctx_generate,
    remote_guard_config,
)
from src.llm.gemini_provider import GeminiProvider
from src.llm.metered_provider import MeteredLLMProvider, remote_guard_is_active
from src.llm.ollama_provider import OllamaProvider
from src.llm.openai_provider import OpenAIProvider


def build_llm_provider(config: dict) -> LLMProvider:
    name = llm_provider_name(config)
    if name in ("ollama", "", "local"):
        return OllamaProvider(
            ollama_base_url(config),
            num_ctx_chat=ollama_num_ctx_chat(config),
            num_ctx_generate=ollama_num_ctx_generate(config),
        )
    if name == "gemini":
        inner: LLMProvider = GeminiProvider.from_config(config)
        guard = remote_guard_config(config)
        if remote_guard_is_active(guard):
            return MeteredLLMProvider(inner, guard)
        return inner
    if name == "openai":
        inner = OpenAIProvider.from_config(config)
        guard = remote_guard_config(config)
        if remote_guard_is_active(guard):
            return MeteredLLMProvider(inner, guard)
        return inner
    raise ValueError(
        f"Unsupported llm.provider {name!r}. Use 'ollama', 'gemini', or 'openai', "
        "or extend build_llm_provider."
    )
