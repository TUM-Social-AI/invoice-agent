"""
Config-driven tool exposure for the agent loop (groups, allow/deny overrides).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# Registry keys must exist; groups are subsets of logical tool names.
## TODO: shouldn't the tools be defined somewhere centralized, so if I want to remove/add/edit a tool, I only have to do it in one place
TOOL_GROUPS: dict[str, frozenset[str]] = {
    "pipeline": frozenset({
        "inspect_file",
        "compress_pages",
        "inventory_pages",
        "classify_document_type",
        "convert_pdf_to_images",
        "extract_fields_vision",
        "check_compliance",
        "check_compliance_visual",
        "crop_region",
        "flag_for_human_review",
        "flag_fields_for_review",
        "finish",
        "note",
    }),
    "granular": frozenset(),
    "learnings": frozenset({
        "read_learnings",
        "write_learning",
        "edit_learning",
        "delete_learning",
    }),
}


def merge_exposed_tool_names(
    tool_groups_enabled: list[str],
    learnings_tools_enabled: bool,
    tools_extra_allow: set[str],
    tools_extra_deny: set[str],
    registry_keys: set[str],
) -> set[str]:
    exposed: set[str] = set()
    for g in tool_groups_enabled:
        exposed |= set(TOOL_GROUPS.get(g, frozenset()))
    if learnings_tools_enabled:
        exposed |= set(TOOL_GROUPS["learnings"])
    exposed |= set(tools_extra_allow)
    exposed -= set(tools_extra_deny)
    exposed &= registry_keys
    # Session control tools always available in loop mode when present in registry.
    for required in ("finish", "note"):
        if required in registry_keys:
            exposed.add(required)
    return exposed


def parse_tool_description_blocks(full: str) -> tuple[str, dict[str, str]]:
    """
    Split the big TOOL_DESCRIPTIONS string into a header line and per-tool blocks.
    Each block starts at a line matching `toolname(`.
    """
    full = full.strip()
    m = re.search(r"(?m)^(?=[a-z_][a-z0-9_]*\()", full)
    if not m:
        return full, {}
    header = full[: m.start()].strip()
    body = full[m.start() :]
    parts = re.split(r"(?m)^(?=[a-z_][a-z0-9_]*\()", body)
    blocks: dict[str, str] = {}
    for p in parts:
        p = p.strip()
        if not p:
            continue
        mm = re.match(r"^([a-z_][a-z0-9_]*)\(", p)
        if mm:
            blocks[mm.group(1)] = p
    return header, blocks


def load_tool_description_overrides(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    return {}


def render_tool_documentation(
    header: str,
    blocks: dict[str, str],
    overrides: dict[str, str],
    allowed_tool_names: set[str],
) -> str:
    lines = [header, ""]
    for name in sorted(allowed_tool_names):
        body = overrides.get(name) or blocks.get(name)
        if body:
            lines.append(body)
    return "\n".join(lines)
