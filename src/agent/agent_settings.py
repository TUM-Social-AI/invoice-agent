"""Runtime/display settings from config.yaml `agent:` section."""


def clip_for_log(text, max_chars: int) -> str:
    """Truncate for human-facing logs. max_chars <= 0 means no truncation."""
    if text is None:
        return ""
    s = str(text)
    if max_chars <= 0:
        return s
    return s if len(s) <= max_chars else s[:max_chars] + "…"


def clip_for_prompt(text: str, max_chars: int) -> str:
    """Cap injected prompt text. max_chars <= 0 means omit (caller should skip block)."""
    if not text or max_chars <= 0:
        return ""
    return text if len(text) <= max_chars else text[:max_chars] + "…"


def parse_agent_runtime_settings(config: dict) -> dict:
    a = config.get("agent", {})
    return {
        "max_turns": int(a.get("max_turns", 25)),
        "max_field_retries": int(a.get("max_field_retries", 3)),
        "confidence_threshold": float(a.get("confidence_threshold", 0.65)),
        "batch_review_threshold": float(a.get("batch_review_threshold", 0.85)),
        "planning_timeout_s": int(a.get("planning_timeout_s", 180)),
        "reflection_timeout_s": int(a.get("reflection_timeout_s", 120)),
        "planning_enabled": bool(a.get("planning_enabled", True)),
        "orchestration": str(a.get("orchestration", "pipeline")).strip().lower(),
        "tool_groups_enabled": list(a.get("tool_groups_enabled", ["pipeline"])),
        "tools_extra_allow": set(a.get("tools_extra_allow", [])),
        "tools_extra_deny": set(a.get("tools_extra_deny", ["install_package"])),
        "learnings_tools_enabled": bool(a.get("learnings_tools_enabled", False)),
        "learnings_inject_enabled": bool(a.get("learnings_inject_enabled", True)),
        "learnings_max_chars": int(a.get("learnings_max_chars", 8000)),
        "planning_learnings_max_chars": int(a.get("planning_learnings_max_chars", 800)),
        "log_line_max_chars": int(a.get("log_line_max_chars", 120)),
        "history_preview_chars": int(a.get("history_preview_chars", 300)),
        "ground_truth_csv_path": a.get("ground_truth_csv_path") or None,
        "ground_truth_source_column": str(a.get("ground_truth_source_column", "Source file")),
        "ground_truth_column_map": dict(a.get("ground_truth_column_map", {})),
        "tool_descriptions_path": a.get("tool_descriptions_path"),
        "micro_tools_phase2": bool(a.get("micro_tools_phase2", False)),
        "recovery_mode": str(a.get("recovery_mode", "none")).strip().lower(),
    }
