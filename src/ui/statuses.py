"""
Workflow statuses of a run, and the actions a reviewer can take on it.

This is the one place to change them: the browser reads labels, colours and allowed actions
from `/api/statuses`, and saved runs keep the agent's raw verdict next to the status, so the
rules here can change without touching stored runs.
"""

from __future__ import annotations

from typing import Any

# Order is the order of the dashboard filter chips. `tone` picks the pill colour in style.css;
# `active` runs are still moving on their own (the dashboard refreshes while any exist);
# `editable` runs let a reviewer correct the extracted field values.
STATUSES: list[dict[str, Any]] = [
    {"id": "new", "label": "New", "tone": "neutral", "active": True, "editable": False,
     "description": "Uploaded and waiting for the agent."},
    {"id": "processing", "label": "Processing", "tone": "running", "active": True, "editable": False,
     "description": "The agent is checking the invoice."},
    {"id": "needs_review", "label": "Needs review", "tone": "warn", "active": False, "editable": True,
     "description": "The agent found no blocking problem; a person needs to confirm it."},
    {"id": "returned", "label": "Returned", "tone": "bad", "active": False, "editable": True,
     "description": "Not compliant: returned for revision with the issues the agent found."},
    {"id": "finalized", "label": "Finalized", "tone": "ok", "active": False, "editable": False,
     "description": "Compliant and confirmed by a reviewer."},
    {"id": "error", "label": "Error", "tone": "error", "active": False, "editable": False,
     "description": "A technical problem stopped the check. Look at the run and re-evaluate it."},
]
STATUS_IDS = {s["id"] for s in STATUSES}

NEW = "new"
PROCESSING = "processing"
ERROR = "error"

# The agent's own decision (AgentStatus values), shown as "Agent decision: …".
VERDICT_LABELS = {
    "passed": "Passed",
    "failed": "Failed",
    "needs_review": "Needs review",
    "error": "Error",
    "interrupted": "Interrupted",
    "running": "Running",
}

_STATUS_FOR_VERDICT = {
    "passed": "needs_review",
    "needs_review": "needs_review",
    "failed": "returned",
    "error": "error",
    "interrupted": "error",
}

_FINISHED = [s["id"] for s in STATUSES if not s["active"]]

# `to`: the status the action moves the run to. Re-evaluate has none: it starts a new run.
# `preview`: shown in the page as a design preview only; the server refuses to carry it out.
ACTIONS: dict[str, dict[str, Any]] = {
    "confirm": {"label": "Confirm compliant", "from": ["needs_review"], "to": "finalized",
                "note": "Confirmed as compliant by a reviewer"},
    # Sending the invoice back needs a business layer that does not exist yet: the page opens
    # a message dialog, but nothing is sent and the status does not change.
    "return": {"label": "Return", "from": ["needs_review"], "to": None, "preview": True,
               "note": "Returned to the local partner"},
    "reevaluate": {"label": "Re-evaluate", "from": _FINISHED, "to": None,
                   "note": "Re-evaluated as a new run"},
}


def status_from_verdict(verdict: str) -> str:
    """Workflow status for a finished agent run; anything unexpected needs a person to look."""
    return _STATUS_FOR_VERDICT.get(verdict, ERROR)


def label(status: str) -> str:
    for s in STATUSES:
        if s["id"] == status:
            return s["label"]
    return status.replace("_", " ").capitalize()


def verdict_label(verdict: str) -> str:
    return VERDICT_LABELS.get(verdict) or verdict.replace("_", " ").capitalize()


def is_active(status: str) -> bool:
    return any(s["id"] == status and s["active"] for s in STATUSES)


def is_editable(status: str) -> bool:
    return any(s["id"] == status and s.get("editable") for s in STATUSES)


def catalog() -> dict[str, Any]:
    """What `/api/statuses` serves."""
    return {"statuses": STATUSES, "actions": ACTIONS, "verdicts": VERDICT_LABELS}
