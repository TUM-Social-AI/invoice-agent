"""Single agent turn: prompt assembly, LLM dispatch, parse, and retry.

Responsibilities
----------------
- Build the system prompt and user message from current state.
- Delegate response-schema construction to ``response_schema.build_response_schema``.
- Delegate payload construction to ``llm_payload.build_payload``.
- Dispatch to the active ``LLMProvider`` (or raw Ollama requests fallback).
- Retry up to 3 times with JSON-repair hints on parse/validation failure.
"""

import json
import logging
import re
import requests
from typing import Any

from src.agent.state import AgentState
from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.llm.config_resolve import (
    fallback_reasoning_model_for_config,
    prompt_limits_for_config,
    reasoning_model_for_config,
)
from src.agent.prompts import build_system_prompt
from src.prompts.llm_prompts import action_json_repair_user_content
from src.agent.action_contract import (
    validate_action_contract as _validate_action_contract,
    allowed_params_for_tool as _allowed_params_for_tool,
)
from src.models.action_models import TOOL_PARAM_MODELS
from src.agent.response_schema import build_response_schema
from src.agent.llm_payload import build_payload

logger = logging.getLogger(__name__)


def _state_summary_kwargs(agent_cfg: dict) -> dict:
    """Bounds for `AgentState.summary_for_prompt` (large-PDF safety)."""
    return {
        "max_page_lines": int(agent_cfg.get("state_summary_max_page_lines", 28)),
        "max_inventory_lines": int(agent_cfg.get("state_summary_max_inventory_lines", 50)),
        "inventory_desc_chars": int(agent_cfg.get("state_summary_inventory_desc_chars", 180)),
    }



def agent_turn(
    state: AgentState,
    store: ConfigStore,
    config: dict,
    max_field_retries: int = 3,
    confidence_threshold: float = 0.65,
    tool_names: "list[str] | None" = None,
    provider: "LLMProvider | None" = None,
    chat_timeout_s: int = 120,
) -> dict:
    allowed = set(tool_names) if tool_names is not None else None
    _plim = prompt_limits_for_config(config)
    history_preview_chars = _plim["history_preview_chars"]
    _ac = config.get("agent") or {}
    system_prompt = build_system_prompt(
        state,
        store,
        config,
        allowed_tool_names=allowed,
        max_field_retries=max_field_retries,
        confidence_threshold=confidence_threshold,
    )

    history_summary = [
        {
            "turn": a.turn,
            "tool": a.tool_name,
            "reasoning": a.reasoning,
            "result": str(a.tool_output)
            if history_preview_chars <= 0
            else str(a.tool_output)[:history_preview_chars],
        }
        for a in state.action_history[-15:]
    ]

    user_message = (
        f"Current state:\n{state.summary_for_prompt(**_state_summary_kwargs(_ac))}\n\n"
        f"Recent actions:\n{json.dumps(history_summary, indent=2)}\n\n"
        "What should you do next? Respond with JSON only."
    )

    # Structured output schema: Ollama enforces shape so "reasoning"/"tool"/"params"
    # are always present and the JSON is always valid — eliminates the entire class
    # of KeyError / parse-failure bugs on malformed LLM output. Requires Ollama ≥ 0.5.
    # Schema is built in response_schema.py; see that module for constraint details.
    _RESPONSE_SCHEMA = build_response_schema(state, store, tool_names)

    # Adaptive model routing: switch to fallback if the agent has been struggling.
    # Falls back to primary model if no fallback is configured.
    primary_model = reasoning_model_for_config(config)
    fb = fallback_reasoning_model_for_config(config)
    fallback_model = fb or primary_model
    chosen_model = fallback_model if getattr(state, "use_fallback_model", False) else primary_model

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    payload = build_payload(chosen_model, messages, schema=_RESPONSE_SCHEMA, temperature=0.2)

    def _parse_action_from_payload(req_payload: dict) -> dict:
        if provider is not None:
            llm_result = provider.chat_json(
                model=req_payload["model"],
                messages=req_payload["messages"],
                response_format=req_payload.get("format"),
                temperature=req_payload.get("options", {}).get("temperature", 0.2),
                timeout_s=chat_timeout_s,
            )
            if not llm_result.content_text:
                raise ValueError(f"Reasoning model returned empty response on turn {state.turn}")
            if llm_result.content_json is not None:
                return llm_result.content_json
            return json.loads(llm_result.content_text)
        resp = requests.post(
            f"{config['ollama']['base_url']}/api/chat",
            json=req_payload,
            timeout=chat_timeout_s,
        )
        resp.raise_for_status()
        raw = resp.json()["message"]["content"]
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        if not raw:
            raise ValueError(f"Reasoning model returned empty response on turn {state.turn}")
        return json.loads(raw)

    def _normalize_action_params(action: dict) -> dict:
        if not isinstance(action, dict):
            return action
        tool = action.get("tool")
        params = action.get("params")
        if not isinstance(params, dict) or not isinstance(tool, str):
            return action
        allowed_params = _allowed_params_for_tool(tool)
        if not allowed_params:
            return action
        filtered = {k: v for k, v in params.items() if k in allowed_params}
        model = TOOL_PARAM_MODELS.get(tool)
        if model is None:
            action = dict(action)
            action["params"] = filtered
            return action
        validated = model.model_validate(filtered)
        action = dict(action)
        action["params"] = validated.model_dump(exclude_none=True)
        return action

    last_err: Exception = RuntimeError("unreachable")
    for attempt in range(3):
        try:
            action = _parse_action_from_payload(payload)
            action = _normalize_action_params(action)
            validation_err = _validate_action_contract(action)
            if validation_err:
                # One repair retry with explicit validation feedback.
                repair_payload = {
                    **payload,
                    "messages": payload["messages"] + [
                        {
                            "role": "user",
                            "content": action_json_repair_user_content(str(validation_err)),
                        }
                    ],
                }
                repaired = _parse_action_from_payload(repair_payload)
                repaired = _normalize_action_params(repaired)
                repaired_err = _validate_action_contract(repaired)
                if repaired_err:
                    raise ValueError(f"Action contract invalid after repair retry: {repaired_err}")
                return repaired
            return action
        except (ValueError, KeyError, json.JSONDecodeError, requests.RequestException) as e:
            last_err = e
            logger.warning(f"Reasoning action parse/validation failed (attempt {attempt + 1}/3): {e} — retrying")
            continue
    raise last_err
