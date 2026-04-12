"""Single agent turn implementation."""

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
    #
    # "tool" is constrained to an enum of registered tool names — hallucinated names
    # are impossible via grammar-based decoding.
    #
    # Every param whose valid values are knowable at call time is also constrained:
    #   page_num    → [1..N]  (rendered page count)
    #   region      → the 6 crop slices defined in REGION_CROPS
    #   dpi         → [48, 96, 150, 200]
    #   quality     → 30–90 (JPEG quality range)
    #   max_width   → fixed set of useful resolutions
    #   field_subset / fields / field_names / field_name / fields_to_flag
    #               → enum of field names for the current invoice type
    #   all_errors_resolved → boolean
    #   category    → the 5 learning categories
    # Free-form params (text, content, package, reason, hints…) remain unconstrained.
    tool_schema: dict = {"type": "string"}
    if tool_names:
        tool_schema = {"type": "string", "enum": sorted(tool_names)}

    # -- page_num: constrained to [1..N] once any page list is populated ---------
    page_count = (
        len(state.page_image_paths)
        or len(state.compressed_page_paths)
        or state.page_count
    )
    page_num_schema: dict = (
        {"type": "integer", "enum": list(range(1, page_count + 1))}
        if page_count > 0 else {"type": "integer"}
    )

    # -- field names: dynamic enum once invoice type is classified ---------------
    field_names: list[str] = []
    if state.invoice_type_id:
        try:
            field_names = sorted(store.build_extraction_schema(state.invoice_type_id).keys())
        except Exception:
            pass
    field_name_schema: dict = (
        {"type": "string", "enum": field_names} if field_names else {"type": "string"}
    )
    field_list_schema: dict = (
        {"type": "array", "items": {"type": "string", "enum": field_names}}
        if field_names else {"type": "array", "items": {"type": "string"}}
    )

    region_schema = {
        "type": "string",
        "enum": ["header", "footer", "address_block", "totals", "line_items", "body"],
    }
    category_schema = {
        "type": "string",
        "enum": [
            "approaches",
            "extraction_patterns",
            "common_failures",
            "compliance_edge_cases",
            "tool_suggestions",
        ],
    }

    # Tool-specific action branches prevent cross-tool param leakage
    # (e.g. write_learning.category on extract_fields_vision).
    tool_param_schemas: dict[str, dict] = {
        "inspect_file": {"type": "object", "properties": {}, "additionalProperties": False},
        "compress_pages": {
            "type": "object",
            "properties": {
                "dpi": {"type": "integer", "enum": [48, 96, 150, 200]},
                "quality": {"type": "integer", "minimum": 30, "maximum": 90},
                "max_width": {"type": "integer", "enum": [800, 1000, 1200, 1400, 1600, 2000]},
            },
            "additionalProperties": False,
        },
        "classify_document_type": {"type": "object", "properties": {}, "additionalProperties": False},
        "convert_pdf_to_images": {
            "type": "object",
            "properties": {"dpi": {"type": "integer", "enum": [48, 96, 150, 200]}},
            "additionalProperties": False,
        },
        "crop_region": {
            "type": "object",
            "properties": {
                "page_num": page_num_schema,
                "image_path": {"type": "string"},
                "region": region_schema,
                "custom_bbox": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                },
            },
            "additionalProperties": False,
        },
        "extract_fields_vision": {
            "type": "object",
            "properties": {
                "page_num": page_num_schema,
                "region": region_schema,
                "hints": {"type": "string"},
                "field_subset": field_list_schema,
                "fields": field_list_schema,
                "field_names": field_list_schema,
            },
            "additionalProperties": False,
        }
        | ({"required": ["page_num"]} if page_count > 0 else {}),
        "check_compliance": {"type": "object", "properties": {}, "additionalProperties": False},
        "check_compliance_visual": {
            "type": "object",
            "properties": {
                "page_num": page_num_schema,
            },
            "additionalProperties": False,
        }
        | ({"required": ["page_num"]} if page_count > 0 else {}),
        "flag_for_human_review": {
            "type": "object",
            "properties": {
                "field_name": field_name_schema,
                "fields": field_list_schema,
                "fields_to_flag": field_list_schema,
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "flag_fields_for_review": {
            "type": "object",
            "properties": {
                "field_name": field_name_schema,
                "fields": field_list_schema,
                "fields_to_flag": field_list_schema,
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "inventory_pages": {"type": "object", "properties": {}, "additionalProperties": False},
        "install_package": {
            "type": "object",
            "properties": {"package": {"type": "string"}},
            "required": ["package"],
            "additionalProperties": False,
        },
        "note": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "read_learnings": {"type": "object", "properties": {}, "additionalProperties": False},
        "write_learning": {
            "type": "object",
            "properties": {
                "invoice_type_id": {"type": "string"},
                "category": category_schema,
                "content": {"type": "string"},
            },
            "required": ["category", "content"],
            "additionalProperties": False,
        },
        "edit_learning": {
            "type": "object",
            "properties": {
                "learning_id": {"type": "string"},
                "id": {"type": "string"},
                "new_content": {"type": "string"},
                "content": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "delete_learning": {
            "type": "object",
            "properties": {"learning_id": {"type": "string"}, "id": {"type": "string"}},
            "additionalProperties": False,
        },
        "finish": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "all_errors_resolved": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    }

    allowed_tool_list = sorted(tool_names) if tool_names else sorted(tool_param_schemas.keys())
    branches = []
    for tname in allowed_tool_list:
        params_branch = tool_param_schemas.get(tname, {"type": "object", "additionalProperties": True})
        branches.append(
            {
                "type": "object",
                "properties": {
                    "reasoning": {"type": "string"},
                    "tool": {"type": "string", "const": tname},
                    "params": params_branch,
                },
                "required": ["reasoning", "tool", "params"],
                "additionalProperties": False,
            }
        )

    _RESPONSE_SCHEMA = {"oneOf": branches}

    # Adaptive model routing: switch to fallback if the agent has been struggling.
    # Falls back to primary model if no fallback is configured.
    primary_model = reasoning_model_for_config(config)
    fb = fallback_reasoning_model_for_config(config)
    fallback_model = fb or primary_model
    chosen_model = fallback_model if getattr(state, "use_fallback_model", False) else primary_model

    payload = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "stream": False,
        "options": {"temperature": 0.2},
        "format": _RESPONSE_SCHEMA,
    }

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
