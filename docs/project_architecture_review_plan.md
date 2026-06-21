# Project Architecture Review and Remediation Plan

Date: 2026-05-12

Scope: whole-repository scan of the invoice compliance agent: CLI, agent loop, tool registry, provider layer, config CSVs, tests, docs, and runtime hygiene.

## Executive Summary

The project has a solid core shape: configuration-driven invoice types/rules, a clear agent state object, separate LLM providers, phase-gated tools, deterministic pipeline mode, and useful tests around compliance/evaluation behavior. The main architectural risks are not in the basic decomposition; they are in reliability boundaries around the agent, test/config drift, runtime package installation, and provider-specific structured-output differences.

The highest-priority work is to restore the failing test suite, remove or hard-gate LLM-triggered package installation, make provider schema behavior explicit and tested, and tighten the compliance/visual backfill contract so the agent cannot silently produce unsupported state.

## P0 - Test Suite Is Currently Broken

Evidence:
- `pytest` fails during collection.
- `tests/test_compliance_visual_backfill.py:6-10` imports `_parse_employee_name_from_visual_observation` and `_parse_payment_phrase_from_visual_observation`.
- `src/tools/compliance_visual.py:50-115` contains `_merge_visual_field_updates` and `_is_short_date_fragment`, but the imported parser functions are absent.
- `tests/test_compliance_visual_backfill.py:66-70` also expects `ConfigStore.observation_fallbacks_for`, which is not implemented in `src/config/loader.py`.

Impact:
- CI cannot run any test beyond collection.
- Architectural changes are high risk because the safety net is offline before execution starts.

Resolution strategy:
1. Decide whether observation fallback parsing is still part of the product contract.
2. If yes, add a typed `ObservationFallback` config model, CSV loader, `ConfigStore.observation_fallbacks_for(invoice_type_id)`, and the two parser helpers tested by `test_compliance_visual_backfill.py`.
3. If no, delete or rewrite the stale tests to assert the newer `field_updates` contract only.
4. Add a smoke test that imports every module under `src/` and runs config loading.
5. Re-run `pytest` as the first acceptance gate.

## P0 - Runtime Package Installation Tool Is Unsafe for an Agent

Evidence:
- `src/tools/misc_tools.py:44-64` lets the agent execute `python -m pip install <package>`.
- `src/tools/tool_wrappers.py:778-782` exposes this as `install_package`.
- `src/agent/phases.py:20` makes `install_package` always available at the phase layer.
- `config/config.yaml` denies it via `tools_extra_deny`, but the implementation and docs still normalize the idea that the LLM can self-install packages.

Impact:
- If enabled by config or future refactor, an LLM-controlled loop can mutate the runtime environment and pull arbitrary packages.
- This is especially risky for an invoice agent handling sensitive financial documents.

Resolution strategy:
1. Remove `install_package` from default registry exposure and phase defaults.
2. Replace it with a non-mutating `dependency_status` tool that reports missing optional dependencies.
3. If installation is still useful, implement it as an explicit CLI command or human-approved maintenance task, never an LLM-callable tool.
4. Update `README.md`, `docs/tools.md`, `config/phase_tools.yaml`, and `src/agent/prompts.py` to remove the agent self-healing package-install story.

## P1 - Gemini Structured Output Is Incomplete for Reasoning Turns

Evidence:
- `src/agent/turn.py` builds a strict action schema and passes it to `provider.chat_json`.
- `src/llm/gemini_provider.py:58-86` only sets JSON mode through `messages_to_gemini_body`; it does not pass a `responseSchema` for chat turns.
- `src/llm/gemini_provider.py:113-116` recently added `responseSchema` for `generate_json`, but not for `chat_json`.

Impact:
- The reasoning loop is much more constrained on Ollama than Gemini.
- Gemini can still return valid JSON with the wrong branch/params, increasing repair retries and tool-call drift.

Resolution strategy:
1. Extend `messages_to_gemini_body` or `GeminiProvider.chat_json` to include provider-compatible `responseSchema`.
2. Add unit tests asserting that Gemini chat requests include `responseMimeType` and `responseSchema` when a schema is passed.
3. Add fallback behavior for schema incompatibilities by model family, with a logged warning and normal contract validation.

## P1 - Visual Backfill Contract Is Too Loose

Evidence:
- `src/prompts/llm_prompts.py` asks the vision model to emit arbitrary `field_updates`.
- `src/tools/compliance_visual.py:50-111` merges those updates into `state.extracted_fields` and flags them for review.
- Missing parser/fallback tests suggest the previous deterministic backfill strategy was partly removed.

Impact:
- Visual compliance observations can mutate extraction state without deterministic provenance or parser-specific validation.
- The agent may satisfy downstream field rules based on a visual rule output that was intended primarily as compliance evidence.

Resolution strategy:
1. Treat visual `field_updates` as candidates, not normal extracted fields.
2. Store them in a separate `state.field_candidates` or use a `source_type=visual_candidate` marker.
3. Require deterministic validation per supported field before promotion to `extracted_fields`.
4. Add tests for employee names, payment phrases, denylist behavior, existing-value precedence, and invalid schema keys.

## P1 - Pipeline Mode Can Attempt a Passing Finish Prematurely

Evidence:
- `src/agent/pipeline.py:175-178` always calls `finish(..., all_errors_resolved=True)`.
- The finish tool independently checks state, but the pipeline’s intent signal is optimistic even after failed visual checks or skipped field checks.

Impact:
- The pipeline can mask its own uncertainty and depend on finish rejection rather than making a principled final decision.
- Logs become harder to interpret during batch evaluation.

Resolution strategy:
1. Compute `all_errors_resolved` from `state.rule_results`, `state.visual_checks_pending`, and `state.skipped_checks` before calling finish.
2. Use `reason="human_review_needed"` when required fields are missing or backfilled candidates remain unverified.
3. Add a pipeline test where visual checks fail and finish must not claim completion.

## P1 - Tool/Phase Configuration Has Split-Brain Defaults

Evidence:
- `config/phase_tools.yaml` documents phase mappings.
- `src/agent/phases.py:20` still forces `_ALWAYS_AVAILABLE = {"note", "install_package"}` into every phase.
- `src/agent/tool_policy.py:67-70` independently forces `finish` and `note` into exposure.

Impact:
- The effective tool surface is not fully explainable from config.
- Security-sensitive tools can survive config edits by being hardcoded elsewhere.

Resolution strategy:
1. Make config the source of truth for phase availability.
2. Keep only `note`/`finish` as explicit code-level controls if needed, and document why.
3. Add a test that compares `config/phase_tools.yaml`, tool policy, and registry keys for consistency.

## P1 - Execution Plans Are Advisory, Not Enforced

Evidence:
- `InvoiceAgent._generate_plan` creates `state.execution_plan`.
- `AgentState.summary_for_prompt` only shows a brief "next tools" summary and progress is inferred by matching tool names.
- The loop does not enforce plan ordering, page targets, or planned second extraction passes.

Impact:
- The LLM can skip planned extraction passes and jump to compliance early.
- A plan can look reassuring in logs while doing little to constrain behavior.

Resolution strategy:
1. Convert plans into actionable milestones with required tool, page role, page_num, and completion predicate.
2. Track completion by semantic predicate, not only by tool name.
3. Add a loop guard that warns or blocks when the agent skips required extraction pages before compliance.
4. Keep deterministic pipeline as the reliable path for batch scoring until this is implemented.

## P2 - Import Hygiene and Copy/Paste Debris Obscure Ownership

Evidence:
- Several modules import unused `base64`, `ast`, `subprocess`, `sys`, `time`, `dataclass`, `Image`, and domain types.
- Examples include `src/tools/compliance_visual.py:3-16`, `src/tools/vision_llm.py:3-16`, and `src/tools/pdf_pages.py:3-16`.

Impact:
- The code looks mechanically split and makes it harder to see real dependencies.
- Security review is noisier because dangerous modules appear imported where they are not used.

Resolution strategy:
1. Add `ruff` or equivalent linting.
2. Enable at least unused-import and basic complexity checks.
3. Clean one package at a time to keep diffs reviewable.

## P2 - Generated Runtime Files Are Present in the Workspace

Evidence:
- The workspace contains many `__pycache__`, `.pyc`, `.DS_Store`, `.pytest_cache`, `output/`, and local invoice artifacts.
- `.gitignore` now ignores common caches and runtime output, but existing local debris remains.

Impact:
- Local scans are noisy.
- It is easy to accidentally include runtime artifacts in future commits.

Resolution strategy:
1. Keep the new ignore rules.
2. Add a maintenance task or documented command to clean ignored files.
3. Before releases, run `git status --ignored --short` and verify only expected local artifacts remain ignored.

## P2 - Dependency and Environment Story Needs Pinning

Evidence:
- `requirements.txt` uses broad lower bounds and includes OCR backend packages without tight version pinning.
- Runtime behavior changes substantially depending on local Ollama/Gemini model versions and OCR backend availability.

Impact:
- Reproducibility is weak across machines.
- Optional OCR can become an install blocker even though the code can skip it.

Resolution strategy:
1. Split runtime, optional OCR, and dev dependencies.
2. Add a lock file or tested constraints file.
3. Add a startup diagnostic command that reports provider, model IDs, OCR status, and config file paths.

## P2 - Compliance Rule State Needs Stronger Invariants

Evidence:
- `check_compliance` reuses definitive visual results, tracks skipped checks, and computes `all_errors_resolved`.
- Visual checks can also merge rule results and update extracted fields.

Impact:
- Multiple tools mutate overlapping compliance state, increasing the chance of stale pending lists or contradictory statuses.

Resolution strategy:
1. Centralize rule-result merging in one helper.
2. Add invariants: one current result per rule, pending visual IDs exclude definitive visual IDs, skipped error checks block pass.
3. Add property-style tests over field-based and visual result sequences.

## P3 - Documentation Is Good but Drifts from Current Defaults

Evidence:
- README still emphasizes local Ollama-only processing while `config/config.yaml` defaults to Gemini.
- Docs describe `install_package` as part of normal agent capabilities.

Impact:
- Operators may misunderstand data-flow/privacy implications.
- Setup instructions can lead to wrong model/provider expectations.

Resolution strategy:
1. Update README to describe both local and Gemini modes clearly.
2. Add a privacy note: Gemini mode sends document images/text to Google APIs.
3. Remove or quarantine `install_package` documentation.

## Acceptance Checklist

- `pytest` passes locally.
- Provider schema tests cover Ollama and Gemini chat/generate paths.
- `install_package` is no longer LLM-callable in default or configured phases.
- Pipeline finish uses computed state, not hardcoded optimism.
- Visual field updates are either deterministic promotions or explicitly marked candidates.
- A config/registry/phase consistency test fails when docs/config/code drift.
- README accurately reflects current default provider and privacy posture.
