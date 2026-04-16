"""Invoice agent orchestrator."""

import json
import logging
import time as _time
from pathlib import Path

from src.agent.state import AgentState, AgentStatus
from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.llm.config_resolve import prompt_limits_for_config, reasoning_model_for_config
from src.llm.factory import build_llm_provider

from src.agent.action_contract import validate_action_contract as _validate_action_contract
from src.agent.phases import phase_tool_names
from src.agent.fallbacks import (
    fallback_action_after_llm_failure as _fallback_action_after_llm_failure,
    reasoning_backend_unreachable as _reasoning_backend_unreachable,
)
from src.agent.registry import build_tool_registry
from src.agent.turn import agent_turn
from src.agent.reflection import reflect
from src.agent.loop_utils import open_run_log as _open_run_log, append_log_entry as _append_log_entry, timeout_cfg as _timeout_cfg, log_tool_result
from src.agent.tool_policy import merge_exposed_tool_names
from src.tools.tools import load_surya_models
from src.prompts.llm_prompts import PLANNING_SYSTEM_MESSAGE, build_planning_user_prompt

logger = logging.getLogger(__name__)


def _agent_cfg(config: dict) -> dict:
    a = config.get("agent", {})
    return {
        "max_turns": int(a.get("max_turns", 25)),
        "max_field_retries": int(a.get("max_field_retries", 3)),
        "confidence_threshold": float(a.get("confidence_threshold", 0.65)),
        "batch_review_threshold": float(a.get("batch_review_threshold", 0.85)),
        "planning_timeout_s": int(a.get("planning_timeout_s", 180)),
        "reflection_timeout_s": int(a.get("reflection_timeout_s", 120)),
        "max_consecutive_failures": int(a.get("max_consecutive_failures", 3)),
        "max_same_action_warn": int(a.get("max_same_action_warn", 2)),
        "max_same_action_stop": int(a.get("max_same_action_stop", 5)),
    }


def _phase_tool_names(state: AgentState, all_tools: list[str]) -> list[str]:
    """Backward-compatible adapter used by tests/importers."""
    return phase_tool_names(state, all_tools, set(all_tools))


class InvoiceAgent:
    def __init__(self, config: dict, store: ConfigStore):
        self.config = config
        self.store = store
        cfg = _agent_cfg(config)
        self.max_turns = cfg["max_turns"]
        self.max_field_retries = cfg["max_field_retries"]
        self.confidence_threshold = cfg["confidence_threshold"]
        self.batch_review_threshold = cfg["batch_review_threshold"]
        self.planning_enabled = bool(config.get("agent", {}).get("planning_enabled", True))
        self.fallback_reasoning_model = config.get("agent", {}).get("fallback_reasoning_model") or None
        _plim = prompt_limits_for_config(config)
        self.planning_learnings_max_chars = _plim["planning_learnings_max_chars"]
        self.orchestration = str(config.get("agent", {}).get("orchestration", "loop")).strip().lower()
        self.log_line_max_chars = int(config.get("agent", {}).get("log_line_max_chars", 120))
        timeouts = _timeout_cfg(config)
        self.chat_timeout_s = timeouts["chat_timeout_s"]
        self.generate_timeout_s = timeouts["generate_timeout_s"]
        self.planning_timeout_s = cfg["planning_timeout_s"]
        self.reflection_timeout_s = cfg["reflection_timeout_s"]
        self.max_consecutive_failures = cfg["max_consecutive_failures"]
        self.max_same_action_warn = cfg["max_same_action_warn"]
        self.max_same_action_stop = cfg["max_same_action_stop"]
        self.provider: LLMProvider = build_llm_provider(config)
        # Load surya OCR models once here — shared across all runs in this session.
        # Takes ~5-15s on first call; subsequent calls use cached weights.
        # If surya-ocr is not installed, this returns None and OCR is silently skipped.
        self.surya_models = load_surya_models()
        self.tools = build_tool_registry(config, store, surya_models=self.surya_models, provider=self.provider)

        # LLM tool exposure (loop mode only). This filters both:
        # - the tool enum used by `agent_turn`
        # - the tool descriptions embedded in the system prompt
        tool_groups_enabled = config.get("agent", {}).get("tool_groups_enabled", ["pipeline"])
        learnings_tools_enabled = bool(config.get("agent", {}).get("learnings_tools_enabled", False))
        tools_extra_allow = set(config.get("agent", {}).get("tools_extra_allow", []))
        tools_extra_deny = set(config.get("agent", {}).get("tools_extra_deny", []))
        self.exposed_tool_names = merge_exposed_tool_names(
            tool_groups_enabled=tool_groups_enabled,
            learnings_tools_enabled=learnings_tools_enabled,
            tools_extra_allow=tools_extra_allow,
            tools_extra_deny=tools_extra_deny,
            registry_keys=set(self.tools.keys()),
        )

    def run_reflection(self, state: AgentState, diff_text: str):
        """Run the reflection loop against a finished state + ground truth diff."""
        reflect(
            state,
            diff_text,
            self.config,
            self.store,
            self.tools,
            provider=self.provider,
            chat_timeout_s=self.reflection_timeout_s,
        )

    def _generate_plan(self, state: AgentState) -> list[dict]:
        """
        Generate a structured execution plan after classify_document_type has run.
        Called once state.invoice_type_id and state.page_inventory are known, so the
        plan can reference actual page roles and skip the SCAN steps already completed.
        Returns a list of {step, tool, rationale} dicts.
        Silently returns [] on any failure — planning is best-effort.
        """
        # Build page inventory summary for the prompt
        inventory_lines = []
        if state.page_inventory:
            for pg in state.page_inventory:
                inventory_lines.append(
                    f"  Page {pg.get('page')}: {pg.get('category', '?')} — {pg.get('description', '')[:120]}"
                )
        inventory_hint = (
            "Page inventory (already completed):\n" + "\n".join(inventory_lines)
            if inventory_lines else "Page inventory not available."
        )
        learnings_hint = (
            f"Loaded learnings for context:\n{state.learnings_context[:self.planning_learnings_max_chars]}"
            if state.learnings_context else "No prior learnings loaded yet."
        )
        inv_type = self.store.get_type(state.invoice_type_id)
        type_display = inv_type.display_name if inv_type else state.invoice_type_id
        planning_prompt = build_planning_user_prompt(
            file_name=Path(state.pdf_path).name,
            invoice_type_id=state.invoice_type_id,
            type_display=type_display,
            inventory_hint=inventory_hint,
            learnings_hint=learnings_hint,
        )

        payload = {
            "model": reasoning_model_for_config(self.config),
            "messages": [
                {"role": "system", "content": PLANNING_SYSTEM_MESSAGE},
                {"role": "user", "content": planning_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.1},
            "format": {
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "step": {"type": "integer"},
                                "tool": {"type": "string"},
                                "rationale": {"type": "string"},
                            },
                            "required": ["step", "tool", "rationale"],
                        },
                    }
                },
                "required": ["plan"],
            },
        }

        try:
            llm_result = self.provider.chat_json(
                model=payload["model"],
                messages=payload["messages"],
                response_format=payload.get("format"),
                temperature=payload.get("options", {}).get("temperature", 0.1),
                timeout_s=self.planning_timeout_s,
            )
            raw = llm_result.content_text
            parsed = llm_result.content_json if llm_result.content_json is not None else json.loads(raw)
            plan = parsed.get("plan", [])
            logger.info(f"  Execution plan ({len(plan)} steps): {[s.get('tool') for s in plan]}")
            return plan
        except Exception as e:
            logger.warning(f"  Plan generation failed (non-fatal): {e}")
            return []



## TODO: the run function is way too long and verbose and complicated. Plan on how to improve, modularize and uncomplicate it
    def run(self, pdf_path: str, output_dir: str, invoice_type_id: str = "") -> AgentState:
        _page_dpi = int(self.config.get("agent", {}).get("page_dpi", 150))
        state = AgentState(
            pdf_path=pdf_path,
            output_dir=output_dir,
            invoice_type_id=invoice_type_id,
            confidence_threshold=self.confidence_threshold,
            batch_review_threshold=self.batch_review_threshold,
            page_render_dpi=_page_dpi,
        )

        # Learnings hydration: load prior insights into state BEFORE the agent
        # starts extracting or evaluating compliance.
        # - If invoice_type_id is provided: load that type + GENERAL
        # - If invoice_type_id is empty: load GENERAL only
        try:
            if "read_learnings" in self.tools:
                self.tools["read_learnings"](state)
        except Exception as e:
            logger.warning(f"Learnings hydration failed (non-fatal): {e}")

        # Open per-run JSONL log before the loop — partial runs still get a log
        log_path, log_handle = _open_run_log(output_dir)
        state.run_log_path = log_path

        if invoice_type_id and not self.store.get_type(invoice_type_id):
            state.status = AgentStatus.ERROR
            state.finish_reason = f"Unknown invoice type: {invoice_type_id}"
            log_handle.close()
            return state

        logger.info(
            f"Agent starting | pdf={pdf_path} | type={invoice_type_id or 'auto-detect'} "
            f"| log={log_path}"
        )

        if hasattr(self.provider, "reset_meters"):
            self.provider.reset_meters()  # type: ignore[attr-defined]

        # Pipeline orchestration (deterministic fixed sequence; no LLM tool routing).
        if self.orchestration == "pipeline":
            from src.agent.pipeline import run_fixed_pipeline

            try:
                run_fixed_pipeline(
                    state=state,
                    tools=self.tools,
                    log_handle=log_handle,
                    log_line_max_chars=self.log_line_max_chars,
                    planning_enabled=self.planning_enabled,
                    generate_plan_fn=self._generate_plan if self.planning_enabled else None,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                logger.error(f"Pipeline orchestration failed: {e}", exc_info=True)
                state.status = AgentStatus.ERROR
                state.finish_reason = str(e)
            finally:
                if state.session_notes:
                    _append_log_entry(log_handle, {
                        "turn": state.turn,
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "tool": "__session_notes__",
                        "params": {},
                        "reasoning": "",
                        "result": {"notes": state.session_notes},
                        "elapsed_ms": 0,
                    })
                log_handle.close()
            return state

        consecutive_failures: dict[str, int] = {}  # tool_name → consecutive fail count
        last_action_sig: str = ""          # (tool, key-params) fingerprint of previous turn
        consecutive_same_action: int = 0
        null_extract_streak_by_key: dict[str, int] = {}

        try:
            while state.status == AgentStatus.RUNNING and state.turn < self.max_turns:
                turn_start = _time.monotonic()
                try:
                    action = agent_turn(
                        state, self.store, self.config,
                        self.max_field_retries, self.confidence_threshold,
                        tool_names=[
                            t for t in _phase_tool_names(state, list(self.tools.keys()))
                            if t in getattr(self, "exposed_tool_names", set())
                        ],
                        provider=self.provider,
                        chat_timeout_s=self.chat_timeout_s,
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(f"LLM call failed on turn {state.turn}: {e}")
                    fallback_action = _fallback_action_after_llm_failure(state, str(e))
                    if fallback_action:
                        logger.info(
                            f"  Applying deterministic fallback action: {fallback_action['tool']} "
                            f"with params={fallback_action.get('params', {})}"
                        )
                        action = fallback_action
                    else:
                        state.status = AgentStatus.ERROR
                        reason = str(e)
                        if _reasoning_backend_unreachable(reason):
                            reason = (
                                f"{reason}\n\nReasoning LLM request failed (HTTP/connection). "
                                "Check that the configured LLM provider is reachable and "
                                "the relevant API key or base_url in config is correct."
                            )
                        state.finish_reason = reason
                        _append_log_entry(log_handle, {
                            "turn": state.turn,
                            "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "tool": None,
                            "params": {},
                            "reasoning": "",
                            "result": {"error": reason},
                            "elapsed_ms": int((_time.monotonic() - turn_start) * 1000),
                        })
                        break

                tool_name = action.get("tool")
                params = action.get("params", {})
                reasoning = action.get("reasoning", "")

                logger.info(f"Turn {state.turn} | tool={tool_name}")
                if self.log_line_max_chars > 0:
                    logger.info(f"  reasoning: {reasoning[:self.log_line_max_chars]}")
                else:
                    logger.info(f"  reasoning: {reasoning}")
                if params:
                    if self.log_line_max_chars > 0:
                        logger.info(f"  params:    {str(params)[:self.log_line_max_chars]}")
                    else:
                        logger.info(f"  params:    {str(params)}")

                if tool_name not in self.tools:
                    logger.warning(
                        f"  UNKNOWN tool: '{tool_name}' — not in tool registry. "
                        f"Valid tools: {', '.join(sorted(self.tools))}"
                    )
                    result = {"error": f"unknown tool '{tool_name}'"}
                    state.record_action(tool_name, params, result, reasoning)
                    _append_log_entry(log_handle, {
                        "turn": state.turn - 1,
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                        "tool": tool_name,
                        "params": params,
                        "reasoning": reasoning,
                        "result": result,
                        "elapsed_ms": int((_time.monotonic() - turn_start) * 1000),
                    })
                    # Count unknown-tool calls toward consecutive failures so the
                    # guard can stop a hallucination loop (e.g. repeated invented tool names)
                    consecutive_failures[tool_name] = consecutive_failures.get(tool_name, 0) + 1
                    if consecutive_failures[tool_name] >= self.max_consecutive_failures:
                        logger.error(
                            f"Unknown tool '{tool_name}' called {self.max_consecutive_failures} "
                            f"times in a row — stopping to avoid infinite loop"
                        )
                        state.status = AgentStatus.ERROR
                        state.finish_reason = (
                            f"Unrecoverable: unknown tool '{tool_name}' called "
                            f"{self.max_consecutive_failures} consecutive times"
                        )
                    continue

                try:
                    result = self.tools[tool_name](state, **params)
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.error(f"  tool FAILED: {e}")
                    result = {"success": False, "error": str(e)}

                elapsed_ms = int((_time.monotonic() - turn_start) * 1000)
                log_tool_result(tool_name, result, self.log_line_max_chars)
                state.record_action(tool_name, params, result, reasoning)

                _append_log_entry(log_handle, {
                    "turn": state.turn - 1,  # record_action already incremented turn
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "tool": tool_name,
                    "params": params,
                    "reasoning": reasoning,
                    "result": result,
                    "elapsed_ms": elapsed_ms,
                })

                if tool_name == "extract_fields_vision" and isinstance(result, dict):
                    _mr = result.get("merge_result") or {}
                    if _mr.get("updated"):
                        state.compliance_same_result_streak = 0
                    _null_fields = _mr.get("null_fields") or []
                    _updated = _mr.get("updated") or []
                    _kept = _mr.get("already_have_better") or []
                    null_only = bool(_null_fields) and not _updated and not _kept
                    if null_only:
                        _null_fs = (
                            params.get("field_subset")
                            or params.get("fields")
                            or params.get("field_names")
                            or params.get("regions")
                        )
                        _null_key = json.dumps(
                            {
                                "page_num": params.get("page_num"),
                                "field_subset": sorted(_null_fs) if isinstance(_null_fs, list) else _null_fs,
                            },
                            sort_keys=True,
                        )
                        null_extract_streak_by_key[_null_key] = null_extract_streak_by_key.get(_null_key, 0) + 1
                        if null_extract_streak_by_key[_null_key] >= 2:
                            msg = (
                                "Repeated null-only extraction for same page/fields. "
                                "Try flag_for_human_review for missing fields or proceed with check_compliance."
                            )
                            logger.warning(f"  {msg}")
                            state.session_notes.append(msg)

                if tool_name == "finish":
                    break

                # After classify_document_type succeeds, generate the execution plan.
                # Done here (not upfront) so the plan is informed by the actual document
                # structure: invoice type, page inventory, and page roles are all known.
                if (
                    tool_name == "classify_document_type"
                    and state.invoice_type_id
                    and self.planning_enabled
                    and not state.execution_plan
                ):
                    logger.info("  Generating execution plan (document type known)...")
                    state.execution_plan = self._generate_plan(state)

                # ── Duplicate action guard ────────────────────────────────────
                # Build a canonical fingerprint that resolves param aliases so
                # page_path/image_path and page_num/page_index/page all map to
                # the same key. Without this, alias variants all look like empty
                # params and every call appears identical.
                _img = (
                    params.get("image_path")
                    or params.get("page_path")
                    or params.get("image")
                )
                _pg = (
                    params.get("page_num")
                    or params.get("page_index")
                    or params.get("page")
                )
                # Normalize all field_subset aliases so alternating between "fields",
                # "regions", "field_names" still counts as the same repeated call.
                _fs = (
                    params.get("field_subset")
                    or params.get("fields")
                    or params.get("field_names")
                    or params.get("regions")
                )
                sig_params = {k: v for k, v in {
                    "image_path":  _img,
                    "page_num":    _pg,
                    "field_subset": _fs,
                    "region":      params.get("region"),
                    "hints":       params.get("hints"),
                    "package":     params.get("package"),
                }.items() if v is not None}
                # check_compliance has no params — do not treat repeated calls as identical actions
                if tool_name == "check_compliance":
                    consecutive_same_action = 0
                else:
                    action_sig = f"{tool_name}|{json.dumps(sig_params, sort_keys=True)}"
                    if action_sig == last_action_sig:
                        consecutive_same_action += 1
                        if consecutive_same_action >= self.max_same_action_warn:
                            warning = (
                                f"LOOP DETECTED: '{tool_name}' called {consecutive_same_action + 1} "
                                f"times in a row with the same params. "
                                f"Change at least one of: page_num, region, hints, or field_subset "
                                f"before retrying; or call flag_for_human_review / check_compliance / finish."
                            )
                            logger.warning(f"  {warning}")
                            state.session_notes.append(warning)
                        if consecutive_same_action >= self.max_same_action_stop:
                            logger.error(
                                f"Hard-stopping: '{tool_name}' called "
                                f"{consecutive_same_action + 1} times in a row with identical params. "
                                f"Agent is stuck in a loop."
                            )
                            state.status = AgentStatus.FAILED
                            state.finish_reason = (
                                f"Loop: '{tool_name}' called {consecutive_same_action + 1} "
                                f"consecutive times with identical params"
                            )
                            break
                    else:
                        consecutive_same_action = 0
                    last_action_sig = action_sig
                # ─────────────────────────────────────────────────────────────

                # Track consecutive failures per tool — stop if unrecoverable
                tool_failed = result.get("success") is False or "error" in result
                if tool_failed:
                    consecutive_failures[tool_name] = consecutive_failures.get(tool_name, 0) + 1
                    if consecutive_failures[tool_name] >= self.max_consecutive_failures:
                        logger.error(
                            f"Tool '{tool_name}' failed {self.max_consecutive_failures} times in a row "
                            f"— stopping to avoid infinite loop"
                        )
                        state.status = AgentStatus.ERROR
                        state.finish_reason = (
                            f"Unrecoverable: '{tool_name}' failed "
                            f"{self.max_consecutive_failures} consecutive times"
                        )
                        break
                else:
                    consecutive_failures[tool_name] = 0

                # Adaptive model routing: escalate to fallback after 2 consecutive
                # failures on any tool; de-escalate once the agent succeeds again.
                if self.fallback_reasoning_model:
                    total_failures = sum(consecutive_failures.values())
                    state.use_fallback_model = total_failures >= 2
                    if state.use_fallback_model and not state.fallback_logged:
                        logger.info(
                            f"  Switching to fallback reasoning model: {self.fallback_reasoning_model}"
                        )
                        state.fallback_logged = True
                    elif not state.use_fallback_model:
                        state.fallback_logged = False

        except KeyboardInterrupt:
            logger.warning(f"Interrupted by user after turn {state.turn} — saving partial results")
            state.status = AgentStatus.INTERRUPTED
            state.finish_reason = "interrupted by user"

        finally:
            # Write session notes before closing so the log is always complete
            if state.session_notes:
                _append_log_entry(log_handle, {
                    "turn": state.turn,
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "tool": "__session_notes__",
                    "params": {},
                    "reasoning": "",
                    "result": {"notes": state.session_notes},
                    "elapsed_ms": 0,
                })
            log_handle.close()

        if state.turn >= self.max_turns and state.status == AgentStatus.RUNNING:
            state.status = AgentStatus.FAILED
            state.finish_reason = f"Max turns ({self.max_turns}) reached"

        logger.info(
            f"Agent done | status={state.status.value} | turns={state.turn} | log={log_path}"
        )
        return state
