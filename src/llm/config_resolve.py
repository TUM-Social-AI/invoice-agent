"""Resolve LLM backend and model names from unified config."""

from __future__ import annotations

import os
from typing import Any


def llm_provider_name(config: dict) -> str:
    return str((config.get("llm") or {}).get("provider", "ollama")).strip().lower()


def ollama_base_url(config: dict) -> str:
    return str((config.get("ollama") or {}).get("base_url", "http://localhost:11434")).rstrip("/")


def ollama_num_ctx_chat(config: dict) -> int | None:
    """Optional Ollama chat context cap (tokens)."""
    raw = (config.get("ollama") or {}).get("num_ctx_chat")
    if raw is None:
        return None
    return int(raw)


def ollama_num_ctx_generate(config: dict) -> int | None:
    """Optional Ollama generate/vision context cap (tokens)."""
    o = config.get("ollama") or {}
    raw = o.get("num_ctx_generate", o.get("num_ctx_vision"))
    if raw is None:
        return None
    return int(raw)


def reasoning_model_for_config(config: dict) -> str:
    prov = llm_provider_name(config)
    if prov == "gemini":
        g = config.get("gemini") or {}
        return str(g.get("reasoning_model") or "gemini-2.5-flash")
    if prov == "openai":
        o = config.get("openai") or {}
        return str(o.get("reasoning_model") or "gpt-4.1-mini")
    return str((config.get("ollama") or {}).get("reasoning_model", "qwen2.5:7b"))


def vision_model_for_config(config: dict) -> str:
    prov = llm_provider_name(config)
    if prov == "gemini":
        g = config.get("gemini") or {}
        return str(g.get("vision_model") or g.get("reasoning_model") or "gemini-2.5-flash")
    if prov == "openai":
        o = config.get("openai") or {}
        return str(o.get("vision_model") or o.get("reasoning_model") or "gpt-4.1")
    return str((config.get("ollama") or {}).get("vision_model", "qwen2.5-vl:7b"))


def gemini_api_key(config: dict) -> str:
    g = config.get("gemini") or {}
    env_name = str(g.get("api_key_env", "GOOGLE_API_KEY")).strip() or "GOOGLE_API_KEY"
    key = (os.environ.get(env_name) or "").strip()
    if not key:
        raise RuntimeError(
            f"llm.provider is 'gemini' but environment variable {env_name!r} is empty or unset"
        )
    return key


def openai_api_key(config: dict) -> str:
    o = config.get("openai") or {}
    env_name = str(o.get("api_key_env", "OPENAI_API_KEY")).strip() or "OPENAI_API_KEY"
    key = (os.environ.get(env_name) or "").strip()
    if not key:
        raise RuntimeError(
            f"llm.provider is 'openai' but environment variable {env_name!r} is empty or unset"
        )
    return key


def openai_base_url(config: dict) -> str | None:
    o = config.get("openai") or {}
    raw = o.get("base_url")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def llm_timeouts(config: dict) -> dict[str, int]:
    """
    HTTP timeouts for chat (reasoning loop) and generate (vision) calls.

    Precedence for Gemini/OpenAI: provider.* > llm.* > ollama.* > defaults.
    Precedence for Ollama: ollama.* > llm.* > defaults.
    """
    llm = config.get("llm") or {}
    o = config.get("ollama") or {}
    g = config.get("gemini") or {}
    oa = config.get("openai") or {}
    prov = llm_provider_name(config)
    def_chat, def_gen = 240, 420

    if prov in ("gemini", "openai"):
        pblock = g if prov == "gemini" else oa
        chat = pblock.get("timeout_chat_s")
        gen = pblock.get("timeout_generate_s")
        if chat is None:
            chat = llm.get("timeout_chat_s")
        if gen is None:
            gen = llm.get("timeout_generate_s")
        if chat is None:
            chat = o.get("timeout_chat_s")
        if gen is None:
            gen = o.get("timeout_generate_s")
        return {
            "chat_timeout_s": int(chat) if chat is not None else def_chat,
            "generate_timeout_s": int(gen) if gen is not None else def_gen,
        }
    chat = o.get("timeout_chat_s")
    gen = o.get("timeout_generate_s")
    if chat is None:
        chat = llm.get("timeout_chat_s")
    if gen is None:
        gen = llm.get("timeout_generate_s")
    return {
        "chat_timeout_s": int(chat) if chat is not None else def_chat,
        "generate_timeout_s": int(gen) if gen is not None else def_gen,
    }


def effective_prompt_profile(config: dict) -> str:
    """Return 'local' or 'remote' for prompt sizing defaults."""
    a = config.get("agent") or {}
    p = str(a.get("prompt_profile", "auto")).strip().lower()
    if p == "local":
        return "local"
    if p == "remote":
        return "remote"
    # auto
    name = llm_provider_name(config)
    return "remote" if name not in ("ollama", "", "local") else "local"


_PROFILE_LOCAL = {
    "history_preview_chars": 300,
    "learnings_max_chars": 8000,
    "planning_learnings_max_chars": 800,
}
_PROFILE_REMOTE = {
    "history_preview_chars": 900,
    "learnings_max_chars": 14000,
    "planning_learnings_max_chars": 1600,
}


def prompt_limits_for_config(config: dict) -> dict[str, int]:
    """
    Effective prompt/history/learnings caps. Explicit agent.* values override profile defaults.
    Use YAML `null` or omit a key to take the profile default for that field.
    """
    a = config.get("agent") or {}
    prof = effective_prompt_profile(config)
    base = _PROFILE_LOCAL if prof == "local" else _PROFILE_REMOTE
    out: dict[str, int] = {}
    for key in _PROFILE_LOCAL:
        v = a.get(key)
        if v is None:
            out[key] = base[key]
        else:
            out[key] = int(v)
    return out


def remote_guard_config(config: dict) -> dict[str, Any]:
    """Optional per-run limits for remote LLM providers (see MeteredLLMProvider)."""
    llm = config.get("llm") or {}
    prov = llm_provider_name(config)
    out = dict(llm.get("remote_guard") or {})
    if prov == "gemini":
        out.update(dict((config.get("gemini") or {}).get("remote_guard") or {}))
    elif prov == "openai":
        out.update(dict((config.get("openai") or {}).get("remote_guard") or {}))
    return out


def active_rule_groups_from_config(config: dict) -> list[str]:
    """
    Which compliance rule_groups apply to this run.
    Default: general + xunta_galicia (full rulebook, backward-compatible).
    Set agent.active_rule_groups: [general] to skip Xunta-only stamp/year/PR811A rules for non-Galicia documents.
    """
    a = config.get("agent") or {}
    raw = a.get("active_rule_groups")
    if raw is None:
        return ["general", "xunta_galicia"]
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    out = [str(x).strip().lower() for x in raw if str(x).strip()]
    return out if out else ["general", "xunta_galicia"]


def fallback_reasoning_model_for_config(config: dict) -> str | None:
    fb = config.get("agent", {}).get("fallback_reasoning_model")
    if fb is None:
        return None
    s = str(fb).strip()
    return s or None
