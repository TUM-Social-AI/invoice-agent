"""Phase-based tool narrowing for the agent loop.

Phase-to-tool mappings are loaded from ``config/phase_tools.yaml`` at import
time.  If the file is absent the hardcoded fallback dict is used so the agent
remains functional without the config file.  Edit ``config/phase_tools.yaml``
to add, remove, or reassign tools without touching this file.
"""

from __future__ import annotations

import logging
import pathlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.state import AgentState

logger = logging.getLogger(__name__)

_ALWAYS_AVAILABLE = {"note", "install_package"}

_PHASE_TOOLS_FALLBACK: dict[str, set[str]] = {
    "SCAN": {
        "inspect_file",
        "compress_pages",
        "inventory_pages",
        "classify_document_type",
        "read_learnings",
    }
    | _ALWAYS_AVAILABLE,
    "EXTRACT": {
        "convert_pdf_to_images",
        "extract_fields_vision",
        "crop_region",
        "flag_for_human_review",
        "flag_fields_for_review",
        "read_learnings",
        "write_learning",
        "edit_learning",
        "delete_learning",
        "check_compliance",
    }
    | _ALWAYS_AVAILABLE,
    "VALIDATE": {
        "check_compliance",
        "check_compliance_visual",
        "extract_fields_vision",
        "crop_region",
        "flag_for_human_review",
        "flag_fields_for_review",
        "write_learning",
        "edit_learning",
        "delete_learning",
        "finish",
    }
    | _ALWAYS_AVAILABLE,
}


def _load_phase_tools(config_dir: str | pathlib.Path | None = None) -> dict[str, set[str]]:
    """Load phase-to-tool mappings from ``config/phase_tools.yaml``.

    Falls back to the hardcoded ``_PHASE_TOOLS_FALLBACK`` dict if the file is
    absent or cannot be parsed.
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return _PHASE_TOOLS_FALLBACK

    if config_dir is None:
        config_dir = pathlib.Path(__file__).parent.parent.parent / "config"
    yaml_path = pathlib.Path(config_dir) / "phase_tools.yaml"
    if not yaml_path.exists():
        return _PHASE_TOOLS_FALLBACK
    try:
        with yaml_path.open() as fh:
            raw: dict = yaml.safe_load(fh) or {}
        result: dict[str, set[str]] = {}
        for phase, tools in raw.items():
            result[phase] = set(tools or []) | _ALWAYS_AVAILABLE
        return result
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to load %s, using fallback: %s", yaml_path, exc)
        return _PHASE_TOOLS_FALLBACK


_PHASE_TOOLS: dict[str, set[str]] = _load_phase_tools()


def current_phase(state: AgentState) -> str:
    if not state.invoice_type_id:
        return "SCAN"
    if not state.rule_results:
        return "EXTRACT"
    return "VALIDATE"


def next_required_step(state: AgentState) -> str:
    if not state.invoice_type_id:
        if not state.compressed:
            return "compress_pages(dpi=48, quality=30)"
        if not state.page_inventory:
            return "inventory_pages()"
        return "classify_document_type()"
    if not state.rule_results:
        full_quality = bool(state.page_image_paths) and state.page_image_paths != state.compressed_page_paths
        if not full_quality:
            dpi = int(getattr(state, "page_render_dpi", 150) or 150)
            return f"convert_pdf_to_images(dpi={dpi})"
    if state.visual_checks_pending:
        return "check_compliance_visual(page_num=N) — visual checks are pending"
    return ""


def phase_tool_names(state: AgentState, all_tools: list[str], exposed: set[str]) -> list[str]:
    allowed = set(_PHASE_TOOLS[current_phase(state)]) & exposed

    if not state.invoice_type_id:
        if not state.compressed:
            allowed = {"compress_pages", "read_learnings", "inspect_file"} & exposed | (
                _ALWAYS_AVAILABLE & exposed
            )
        elif not state.page_inventory:
            allowed = {"inventory_pages"} & exposed | (_ALWAYS_AVAILABLE & exposed)
        else:
            allowed = {"classify_document_type"} & exposed | (_ALWAYS_AVAILABLE & exposed)
    else:
        full_quality_rendered = bool(state.page_image_paths) and state.page_image_paths != state.compressed_page_paths
        if full_quality_rendered:
            allowed.discard("convert_pdf_to_images")
        if (
            current_phase(state) == "VALIDATE"
            and getattr(state, "compliance_same_result_streak", 0) >= 2
        ):
            allowed.discard("check_compliance")

    return [t for t in all_tools if t in allowed]
