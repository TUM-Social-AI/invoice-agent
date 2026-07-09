"""Tool registry assembler.

Constructs the agent's runtime tool dict by wiring tool-wrapper factories
from :mod:`src.tools.tool_wrappers` to the current run's config, config
store, and LLM provider.  All tool *logic* lives in ``tool_wrappers.py``;
this module is intentionally a thin assembler so adding, removing, or
renaming a tool requires editing only one place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.llm.config_resolve import (
    active_rule_groups_from_config,
    ollama_base_url,
    prompt_limits_for_config,
    vision_model_for_config,
)
from src.agent.loop_utils import timeout_cfg as _timeout_cfg
from src.tools.tool_wrappers import (
    ToolContext,
    make_inspect,
    make_compress,
    make_classify,
    make_convert_pdf,
    make_crop,
    make_extract,
    make_check,
    make_flag,
    make_read_learnings,
    make_write_learning,
    make_edit_learning,
    make_delete_learning,
    make_inventory,
    make_check_visual,
    make_install_package,
    make_note,
    make_finish,
)

if TYPE_CHECKING:
    from src.tools.tools import SuryaModels

logger = logging.getLogger(__name__)


def _resolve_ollama_url(provider: "LLMProvider | None", config: dict) -> str:
    """Return the Ollama base URL, preferring the provider over config.

    When ``provider`` is an ``OllamaProvider`` the URL was already resolved
    at startup and stored on the object — using it here avoids a second
    config read and makes the registry independent of Ollama-specific config
    keys.  For non-Ollama providers (e.g. Gemini) ``base_url`` is absent, so
    we fall back to reading the config as before.
    """
    if provider is not None and getattr(provider, "base_url", None):
        return provider.base_url  # type: ignore[attr-defined]
    return ollama_base_url(config)


def build_tool_registry(
    config: dict,
    store: ConfigStore,
    surya_models: "Optional[SuryaModels]" = None,
    provider: "LLMProvider | None" = None,
    ocr_silent: bool = False,
) -> dict:
    """Assemble and return the agent's runtime tool dict.

    Creates a :class:`ToolContext` from the current run's resolved config
    values, then calls each tool factory in :mod:`src.tools.tool_wrappers`
    to produce the callable.

    Args:
        config:        Full application config dict.
        store:         ConfigStore for invoice-type schemas and compliance rules.
        surya_models:  Pre-loaded Surya OCR models (None → loaded on first use).
        provider:      Active LLM provider (None → raw Ollama requests fallback).

    Returns:
        Dict mapping tool name → callable with signature
        ``(state: AgentState, **kwargs) -> dict``.
    """
    agent_cfg = config.get("agent", {})
    _plim = prompt_limits_for_config(config)

    ctx = ToolContext(
        ollama_url=_resolve_ollama_url(provider, config),
        vision_model=vision_model_for_config(config),
        learnings_path=config.get("learnings_path", "learnings/learnings.md"),
        agent_cfg=agent_cfg,
        timeouts=_timeout_cfg(config),
        learnings_max_chars=_plim["learnings_max_chars"],
        visual_max_evidence_pages=int(agent_cfg.get("visual_max_evidence_pages", 6)),
        ocr_prompt_max_chars=int(agent_cfg.get("ocr_prompt_max_chars", 24000) or 0),
        ocr_langs=config.get("ocr", {}).get("langs", ["es", "en"]),
        active_rule_groups=active_rule_groups_from_config(config),
        store=store,
        provider=provider,
        surya_models=surya_models,
        inventory_batch_size=int(agent_cfg.get("inventory_batch_size", 1)),
        ocr_silent=ocr_silent,
    )

    return {
        "inspect_file":            make_inspect(ctx),
        "compress_pages":          make_compress(ctx),
        "classify_document_type":  make_classify(ctx),
        "convert_pdf_to_images":   make_convert_pdf(ctx),
        "crop_region":             make_crop(ctx),
        "extract_fields_vision":   make_extract(ctx),
        "check_compliance":        make_check(ctx),
        "flag_for_human_review":   make_flag(ctx),
        "flag_fields_for_review":  make_flag(ctx),   # alias the LLM tends to invent
        "inventory_pages":         make_inventory(ctx),
        "check_compliance_visual": make_check_visual(ctx),
        "install_package":         make_install_package(ctx),
        "note":                    make_note(ctx),
        "read_learnings":          make_read_learnings(ctx),
        "write_learning":          make_write_learning(ctx),
        "edit_learning":           make_edit_learning(ctx),
        "delete_learning":         make_delete_learning(ctx),
        "finish":                  make_finish(ctx),
    }
