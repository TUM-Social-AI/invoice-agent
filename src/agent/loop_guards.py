"""Loop guard classes for the main agent loop.

The agent loop in ``agent.py`` uses two guards to detect stuck states and
stop before wasting turns or budget:

:class:`DuplicateActionGuard`
    Detects when the agent calls the same tool with identical parameters
    consecutively.  Issues a warning after ``warn_threshold`` repeats and
    hard-stops after ``stop_threshold`` repeats.  ``check_compliance`` is
    intentionally exempt — repeating it after new fields are extracted is
    valid and useful.

:class:`ConsecutiveFailureGuard`
    Detects when a specific tool fails too many times in a row.  Also used
    for unknown-tool hallucinations.  Tracks failures per-tool so one broken
    tool does not unfairly penalise others.  Also exposes ``total_failures``
    so the adaptive-model-routing logic can decide when to switch to the
    fallback reasoning model.
"""

from __future__ import annotations

import json

from src.agent.param_resolver import resolve_sig_params


class DuplicateActionGuard:
    """Detects consecutive identical tool calls and signals warn / stop.

    Args:
        warn_threshold: Number of consecutive identical calls before a
                        warning is issued (0-indexed streak, so 2 means
                        the *third* repeated call warns).
        stop_threshold: Number of consecutive identical calls before a
                        hard-stop signal is returned.
    """

    def __init__(self, warn_threshold: int, stop_threshold: int) -> None:
        self._warn = warn_threshold
        self._stop = stop_threshold
        self._last_sig: str = ""
        self._streak: int = 0

    @property
    def streak(self) -> int:
        """Current consecutive repeat count for the most recent tool call."""
        return self._streak

    def check_and_record(self, tool_name: str, params: dict) -> str | None:
        """Record this call and return a signal if a threshold is crossed.

        Args:
            tool_name: Name of the tool being called.
            params:    Raw params dict from the agent action.

        Returns:
            ``"warn"`` if the warn threshold was just reached,
            ``"stop"`` if the stop threshold was just reached, or
            ``None`` if the call is fine (different from previous or below warn).
        """
        # check_compliance is always exempt: repeating it after new fields is valid.
        if tool_name == "check_compliance":
            self._streak = 0
            return None

        sig = f"{tool_name}|{json.dumps(resolve_sig_params(params), sort_keys=True)}"
        if sig == self._last_sig:
            self._streak += 1
            if self._streak >= self._stop:
                return "stop"
            if self._streak >= self._warn:
                return "warn"
            return None
        else:
            self._streak = 0
            self._last_sig = sig
            return None


class ConsecutiveFailureGuard:
    """Tracks per-tool consecutive failures and signals when to hard-stop.

    Args:
        max_failures: Number of consecutive failures for a single tool before
                      ``should_stop`` returns ``True``.
    """

    def __init__(self, max_failures: int) -> None:
        self._max = max_failures
        self._counts: dict[str, int] = {}

    @property
    def total_failures(self) -> int:
        """Sum of all per-tool consecutive failure counts.

        Used by the adaptive-model-routing logic: if the total exceeds 2 the
        agent switches to the fallback reasoning model.
        """
        return sum(self._counts.values())

    def record_failure(self, tool_name: str) -> bool:
        """Increment the failure count for *tool_name* and return whether to stop.

        Returns:
            ``True`` if the per-tool count has reached ``max_failures``.
        """
        self._counts[tool_name] = self._counts.get(tool_name, 0) + 1
        return self._counts[tool_name] >= self._max

    def record_success(self, tool_name: str) -> None:
        """Reset the failure count for *tool_name* to zero."""
        self._counts[tool_name] = 0

    def count(self, tool_name: str) -> int:
        """Return the current consecutive failure count for *tool_name*."""
        return self._counts.get(tool_name, 0)
