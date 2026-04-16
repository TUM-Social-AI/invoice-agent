"""Centralised parameter alias resolution for agent tool calls.

The LLM often uses interchangeable names for the same logical parameter
(e.g. ``image_path`` / ``page_path`` / ``image``).  Resolving aliases in
one place prevents the same ``or``-chain from appearing in ``registry.py``,
``agent.py``, and anywhere else that inspects raw tool-call params.

Usage
-----
    from src.agent.param_resolver import resolve_param, resolve_sig_params

    image = resolve_param(kwargs, "image_path")
    sig   = resolve_sig_params(params)
"""

from __future__ import annotations

# Canonical name → ordered list of fallback aliases.
# The canonical name itself is always tried first by resolve_param.
PARAM_ALIASES: dict[str, list[str]] = {
    "image_path": [
        "page_path",
        "page_image_path",
        "image",
        "path",
        "file_path",
        "page_file",
    ],
    "page_num": [
        "page_index",
        "page",
    ],
    "field_subset": [
        "fields",
        "field_names",
        "regions",
    ],
}


def resolve_param(kwargs: dict, canonical_name: str) -> object:
    """Return the first non-falsy value found under *canonical_name* or any alias.

    Args:
        kwargs:         Raw keyword-argument dict from a tool call.
        canonical_name: The preferred parameter name (key in PARAM_ALIASES).

    Returns:
        The resolved value, or ``None`` if no alias yields a value.
    """
    # Try canonical name first, then each alias in order.
    if kwargs.get(canonical_name):
        return kwargs[canonical_name]
    for alias in PARAM_ALIASES.get(canonical_name, []):
        if kwargs.get(alias):
            return kwargs[alias]
    return None


def resolve_sig_params(params: dict) -> dict:
    """Build a normalised signature-param dict used by the duplicate-action guard.

    Resolves all known aliases so that alternating between e.g. ``fields``
    and ``field_names`` still produces the same fingerprint and is correctly
    identified as a repeated call.

    Args:
        params: Raw params dict from the agent action.

    Returns:
        A dict with only the canonical keys that are present (None values
        are excluded so fingerprints are stable).
    """
    sig = {
        "image_path":   resolve_param(params, "image_path"),
        "page_num":     resolve_param(params, "page_num"),
        "field_subset": resolve_param(params, "field_subset"),
        "region":       params.get("region"),
        "hints":        params.get("hints"),
        "package":      params.get("package"),
    }
    return {k: v for k, v in sig.items() if v is not None}
