"""
Agent state — the single object threaded through the entire agentic loop.
Every tool call reads from and writes back to this state.
"""

import time
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class AgentStatus(Enum):
    RUNNING = "running"
    PASSED = "passed"           # all rules satisfied
    FAILED = "failed"           # max retries hit, some rules still failing
    NEEDS_REVIEW = "needs_review"  # agent flagged fields for human
    ERROR = "error"             # unrecoverable error
    INTERRUPTED = "interrupted" # user cancelled (Ctrl+C)


class FieldResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field_id: str
    field_name: str
    extracted_value: Any
    confidence: float           # 0.0 – 1.0
    source_page: Optional[int]
    source_region: Optional[str]
    extraction_attempts: int = 0
    flagged_for_review: bool = False
    review_reason: Optional[str] = None
    batch_review: bool = False  # True when confidence is medium (threshold–0.85); non-blocking


class RuleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    rule_name: str
    field_id: str
    status: str                 # passed | failed | skipped | flagged
    severity: str               # error | warning
    message: str
    agent_notes: Optional[str] = None


def rule_verdict_summary(rule_results: list[RuleResult]) -> dict[str, Any]:
    """
    Split rule outcomes by severity. `status == "failed"` includes both error- and
    warning-severity rules; finish() only blocks on the former.
    """
    passed_n = sum(1 for r in rule_results if r.status == "passed")
    err_failed = [r.rule_id for r in rule_results if r.status == "failed" and r.severity == "error"]
    warn_failed = [r.rule_id for r in rule_results if r.status == "failed" and r.severity == "warning"]
    return {
        "passed_count": passed_n,
        "error_failed_rule_ids": err_failed,
        "warning_failed_rule_ids": warn_failed,
    }


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    turn: int
    tool_name: str
    tool_input: dict
    tool_output: Any
    reasoning: str              # why the agent picked this tool


class AgentState(BaseModel):
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)
    # --- Inputs ---
    pdf_path: str
    output_dir: str
    invoice_type_id: str = ""       # empty until classify_document_type is called

    # --- Runtime ---
    status: AgentStatus = AgentStatus.RUNNING
    turn: int = 0
    started_at: float = Field(default_factory=time.time)

    # --- File metadata (set by inspect_file) ---
    file_info: dict = Field(default_factory=dict)

    # --- PDF processing ---
    page_image_paths: list[str] = Field(default_factory=list)
    compressed_page_paths: list[str] = Field(default_factory=list)  # low-res copies from compress_pages
    # Medium-res pages for hybrid extraction/visual (tmp/medium_pages); same length as page_image_paths when set
    medium_page_paths: list[str] = Field(default_factory=list)
    page_count: int = 0
    region_crops: dict[str, str] = Field(default_factory=dict)   # "page1_header" → path
    compressed: bool = False        # True after compress_pages was called
    # Default DPI for convert_pdf_to_images when the tool omits dpi (from config agent.page_dpi).
    page_render_dpi: int = 150

    # --- Page inventory (set by inventory_pages) ---
    # [{page: 1, description: "invoice header with vendor info"}, ...]
    page_inventory: list[dict] = Field(default_factory=list)
    # Normalized page facts used by evidence-grounded compliance.
    # {page_num: {"category": str, "doc_subtype": str, "entities": {...}, "confidence": float}}
    page_facts: dict[int, dict] = Field(default_factory=dict)

    # --- Extraction ---
    extracted_fields: dict[str, FieldResult] = Field(default_factory=dict)  # field_name → result
    field_retry_counts: dict[str, int] = Field(default_factory=dict)

    # --- Compliance ---
    rule_results: list[RuleResult] = Field(default_factory=list)
    failed_rules: list[str] = Field(default_factory=list)         # rule_ids still failing
    passed_rules: list[str] = Field(default_factory=list)
    skipped_checks: list[dict] = Field(default_factory=list)      # non-visual skips: [{rule_id, reason}]
    visual_checks_pending: list[str] = Field(default_factory=list)  # rule_ids awaiting check_compliance_visual
    # Evidence and policy grounding for each rule_id.
    # rule_evidence[rule_id] = {"required_slots": [...], "filled_slots": [...], "missing_slots": [...], "refs": [...]}
    rule_evidence: dict[str, dict] = Field(default_factory=dict)
    # rule_policy_refs[rule_id] = [{"source": "learnings", "snippet_id": "L042", "snippet": "..."}]
    rule_policy_refs: dict[str, list[dict]] = Field(default_factory=dict)
    # rule_state[rule_id] = unseen|candidate|supported|contradicted|finalized_pass|finalized_fail|needs_review
    rule_state: dict[str, str] = Field(default_factory=dict)

    # --- Compliance loop detection ---
    last_compliance_hash: str = ""   # MD5 of last check_compliance result (detects redundant calls)
    last_compliance_turn: int = -1   # turn number of last check_compliance call
    # Consecutive check_compliance calls that returned the same result hash as the previous run.
    compliance_same_result_streak: int = 0

    # --- History ---
    action_history: list[AgentAction] = Field(default_factory=list)
    learnings_context: str = ""     # loaded from learnings.md at start

    # --- Per-file working memory ---
    # The agent can freely write observations, hypotheses and decisions here
    # across turns without committing them to the permanent learnings file.
    session_notes: list[str] = Field(default_factory=list)

    # --- Final output ---
    finish_reason: Optional[str] = None

    # --- Runtime config (passed from config.yaml via InvoiceAgent) ---
    confidence_threshold: float = 0.65
    # Fields with confidence in [confidence_threshold, batch_review_threshold) are
    # marked batch_review=True — non-blocking, noted in output but don't block finish().
    # Only fields below confidence_threshold trigger a full blocking human-review flag.
    batch_review_threshold: float = 0.85

    # --- Execution plan (generated once before the main loop) ---
    execution_plan: list[dict] = Field(default_factory=list)

    # --- Adaptive model routing ---
    use_fallback_model: bool = False
    fallback_logged: bool = False

    # --- Per-run log ---
    run_log_path: Optional[str] = None

    @property
    def tmp_dir(self) -> str:
        return str(Path(self.output_dir) / "tmp")

    def record_action(self, tool_name: str, tool_input: dict, tool_output: Any, reasoning: str):
        self.action_history.append(AgentAction(
            turn=self.turn,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_output=tool_output,
            reasoning=reasoning,
        ))
        self.turn += 1

    def get_field_retry_count(self, field_name: str) -> int:
        return self.field_retry_counts.get(field_name, 0)

    def increment_field_retry(self, field_name: str):
        self.field_retry_counts[field_name] = self.get_field_retry_count(field_name) + 1


    def summary_for_prompt(
        self,
        *,
        max_page_lines: int = 28,
        max_inventory_lines: int = 50,
        inventory_desc_chars: int = 180,
    ) -> str:
        """Returns a compact state summary for injection into agent turns.

        Path and inventory lists are truncated for large PDFs so prompts stay bounded
        for remote APIs (see agent.state_summary_* in config).
        """
        def _truncate_desc(d: str, max_chars: int) -> str:
            d = (d or "").strip()
            if max_chars <= 0:
                return d
            if len(d) <= max_chars:
                return d
            return d[: max(0, max_chars - 3)] + "..."

        def _format_paths_block(
            paths: list[str],
            *,
            title: str,
            subtitle: str,
            max_lines: int,
        ) -> str:
            n = len(paths)
            if n == 0:
                return ""
            if max_lines <= 0:
                return (
                    f"{title} ({n} total — {subtitle}): "
                    f"(paths omitted — use page_num 1..{n})\n"
                    f"  → Pass page_num=N (integer 1-indexed). NEVER construct or guess paths."
                )
            if n <= max_lines:
                body = "\n".join(f"  page_num={i+1}: {p}" for i, p in enumerate(paths))
            else:
                head = max_lines // 2
                tail = max_lines - head
                first = "\n".join(f"  page_num={i+1}: {p}" for i, p in enumerate(paths[:head]))
                last = "\n".join(
                    f"  page_num={n - tail + i + 1}: {p}"
                    for i, p in enumerate(paths[-tail:])
                )
                omitted = n - head - tail
                body = (
                    f"{first}\n"
                    f"  ... ({omitted} pages omitted — valid page_num 1..{n}) ...\n"
                    f"{last}"
                )
            return (
                f"{title} ({n} total — {subtitle}):\n{body}\n"
                f"  → Pass page_num=N (integer 1-indexed). NEVER construct or guess paths."
            )

        file_summary = (
            f"File: {self.file_info.get('filename', Path(self.pdf_path).name)} | "
            f"Size: {self.file_info.get('size_mb', '?')} MB | "
            f"Pages: {self.file_info.get('page_count', self.page_count or '?')}"
            if self.file_info else f"File: {Path(self.pdf_path).name} (not yet inspected)"
        )
        type_summary = (
            f"Invoice type: {self.invoice_type_id}"
            if self.invoice_type_id else "Invoice type: UNKNOWN — must call classify_document_type"
        )
        notes_summary = (
            "\n".join(f"  - {n}" for n in self.session_notes[-5:])
            if self.session_notes else "  (none)"
        )
        # Full-res pages (for extraction)
        if self.page_image_paths:
            pages_summary = _format_paths_block(
                self.page_image_paths,
                title="Full-res pages",
                subtitle="USE THESE for extract_fields_vision",
                max_lines=max_page_lines,
            )
        else:
            _dpi = int(self.page_render_dpi or 150)
            pages_summary = (
                "Full-res pages: NOT RENDERED"
                f" — call convert_pdf_to_images(dpi={_dpi}) before extraction."
            )

        # Compressed thumbnails (for inventory/classify only)
        if self.compressed_page_paths:
            compressed_summary = _format_paths_block(
                self.compressed_page_paths,
                title="Compressed thumbnails",
                subtitle="inventory/classify ONLY, NOT suitable for extraction",
                max_lines=max_page_lines,
            )
        else:
            compressed_summary = "Compressed thumbnails: none"

        if self.page_inventory:
            inv = self.page_inventory
            max_inv = max(1, max_inventory_lines)
            if len(inv) <= max_inv:
                slice_inv = inv
                inv_omit = ""
            else:
                head = max_inv // 2
                tail = max_inv - head
                slice_inv = inv[:head] + inv[-tail:]
                inv_omit = (
                    f"  ... ({len(inv) - len(slice_inv)} inventory rows omitted; "
                    f"pages 1..{len(inv)}) ...\n"
                )
            inv_lines = "\n".join(
                f"  p{e['page']} [{e.get('category', '?')}]: "
                f"{_truncate_desc(str(e.get('description', '')), inventory_desc_chars)}"
                for e in slice_inv
            )
            inventory_summary = f"Page inventory:\n{inv_omit}{inv_lines}"
        else:
            inventory_summary = "Page inventory: (not built yet — call inventory_pages after compression)"

        # Split fields: done (high-confidence) vs batch_review (medium) vs needs attention
        done_fields = {}
        batch_review_fields = {}
        attention_fields = {}
        for k, v in self.extracted_fields.items():
            if v.flagged_for_review or v.extracted_value is None:
                attention_fields[k] = {
                    "value": v.extracted_value,
                    "confidence": round(v.confidence, 2),
                    "flagged": v.flagged_for_review,
                }
            elif v.batch_review:
                batch_review_fields[k] = round(v.confidence, 2)
            elif v.confidence >= self.confidence_threshold:
                done_fields[k] = round(v.confidence, 2)
            else:
                attention_fields[k] = {
                    "value": v.extracted_value,
                    "confidence": round(v.confidence, 2),
                    "flagged": v.flagged_for_review,
                }

        done_summary = (
            f"{len(done_fields)} done: {done_fields}"
            if done_fields else "0 done"
        )
        batch_review_summary = (
            f"{len(batch_review_fields)} batch-review (medium confidence, non-blocking): {batch_review_fields}"
            if batch_review_fields else ""
        )

        if self.skipped_checks:
            skipped_summary = ", ".join(
                f"{s['rule_id']} (missing: {s['reason']})" for s in self.skipped_checks
            )
            skipped_line = f"Skipped compliance checks (fields missing — extract these first): {skipped_summary}"
        else:
            skipped_line = ""

        plan_summary = ""
        if self.execution_plan:
            # Track progress by counting how many plan steps have a matching
            # successful tool call in action_history (skips retries/failures).
            used_tools = [
                a.tool_name for a in self.action_history
                if isinstance(a.tool_output, dict) and a.tool_output.get("success", True)
                and a.tool_output.get("success") is not False
            ]
            done_steps = 0
            used_copy = list(used_tools)
            for step in self.execution_plan:
                t = step.get("tool")
                if t in used_copy:
                    used_copy.remove(t)
                    done_steps += 1
                else:
                    break
            remaining = self.execution_plan[done_steps:]
            if remaining:
                next_steps = " → ".join(s.get("tool", "?") for s in remaining[:4])
                plan_summary = f"Execution plan — next: {next_steps}\n"

        # Import here to avoid circular imports at module level.
        from src.agent.phases import next_required_step as _next_required_step
        next_step_hint = _next_required_step(self)

        evidence_gap_line = ""
        if self.rule_evidence:
            high_priority_gaps = []
            for rr in self.rule_results:
                if rr.severity != "error":
                    continue
                ev = self.rule_evidence.get(rr.rule_id, {})
                missing = ev.get("missing_slots", [])
                if missing:
                    high_priority_gaps.append(f"{rr.rule_id}:{missing}")
            if high_priority_gaps:
                evidence_gap_line = (
                    f"Evidence gaps (error-priority): {', '.join(high_priority_gaps[:8])}\n"
                )

        rv = rule_verdict_summary(self.rule_results)
        rules_line = (
            f"Non-pass rules — blocking (error): {rv['error_failed_rule_ids']} | "
            f"non-blocking (warning): {rv['warning_failed_rule_ids']}\n"
        )

        return (
            f"Turn: {self.turn}\n"
            f"{file_summary}\n"
            f"{type_summary}\n"
            + (f"⟶ NEXT REQUIRED STEP: {next_step_hint}\n" if next_step_hint else "")
            + (plan_summary if plan_summary else "")
            + f"Pages rendered: {self.page_count} | Compressed: {self.compressed}\n"
            f"{pages_summary}\n"
            f"{compressed_summary}\n"
            f"{inventory_summary}\n"
            f"Extracted fields — {done_summary}\n"
            + (f"Extracted fields — {batch_review_summary}\n" if batch_review_summary else "")
            +             f"Extracted fields — need attention ({len(attention_fields)}): {attention_fields}\n"
            f"{rules_line}"
            + (
                f"⚠ VISUAL CHECKS PENDING ({len(self.visual_checks_pending)}): "
                f"{self.visual_checks_pending} — MUST call check_compliance_visual before finish\n"
                if self.visual_checks_pending else ""
            )
            + (f"{skipped_line}\n" if skipped_line else "")
            + evidence_gap_line
            + f"Fields flagged for review: {[k for k,v in self.extracted_fields.items() if v.flagged_for_review]}\n"
            f"Field retry counts: {self.field_retry_counts}\n"
            f"Session notes (last 5):\n{notes_summary}\n"
        )
