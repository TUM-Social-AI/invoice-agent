"""Shared response-format normalization for LLM providers."""

from __future__ import annotations

import json
import re
from typing import Any, Literal

ResponseFormatMode = Literal["none", "json", "schema"]

# JSON Schema constructs that remote APIs commonly reject for structured constraints.
_SCHEMA_BLOCKING_KEYS = frozenset(
    {"oneOf", "anyOf", "allOf", "not", "if", "then", "else", "dependentRequired", "dependentSchemas"}
)


def clean_json_text(raw: str) -> str:
    return re.sub(r"```(?:json)?", "", raw or "").strip().rstrip("`").strip()


def parse_json_result(text: str) -> dict | None:
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def normalize_response_format(response_format: Any) -> tuple[ResponseFormatMode, dict | None]:
    """Classify response_format into none, json-only, or full schema."""
    if response_format is None:
        return "none", None
    if isinstance(response_format, dict):
        if response_format.get("type") == "json_object":
            return "json", None
        return "schema", response_format
    if isinstance(response_format, str):
        key = response_format.strip().lower()
        if key in ("json", "application/json"):
            return "json", None
    return "none", None


def schema_has_blocking_construct(schema: Any) -> bool:
    """True when schema contains oneOf/anyOf/etc. unsuitable for API schema constraints."""
    if not isinstance(schema, dict):
        return False
    if _SCHEMA_BLOCKING_KEYS & schema.keys():
        return True
    for val in schema.values():
        if isinstance(val, dict) and schema_has_blocking_construct(val):
            return True
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and schema_has_blocking_construct(item):
                    return True
    return False


def provider_json_mode(provider_name: str) -> Any:
    """Provider-specific plain JSON mode when no full schema is attached."""
    if provider_name == "gemini":
        return "application/json"
    if provider_name == "openai":
        return {"type": "json_object"}
    if provider_name == "ollama":
        return "json"
    return "json"


def _is_openai_strict_compatible(schema: Any) -> bool:
    """Best-effort check for OpenAI strict structured-output rules (not API-validated)."""
    if not isinstance(schema, dict) or schema_has_blocking_construct(schema):
        return False

    if schema.get("type") == "object" or "properties" in schema:
        if schema.get("additionalProperties") is not False:
            return False
        props = schema.get("properties")
        if not isinstance(props, dict):
            return False
        required = schema.get("required")
        if not isinstance(required, list) or set(required) != set(props.keys()):
            return False
        for prop in props.values():
            if not _is_openai_strict_compatible(prop):
                return False

    if schema.get("type") == "array" or "items" in schema:
        items = schema.get("items")
        if isinstance(items, list):
            return False
        if isinstance(items, dict) and not _is_openai_strict_compatible(items):
            return False

    for key, val in schema.items():
        if key in (
            "type",
            "properties",
            "required",
            "additionalProperties",
            "items",
            "enum",
            "description",
            "title",
            "default",
            "minimum",
            "maximum",
            "minItems",
            "maxItems",
            "format",
            "pattern",
        ):
            continue
        if isinstance(val, dict) and not _is_openai_strict_compatible(val):
            return False
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict) and not _is_openai_strict_compatible(item):
                    return False
    return True


def to_openai_response_format(response_format: Any) -> dict[str, Any] | None:
    """Map unified response_format to OpenAI Chat Completions response_format.

    Complex schemas (agent oneOf, vision anyOf) fall back to json_object mode so
    OpenAI does not return 400 schema validation errors. That trades enforcement
    for reliability vs Ollama/Gemini full-schema paths.
    """
    mode, schema = normalize_response_format(response_format)
    if mode == "none":
        return None
    if mode == "json":
        return {"type": "json_object"}
    if schema is not None and _is_openai_strict_compatible(schema):
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_output",
                "strict": True,
                "schema": schema,
            },
        }
    return {"type": "json_object"}


def _is_gemini_native_schema(schema: dict[str, Any]) -> bool:
    """True when schema uses Gemini uppercase types (OBJECT, STRING, …)."""
    stype = schema.get("type")
    if isinstance(stype, str) and stype.isupper():
        return True
    props = schema.get("properties")
    if isinstance(props, dict):
        for prop in props.values():
            if isinstance(prop, dict) and isinstance(prop.get("type"), str) and prop["type"].isupper():
                return True
    return False


def gemini_generation_config_kwargs(
    *,
    response_format: Any,
    temperature: float,
    timeout_s: float,
) -> dict[str, Any]:
    """Build kwargs for google.genai GenerateContentConfig from unified response_format."""
    try:
        from google.genai import types  # type: ignore
    except (ModuleNotFoundError, ImportError):  # pragma: no cover
        # Allow unit tests to run in environments without google-genai installed.
        types = None

    mode, schema = normalize_response_format(response_format)
    cfg: dict[str, Any] = {
        "temperature": temperature,
        "http_options": (
            types.HttpOptions(timeout=int(timeout_s * 1000)) if types is not None else {"timeout": int(timeout_s * 1000)}
        ),
    }
    if mode == "none":
        return cfg
    cfg["response_mime_type"] = "application/json"
    if mode == "schema" and schema is not None and not schema_has_blocking_construct(schema):
        if _is_gemini_native_schema(schema):
            cfg["response_schema"] = schema
        else:
            cfg["response_json_schema"] = schema
    return cfg
