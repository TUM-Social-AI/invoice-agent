from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.sources.models import RunIdentity, SourceProvenance


class AgentStatus(str, Enum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"
    ERROR = "error"
    INTERRUPTED = "interrupted"


class FieldResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    field_name: str
    extracted_value: Any
    confidence: float
    source_page: int | None
    source_region: str | None
    extraction_attempts: int = 0
    flagged_for_review: bool = False
    review_reason: str | None = None
    batch_review: bool = False


class RuleResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    rule_name: str
    field_id: str
    status: str
    severity: str
    message: str
    agent_notes: str | None = None


class AgentActionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn: int
    tool_name: str
    tool_input: dict[str, Any]
    tool_output: Any
    reasoning: str


class AgentStateModel(BaseModel):
    # Keep this permissive enough for moderate migration strictness.
    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    pdf_path: str
    output_dir: str
    invoice_type_id: str = ""

    status: AgentStatus = AgentStatus.RUNNING
    turn: int = 0
    started_at: float = Field(default_factory=time.time)

    file_info: dict[str, Any] = Field(default_factory=dict)
    page_image_paths: list[str] = Field(default_factory=list)
    compressed_page_paths: list[str] = Field(default_factory=list)
    medium_page_paths: list[str] = Field(default_factory=list)
    page_count: int = 0
    region_crops: dict[str, str] = Field(default_factory=dict)
    compressed: bool = False

    page_inventory: list[dict[str, Any]] = Field(default_factory=list)
    page_facts: dict[int, dict[str, Any]] = Field(default_factory=dict)

    extracted_fields: dict[str, FieldResultModel] = Field(default_factory=dict)
    field_retry_counts: dict[str, int] = Field(default_factory=dict)

    rule_results: list[RuleResultModel] = Field(default_factory=list)
    failed_rules: list[str] = Field(default_factory=list)
    passed_rules: list[str] = Field(default_factory=list)
    skipped_checks: list[dict[str, Any]] = Field(default_factory=list)
    visual_checks_pending: list[str] = Field(default_factory=list)
    rule_evidence: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rule_policy_refs: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    rule_state: dict[str, str] = Field(default_factory=dict)

    last_compliance_hash: str = ""
    last_compliance_turn: int = -1
    compliance_same_result_streak: int = 0

    action_history: list[AgentActionModel] = Field(default_factory=list)
    learnings_context: str = ""
    session_notes: list[str] = Field(default_factory=list)
    finish_reason: str | None = None

    confidence_threshold: float = 0.65
    batch_review_threshold: float = 0.85
    execution_plan: list[dict[str, Any]] = Field(default_factory=list)
    use_fallback_model: bool = False
    run_log_path: str | None = None
    run_id: str = ""
    source_provenance: SourceProvenance | None = None
    run_identity: RunIdentity | None = None

    # Existing runtime dynamic flag now explicit.
    fallback_logged: bool = False

    @property
    def tmp_dir(self) -> str:
        return str(Path(self.output_dir) / "tmp")
