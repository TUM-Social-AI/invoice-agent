"""Validate LLM tool actions before dispatch."""

_TOOL_PARAM_ALLOWLIST: dict[str, set[str]] = {
    "inspect_file": set(),
    "compress_pages": {"dpi", "quality", "max_width"},
    "classify_document_type": set(),
    "convert_pdf_to_images": {"dpi"},
    "crop_region": {"page_num", "region", "custom_bbox", "image_path"},
    "extract_fields_vision": {"page_num", "region", "hints", "field_subset", "fields", "field_names"},
    "check_compliance": set(),
    "check_compliance_visual": {"page_num"},
    "flag_for_human_review": {"field_name", "fields", "fields_to_flag", "reason"},
    "flag_fields_for_review": {"field_name", "fields", "fields_to_flag", "reason"},
    "inventory_pages": set(),
    "install_package": {"package"},
    "note": {"text"},
    "read_learnings": set(),
    "write_learning": {"invoice_type_id", "category", "content"},
    "edit_learning": {"learning_id", "id", "new_content", "content"},
    "delete_learning": {"learning_id", "id"},
    "finish": {"reason", "all_errors_resolved"},
}


def allowed_params_for_tool(tool: str) -> set[str]:
    """Return allowed params for a tool (empty set if unknown)."""
    return set(_TOOL_PARAM_ALLOWLIST.get(tool, set()))


def validate_action_contract(action: dict) -> str | None:
    """Return None when valid, otherwise an explicit validation error string."""
    if not isinstance(action, dict):
        return "Action must be a JSON object."
    for key in ("tool", "params", "reasoning"):
        if key not in action:
            return f"Missing required key '{key}'."
    tool = action.get("tool")
    params = action.get("params")
    if tool not in _TOOL_PARAM_ALLOWLIST:
        return f"Unknown tool '{tool}'."
    if not isinstance(params, dict):
        return "params must be a JSON object."
    allowed = _TOOL_PARAM_ALLOWLIST[tool]
    extra = sorted([k for k in params.keys() if k not in allowed])
    if extra:
        return f"Tool '{tool}' has invalid params: {extra}. Allowed: {sorted(allowed)}"
    if tool in {"extract_fields_vision", "check_compliance_visual"}:
        if "page_num" not in params:
            return f"Tool '{tool}' requires 'page_num' (integer page index)."
    if tool == "crop_region":
        if "page_num" not in params and "image_path" not in params:
            return "Tool 'crop_region' requires either 'page_num' or 'image_path'."
    if tool == "crop_region" and "region" not in params:
        return "Tool 'crop_region' requires 'region'."
    return None
