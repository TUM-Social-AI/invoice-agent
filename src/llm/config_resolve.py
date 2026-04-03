"""Resolve LLM backend and model names from unified config."""

from __future__ import annotations

import os
from typing import Any


def llm_provider_name(config: dict) -> str:
    return str((config.get("llm") or {}).get("provider", "ollama")).strip().lower()


def ollama_base_url(config: dict) -> str:
    return str((config.get("ollama") or {}).get("base_url", "http://localhost:11434")).rstrip("/")


def reasoning_model_for_config(config: dict) -> str:
    if llm_provider_name(config) == "gemini":
        g = config.get("gemini") or {}
        return str(g.get("reasoning_model") or "gemini-2.0-flash")
    return str((config.get("ollama") or {}).get("reasoning_model", "qwen2.5:7b"))


def vision_model_for_config(config: dict) -> str:
    if llm_provider_name(config) == "gemini":
        g = config.get("gemini") or {}
        return str(g.get("vision_model") or g.get("reasoning_model") or "gemini-2.0-flash")
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


def llm_timeouts(config: dict) -> dict[str, int]:
    """
    HTTP timeouts for chat (reasoning loop) and generate (vision) calls.

    Precedence for Gemini: gemini.* > llm.* > ollama.* > defaults.
    Precedence for Ollama: ollama.* > llm.* > defaults.
    """
    llm = config.get("llm") or {}
    o = config.get("ollama") or {}
    g = config.get("gemini") or {}
    prov = llm_provider_name(config)
    def_chat, def_gen = 240, 420

    if prov == "gemini":
        chat = g.get("timeout_chat_s")
        gen = g.get("timeout_generate_s")
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
    """Optional per-run limits for non-local LLM providers (see MeteredLLMProvider). Gemini overrides llm."""
    llm = config.get("llm") or {}
    gem = config.get("gemini") or {}
    out = dict(llm.get("remote_guard") or {})
    out.update(dict(gem.get("remote_guard") or {}))
    return out


def fallback_reasoning_model_for_config(config: dict) -> str | None:
    fb = config.get("agent", {}).get("fallback_reasoning_model")
    if fb is None:
        return None
    s = str(fb).strip()
    return s or None


def messages_to_gemini_body(
    messages: list[dict[str, Any]],
    *,
    json_mode: bool,
    temperature: float,
) -> dict[str, Any]:
    """Map OpenAI-style messages to Gemini generateContent JSON body."""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for m in messages:
        role = str(m.get("role", "")).strip().lower()
        text = m.get("content")
        if not isinstance(text, str):
            text = str(text or "")
        if role == "system":
            system_parts.append(text)
        elif role == "user":
            contents.append({"role": "user", "parts": [{"text": text}]})
        elif role == "assistant":
            contents.append({"role": "model", "parts": [{"text": text}]})

    body: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": temperature,
        },
    }
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    return body


def extract_gemini_text(response_json: dict[str, Any]) -> str:
    cands = response_json.get("candidates") or []
    if not cands:
        return ""
    parts = ((cands[0].get("content") or {}).get("parts")) or []
    chunks: list[str] = []
    for p in parts:
        if isinstance(p, dict) and "text" in p:
            chunks.append(str(p["text"] or ""))
    return "".join(chunks).strip()
