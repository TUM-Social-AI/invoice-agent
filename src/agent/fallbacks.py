"""Deterministic fallback when the reasoning model fails."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agent.state import AgentState

from src.agent.phases import next_required_step, current_phase

# When these appear in an exception message, the reasoning (or shared) HTTP backend
# is not usable — deterministic scan/classify/extract fallbacks would also call Ollama
# and fail the same way. Fail fast instead of looping.
_BACKEND_UNREACHABLE_MARKERS = (
    "401 client error",
    "403 client error",
    "404 client error",
    "connection refused",
    "connection reset",
    "failed to establish a new connection",
    "max retries exceeded",
    "name or service not known",
    "nodename nor servname provided, or not known",
    "temporary failure in name resolution",
)


def reasoning_backend_unreachable(error_message: str) -> bool:
    """True when errors indicate Ollama (or configured LLM host) is down or misconfigured."""
    m = (error_message or "").lower()
    return any(marker in m for marker in _BACKEND_UNREACHABLE_MARKERS)


def fallback_action_after_llm_failure(state: AgentState, error_message: str = "") -> dict | None:
    if reasoning_backend_unreachable(error_message):
        return None
    err = (error_message or "").lower()
    if "action contract invalid" in err:
        phase = current_phase(state)
        if phase == "EXTRACT":
            return {"tool": "check_compliance", "params": {}, "reasoning": "fallback: move forward after repeated invalid extraction action shape"}
        if phase == "VALIDATE":
            if state.visual_checks_pending:
                return {
                    "tool": "check_compliance_visual",
                    "params": {"page_num": 1},
                    "reasoning": "fallback: required visual checks pending after invalid action shape",
                }
            return {
                "tool": "finish",
                "params": {"reason": "human_review_needed", "all_errors_resolved": False},
                "reasoning": "fallback: finish safely after repeated invalid action shape",
            }

    next_step = next_required_step(state)
    if next_step.startswith("compress_pages"):
        return {"tool": "compress_pages", "params": {"dpi": 48, "quality": 30}, "reasoning": "fallback: required scan step"}
    if next_step.startswith("inventory_pages"):
        return {"tool": "inventory_pages", "params": {}, "reasoning": "fallback: required scan step"}
    if next_step.startswith("classify_document_type"):
        return {"tool": "classify_document_type", "params": {}, "reasoning": "fallback: required scan step"}
    if next_step.startswith("convert_pdf_to_images"):
        dpi = int(getattr(state, "page_render_dpi", 150) or 150)
        return {
            "tool": "convert_pdf_to_images",
            "params": {"dpi": dpi},
            "reasoning": "fallback: required extraction step",
        }

    if state.action_history:
        last = state.action_history[-1]
        if (
            last.tool_name == "extract_fields_vision"
            and isinstance(last.tool_output, dict)
            and "timed out" in str(last.tool_output.get("error", "")).lower()
        ):
            params = last.tool_input if isinstance(last.tool_input, dict) else {}
            page_num = params.get("page_num", 1)
            region = params.get("region", "body")
            candidates = params.get("field_subset") or last.tool_output.get("fallback_fields") or []
            if isinstance(candidates, str):
                candidates = [c.strip() for c in candidates.split(",") if c.strip()]
            if not isinstance(candidates, list):
                candidates = []
            small_subset = [f for f in candidates if isinstance(f, str)][:5]
            if small_subset:
                return {
                    "tool": "extract_fields_vision",
                    "params": {"page_num": page_num, "region": region, "field_subset": small_subset},
                    "reasoning": "fallback: reduce extraction subset after vision timeout",
                }
            return {"tool": "check_compliance", "params": {}, "reasoning": "fallback: proceed after extraction timeout"}

    return None
