"""Reflection loop."""

import json
import logging
import re
import requests
from typing import Any

from src.agent.state import AgentState
from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.llm.config_resolve import reasoning_model_for_config

logger = logging.getLogger(__name__)

MAX_REFLECTION_TURNS = 8

def reflect(
    state: AgentState,
    diff_text: str,
    config: dict,
    store: ConfigStore,
    tools: dict,
    provider: "LLMProvider | None" = None,
    chat_timeout_s: int = 60,
):
    """
    Short agent loop that receives the ground truth diff and writes learnings.
    Shares the same tool registry as the main run (write_learning, finish, note).
    """
    logger.info("Entering reflection loop")
    reflection_state = state  # same state object — learnings write to same file

    for turn in range(MAX_REFLECTION_TURNS):
        payload = {
            "model": reasoning_model_for_config(config),
            "messages": [
                {"role": "system", "content": build_reflection_prompt(state, diff_text, store)},
                {"role": "user", "content": f"Reflection turn {turn + 1}. Write your next learning or finish."},
            ],
            "stream": False,
            "options": {"temperature": 0.3},
            "format": "json",
        }

        try:
            if provider is not None:
                llm_result = provider.chat_json(
                    model=payload["model"],
                    messages=payload["messages"],
                    response_format=payload.get("format"),
                    temperature=payload.get("options", {}).get("temperature", 0.3),
                    timeout_s=chat_timeout_s,
                )
                raw = llm_result.content_text
                action = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
            else:
                resp = requests.post(
                    f"{config['ollama']['base_url']}/api/chat",
                    json=payload,
                    timeout=chat_timeout_s,
                )
                resp.raise_for_status()
                raw = resp.json()["message"]["content"]
                raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
                action = json.loads(raw)
        except Exception as e:
            logger.error(f"Reflection turn {turn} failed: {e}")
            break

        tool_name = action.get("tool")
        params = action.get("params", {})
        reasoning = action.get("reasoning", "")
        logger.info(f"Reflection turn {turn + 1} | tool={tool_name} | {reasoning[:80]}")

        if tool_name == "finish":
            break

        if tool_name in tools:
            try:
                tools[tool_name](reflection_state, **params)
            except Exception as e:
                logger.warning(f"Reflection tool {tool_name} failed: {e}")

    logger.info("Reflection loop complete")
