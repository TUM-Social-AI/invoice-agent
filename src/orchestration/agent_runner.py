"""
Adapter over `InvoiceAgent.run` + `write_results`.

Keeps the worker loop decoupled from AgentState internals: it runs one materialized
document and returns a small `ProcessOutcome` describing whether a human needs to look
at the result. Heavy imports (agent -> tools -> torch/surya) are pulled in here rather
than in `worker`, so the orchestration logic can be imported/tested without them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from src.agent.agent import InvoiceAgent
from src.agent.state import AgentStatus, rule_verdict_summary
from src.output.writer import write_results
from src.sources.models import MaterializedDocument


@dataclass
class ProcessOutcome:
    status: str                                   # "ok" | "needs_review"
    needs_review: bool
    result_paths: dict[str, str]
    invoice_type_id: str
    blocking_rule_ids: list[str] = field(default_factory=list)
    flagged_fields: list[str] = field(default_factory=list)


def process_document(
    agent: InvoiceAgent,
    materialized: MaterializedDocument,
    output_root: str,
) -> ProcessOutcome:
    """
    Run one document. Raises on an unrecoverable agent error so the caller can retry
    (transient) and eventually park it — a needs-review result is NOT an error; it is
    uploaded normally with a marker.
    """
    out_dir = str(Path(output_root) / materialized.run_identity.safe_document_stem)

    state = agent.run(
        pdf_path=materialized.local_pdf_path,
        output_dir=out_dir,
        source_provenance=materialized.provenance,
        run_identity=materialized.run_identity,
    )

    if state.status == AgentStatus.ERROR:
        raise RuntimeError(
            f"agent returned ERROR for {materialized.provenance.display_name}: "
            f"{state.finish_reason or 'unknown'}"
        )

    paths = write_results(state, out_dir)

    rv = rule_verdict_summary(state.rule_results)
    blocking = list(rv["error_failed_rule_ids"])
    flagged = [name for name, fr in state.extracted_fields.items() if fr.flagged_for_review]
    needs_review = bool(blocking) or bool(flagged) or state.status in (
        AgentStatus.NEEDS_REVIEW,
        AgentStatus.FAILED,
    )

    return ProcessOutcome(
        status="needs_review" if needs_review else "ok",
        needs_review=needs_review,
        result_paths=paths,
        invoice_type_id=state.invoice_type_id or "",
        blocking_rule_ids=blocking,
        flagged_fields=flagged,
    )
