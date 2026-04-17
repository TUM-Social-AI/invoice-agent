# Agent Architecture

This document explains how the agent decides which tool to call, which tools are available per phase, and how execution differs between `loop` and `pipeline` orchestration modes.

## 1) Tool Selection and Dispatch (Loop Mode)

Mapped files:
- `src/agent/agent.py` — orchestrator; `run()` delegates to `_run_agent_loop()`
- `src/agent/turn.py` — single LLM turn: prompt assembly, JSON schema, parse, retry
- `src/agent/action_contract.py` — validates and repairs the structured action
- `src/agent/registry.py` — thin assembler; wires factories from `tool_wrappers.py` into the runtime dict
- `src/tools/tool_wrappers.py` — all tool closure factories (`make_inspect`, `make_extract`, …)
- `src/agent/loop_guards.py` — `DuplicateActionGuard` + `ConsecutiveFailureGuard`
- `src/agent/llm_payload.py` — shared `build_payload()` used by `turn.py` and `reflection.py`
- `src/agent/response_schema.py` — `build_response_schema()` for dynamic per-turn JSON schema
- `src/agent/param_resolver.py` — `PARAM_ALIASES` + `resolve_param()` for LLM alias normalisation

Last validated with code: current working tree

```mermaid
flowchart TD
  runStart[RunStart] --> exposedMerge[MergeExposedTools]
  exposedMerge --> phaseFilter[PhaseToolFilter]
  phaseFilter --> promptBuild[BuildSystemPromptAndSchema]
  promptBuild --> llmAction[LLMReturnsAction]
  llmAction --> sanitize[SanitizeParams]
  sanitize --> contractCheck{ActionContractValid}
  contractCheck -->|"no"| repairTry[RepairRetryWithFeedback]
  repairTry --> contractRecheck{RepairedActionValid}
  contractRecheck -->|"no"| failTurn[TurnErrorFallbackOrStop]
  contractRecheck -->|"yes"| dispatch
  contractCheck -->|"yes"| dispatch[RegistryDispatchByToolName]
  dispatch --> toolExec[ExecuteToolWrapper_tool_wrappers.py]
  toolExec --> stateUpdate[RecordActionAndUpdateState]
  stateUpdate --> guards[LoopAndFailureGuards_loop_guards.py]
  guards --> doneCheck{FinishOrStopCondition}
  doneCheck -->|"no"| phaseFilter
  doneCheck -->|"yes"| runEnd[RunEnd]
```

How to read this:
- `MergeExposedTools` is policy-driven (`tool_groups_enabled`, learnings toggle, allow/deny overrides, mandatory `finish`/`note`).
- `PhaseToolFilter` narrows the set further based on phase state and guard conditions.
- `BuildSystemPromptAndSchema` constrains tool and params through dynamic JSON schema.
- Contract validation runs before dispatch; invalid actions get one repair retry before fallback/stop paths.

## 2) Per-Phase Available Tools

Mapped files:
- `src/agent/phases.py` — loads phase-to-tool map from `config/phase_tools.yaml` (hardcoded fallback)
- `src/agent/tool_policy.py` — `TOOL_GROUPS` access control + allow/deny override merge
- `config/phase_tools.yaml` — canonical phase-to-tool mappings (edit without touching Python)
- `config/config.yaml` — `agent.tool_groups_enabled`, `learnings_tools_enabled`, allow/deny overrides
- `src/agent/prompts.py` — system prompt builder; tool descriptions overridable via `config/tool_descriptions.yaml`

Last validated with code: current working tree

```mermaid
flowchart TD
  registryAll[RegistryTools] --> groupFilter[ToolGroupPolicy]
  groupFilter --> allowDeny[AllowDenyOverrides]
  allowDeny --> exposedSet[ExposedToolSet]
  exposedSet --> phaseGate[PhaseGateFilter]
  phaseGate --> scanSet[ScanPhaseTools]
  phaseGate --> extractSet[ExtractPhaseTools]
  phaseGate --> validateSet[ValidatePhaseTools]
  scanSet --> promptVisible[PromptVisibleAndCallable]
  extractSet --> promptVisible
  validateSet --> promptVisible
```

### 2.1) PhaseToToolSet quick reference

The list below reflects phase-gated tool sets from `src/agent/phases.py`, then intersected with the exposed set from `src/agent/tool_policy.py`.

- `SCAN`
  - `inspect_file`, `compress_pages`, `inventory_pages`, `classify_document_type`, `read_learnings`
  - Always-available controls: `note`, `install_package`
- `EXTRACT`
  - `convert_pdf_to_images`, `extract_fields_vision`, `crop_region`, `check_compliance`
  - Review/learnings helpers: `flag_for_human_review`, `flag_fields_for_review`, `read_learnings`, `write_learning`, `edit_learning`, `delete_learning`
  - Always-available controls: `note`, `install_package`
- `VALIDATE`
  - `check_compliance`, `check_compliance_visual`, `extract_fields_vision`, `crop_region`, `finish`
  - Review/learnings helpers: `flag_for_human_review`, `flag_fields_for_review`, `write_learning`, `edit_learning`, `delete_learning`
  - Always-available controls: `note`, `install_package`

Dynamic phase constraints:
- Before classification, allowed tools are tightened to one required step at a time: `compress_pages` -> `inventory_pages` -> `classify_document_type`.
- After full-quality render exists, `convert_pdf_to_images` is removed.
- In `VALIDATE`, repeated identical compliance outcomes can suppress repeated `check_compliance`.

Configuration notes:
- Baseline exposure comes from `agent.tool_groups_enabled` (default includes `pipeline` group).
- Learnings CRUD tools are included if `agent.learnings_tools_enabled: true`.
- `tools_extra_allow` and `tools_extra_deny` apply after group merge.
- `finish` and `note` are forcibly exposed in loop mode when present in registry.

## 3) Orchestration Modes

Mapped files:
- `src/agent/agent.py`
- `src/agent/pipeline.py`

Last validated with code: current working tree

```mermaid
flowchart TD
  runEntry[InvoiceAgentRun] --> modeCheck{OrchestrationMode}
  modeCheck -->|"pipeline"| fixedSeq[RunFixedPipeline]
  modeCheck -->|"loop"| loopSeq[RunAgentLoop]
  fixedSeq --> pInspect[inspect_file]
  pInspect --> pScan[compress_pagesAndInventoryAndClassify]
  pScan --> pExtract[convertAndExtract]
  pExtract --> pRules[check_complianceAndVisual]
  pRules --> pFinish[finish]
  loopSeq --> lChoose[LLMChoosesNextTool]
  lChoose --> lDispatch[RegistryDispatch]
  lDispatch --> lGuards[LoopGuardsAndRetries]
  lGuards --> lDone{StopCondition}
  lDone -->|"no"| lChoose
  lDone -->|"yes"| lFinish[FinalizeStatus]
```

## Diagram Maintenance Checklist

Update this file whenever any of the following changes:
- Tool registry keys change in `src/agent/registry.py`.
- Phase gates or phase definitions change in `src/agent/phases.py`.
- Group exposure or allow/deny merge logic changes in `src/agent/tool_policy.py`.
- Action schema/contract behavior changes in `src/agent/turn.py` or `src/agent/action_contract.py`.
- Orchestration branch behavior changes in `src/agent/agent.py` or `src/agent/pipeline.py`.

Recommended review routine:
1. Update diagram nodes/edges first.
2. Update `PhaseToToolSet` quick reference second.
3. Run one loop-mode and one pipeline-mode smoke test.
4. Include "diagram sync" note in the PR description when orchestration changes.
