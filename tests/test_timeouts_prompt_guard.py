"""Unified LLM timeouts, prompt profiles, and metered remote guard."""

import pytest

from src.agent.loop_utils import timeout_cfg
from src.llm.config_resolve import (
    effective_prompt_profile,
    llm_timeouts,
    prompt_limits_for_config,
    remote_guard_config,
)
from src.llm.factory import build_llm_provider
from src.llm.metered_provider import MeteredLLMProvider, remote_guard_is_active


def test_llm_timeouts_gemini_precedence():
    cfg = {
        "llm": {"provider": "gemini", "timeout_chat_s": 111, "timeout_generate_s": 222},
        "gemini": {"timeout_chat_s": 999, "timeout_generate_s": 888},
        "ollama": {"timeout_chat_s": 1, "timeout_generate_s": 2},
    }
    t = llm_timeouts(cfg)
    assert t["chat_timeout_s"] == 999
    assert t["generate_timeout_s"] == 888


def test_llm_timeouts_gemini_falls_back_llm_then_ollama():
    cfg = {
        "llm": {"provider": "gemini", "timeout_chat_s": 50},
        "gemini": {},
        "ollama": {"timeout_generate_s": 77},
    }
    t = llm_timeouts(cfg)
    assert t["chat_timeout_s"] == 50
    assert t["generate_timeout_s"] == 77


def test_llm_timeouts_ollama_precedence():
    cfg = {
        "llm": {"provider": "ollama", "timeout_chat_s": 10, "timeout_generate_s": 20},
        "ollama": {"timeout_chat_s": 100, "timeout_generate_s": 200},
    }
    t = llm_timeouts(cfg)
    assert t["chat_timeout_s"] == 100
    assert t["generate_timeout_s"] == 200


def test_timeout_cfg_alias():
    cfg = {"llm": {"provider": "ollama"}, "ollama": {"timeout_chat_s": 55, "timeout_generate_s": 66}}
    assert timeout_cfg(cfg) == {"chat_timeout_s": 55, "generate_timeout_s": 66}


def test_effective_prompt_profile_auto():
    assert effective_prompt_profile({"llm": {"provider": "ollama"}, "agent": {}}) == "local"
    assert effective_prompt_profile({"llm": {"provider": "gemini"}, "agent": {}}) == "remote"


def test_prompt_limits_explicit_override():
    cfg = {
        "llm": {"provider": "ollama"},
        "agent": {
            "prompt_profile": "local",
            "history_preview_chars": 123,
            "learnings_max_chars": None,
            "planning_learnings_max_chars": None,
        },
    }
    pl = prompt_limits_for_config(cfg)
    assert pl["history_preview_chars"] == 123
    assert pl["learnings_max_chars"] == 8000
    assert pl["planning_learnings_max_chars"] == 800


def test_prompt_limits_remote_profile():
    cfg = {"llm": {"provider": "gemini"}, "agent": {"prompt_profile": "auto"}}
    pl = prompt_limits_for_config(cfg)
    assert pl["history_preview_chars"] == 900
    assert pl["learnings_max_chars"] == 14000
    assert pl["planning_learnings_max_chars"] == 1600


def test_remote_guard_config_merge():
    cfg = {
        "llm": {"remote_guard": {"max_llm_requests_per_run": 10, "warn_token_threshold": 1}},
        "gemini": {"remote_guard": {"max_llm_requests_per_run": 20}},
    }
    g = remote_guard_config(cfg)
    assert g["max_llm_requests_per_run"] == 20
    assert g["warn_token_threshold"] == 1


def test_remote_guard_is_active():
    assert remote_guard_is_active({}) is False
    assert remote_guard_is_active({"warn_token_threshold": 100}) is True
    assert remote_guard_is_active({"max_llm_requests_per_run": 5}) is True


def test_build_llm_provider_gemini_wraps_metered(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "x")
    cfg = {
        "llm": {
            "provider": "gemini",
            "remote_guard": {"max_llm_requests_per_run": 5},
        },
        "gemini": {"api_key_env": "GOOGLE_API_KEY"},
    }
    p = build_llm_provider(cfg)
    assert isinstance(p, MeteredLLMProvider)


def test_metered_raises_after_max_llm(monkeypatch):
    from src.llm.base import LLMResult

    class Fake:
        provider_name = "fake"

        def chat_json(self, **kwargs):
            return LLMResult(
                content_text="{}",
                content_json={},
                raw={},
                model="m",
                provider="fake",
            )

        def generate_json(self, **kwargs):
            return LLMResult(
                content_text="{}",
                content_json={},
                raw={},
                model="m",
                provider="fake",
            )

    m = MeteredLLMProvider(Fake(), {"max_llm_requests_per_run": 1})
    m.chat_json(model="m", messages=[{"role": "user", "content": "a"}])
    with pytest.raises(RuntimeError, match="max_llm_requests_per_run"):
        m.chat_json(model="m", messages=[{"role": "user", "content": "b"}])
