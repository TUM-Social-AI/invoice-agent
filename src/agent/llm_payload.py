"""Shared LLM payload builder used by the agent turn and reflection loops.

Both ``agent_turn`` (turn.py) and ``reflect`` (reflection.py) send chat
requests to the same LLM backend.  This module provides a single
``build_payload`` function so the wire format is defined in exactly one
place — temperature defaults, the ``stream`` flag, and the optional
``format`` key are all controlled here.
"""

from typing import Any


def build_payload(
    model: str,
    messages: list[dict],
    schema: Any = None,
    temperature: float = 0.2,
) -> dict:
    """Return an Ollama-compatible chat payload dict.

    Args:
        model:       LLM model identifier string.
        messages:    Conversation message list (role/content dicts).
        schema:      Optional response-format constraint — either a JSON
                     Schema dict (structured output) or the string ``"json"``
                     (unstructured JSON mode).  Omitted from the payload
                     when ``None`` so provider wrappers that ignore the
                     field are not affected.
        temperature: Sampling temperature; defaults to 0.2.

    Returns:
        A dict ready to pass to an Ollama ``/api/chat`` endpoint or to
        ``LLMProvider.chat_json`` via its ``response_format`` parameter.
    """
    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if schema is not None:
        payload["format"] = schema
    return payload
