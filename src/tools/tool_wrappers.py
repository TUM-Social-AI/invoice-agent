"""Tool closure factories for the invoice agent.

Each public function (``make_*``) accepts a :class:`ToolContext` and returns
a callable with signature ``(state: AgentState, **kwargs) -> dict``.
``build_tool_registry`` in ``src/agent/registry.py`` constructs a
``ToolContext`` from the current run's config and calls these factories to
assemble the tool dict — keeping registry.py a thin assembler and keeping all
tool logic here.

Utility helpers shared across multiple tools
--------------------------------------------
- :func:`_coerce_page_num`      — parse raw page-num values from the LLM
- :func:`_resolve_image_path`   — resolve image path / page num from kwargs
- :func:`_auto_expand_fields`   — batch field expansion for extract_fields_vision
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agent.state import AgentState, AgentStatus, rule_verdict_summary
from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.agent.param_resolver import resolve_param
from src.tools.tools import (
    inspect_file,
    compress_pages,
    classify_document_type,
    convert_pdf_to_images,
    render_medium_pages,
    crop_region,
    extract_fields_vision,
    merge_extracted_fields,
    check_compliance,
    check_compliance_visual,
    flag_for_human_review,
    read_learnings,
    write_learning,
    edit_learning,
    delete_learning,
    install_package,
    inventory_pages,
    _ocr_with_layout,
    _localize_field_in_ocr,
    _union_bboxes,
    _save_image_crop,
)
from src.models.action_models import (
    CheckVisualParams,
    CropRegionParams,
    ExtractFieldsParams,
    FinishParams,
    FlagParams,
)

logger = logging.getLogger(__name__)

_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")


# ---------------------------------------------------------------------------
# Shared runtime context
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    """Bundle of resolved config values shared across all tool closures.

    Created once per run in ``build_tool_registry`` and captured by every
    tool factory.  Avoids threading raw ``config`` dicts through every tool.
    """
    ollama_url: str
    vision_model: str
    learnings_path: str
    agent_cfg: dict
    timeouts: dict
    learnings_max_chars: int
    visual_max_evidence_pages: int
    ocr_prompt_max_chars: int
    ocr_langs: list[str]
    active_rule_groups: list
    store: ConfigStore
    provider: LLMProvider | None
    ocr_engine: Any

    # -- helpers ----------------------------------------------------------------

    def cap_learnings(self, text: str) -> str:
        """Truncate learnings text to the configured cap."""
        if not text:
            return ""
        if self.learnings_max_chars <= 0:
            return text
        return text if len(text) <= self.learnings_max_chars else text[:self.learnings_max_chars]

    def cap_ocr_prompt_text(self, text: str) -> str:
        """Truncate OCR text symmetrically to fit the prompt cap."""
        if self.ocr_prompt_max_chars <= 0 or len(text) <= self.ocr_prompt_max_chars:
            return text
        head = self.ocr_prompt_max_chars // 2
        tail_budget = self.ocr_prompt_max_chars - head - 72
        if tail_budget < 1:
            return text[:self.ocr_prompt_max_chars]
        sep = "\n...[OCR text truncated for prompt size]...\n"
        return text[:head] + sep + text[-tail_budget:]

    def default_convert_dpi(self, state: AgentState, kwargs: dict) -> int:
        """Return the DPI to use for convert_pdf_to_images.

        Prefers an explicit ``dpi`` kwarg, then the value already stored on
        ``state.page_render_dpi``, then the config default.
        """
        if kwargs.get("dpi") is not None:
            return int(kwargs["dpi"])
        return int(getattr(state, "page_render_dpi", None) or self.agent_cfg.get("page_dpi", 150))


# ---------------------------------------------------------------------------
# Standalone utility functions
# ---------------------------------------------------------------------------

def _coerce_page_num(raw: Any) -> int:
    """Parse a raw page-number value from LLM output into a 1-based int.

    Handles lists (takes first element), comma-separated strings, and strings
    with non-digit characters.
    """
    if raw is None:
        return 1
    if isinstance(raw, list):
        raw = raw[0] if raw else 1
    first = str(raw).split(",")[0].strip()
    digits = re.sub(r"[^\d]", "", first)
    return int(digits) if digits else 1


def _resolve_image_path(
    kwargs: dict,
    state: AgentState,
    *,
    label: str = "",
    require_page_num: bool = False,
) -> tuple[str | None, int, str | None]:
    """Resolve (image_path, page_num, error) from raw tool-call kwargs.

    When *require_page_num* is True (used by ``extract_fields_vision`` and
    ``check_compliance_visual``) only ``state.page_image_paths[page_num]``
    is consulted — the LLM's ``image_path`` value is ignored.  For
    ``crop_region`` (require_page_num=False) an explicit path is accepted
    first, falling back to page lookup.

    Returns:
        A 3-tuple of (resolved_path_or_None, page_num, error_string_or_None).
        If error is not None the caller should return an error result immediately.
    """
    _raw_page = (
        kwargs.get("page_num")
        or kwargs.get("page_index")
        or kwargs.get("page")
        or kwargs.get("page_number")
    )
    page_given = _raw_page is not None
    page_num = _coerce_page_num(_raw_page)

    if require_page_num:
        if not state.page_image_paths:
            return None, page_num, "No rendered pages yet — call convert_pdf_to_images first."
        if not page_given:
            return (
                None,
                page_num,
                "page_num is required — do not invent image_path or page_path placeholders.",
            )
        idx = max(0, min(page_num - 1, len(state.page_image_paths) - 1))
        image_path = state.page_image_paths[idx]
        logger.info(f"  {label or 'tool'}: page_num={page_num} → {image_path}")
        return image_path, page_num, None

    # crop_region: accept an explicit path when it exists and looks valid.
    # Named aliases resolved via param_resolver; extension-based fallback
    # handles ad-hoc path keys the LLM may invent.
    image_path = resolve_param(kwargs, "image_path") or next(
        (v for v in kwargs.values() if isinstance(v, str) and v.lower().endswith(_IMG_EXTS)),
        None,
    ) or next(
        (v[0] for v in kwargs.values()
         if isinstance(v, list) and v and isinstance(v[0], str) and v[0].lower().endswith(_IMG_EXTS)),
        None,
    )

    if page_given and state.page_image_paths:
        idx = max(0, min(page_num - 1, len(state.page_image_paths) - 1))
        image_path = state.page_image_paths[idx]
        logger.info(f"  {label or 'tool'}: page_num={page_num} → {image_path}")
    elif image_path:
        raw = str(image_path).strip()
        if re.match(r"(?i)^(page_path\d*|page_path)$", raw) or raw.lower() in ("path", "image"):
            return None, page_num, f"Invalid image_path placeholder: {image_path}"
        try:
            exists = Path(raw).expanduser().exists()
        except OSError:
            exists = False
        allowed = (
            set(state.page_image_paths or [])
            | set(getattr(state, "medium_page_paths", None) or [])
            | set(state.compressed_page_paths or [])
        )
        if not exists and raw not in allowed:
            return (
                None,
                page_num,
                f"image_path not found and not a known rendered page file: {image_path}",
            )
    elif not image_path and state.page_image_paths:
        logger.warning(
            f"  {label or 'tool'}: missing page target (no image_path/page_num) "
            f"| kwargs keys: {list(kwargs)}"
        )
    return image_path, page_num, None


def _auto_expand_fields(
    kwargs: dict,
    full_schema: dict,
    state: AgentState,
    agent_cfg: dict,
) -> dict:
    """Return the effective extraction schema for one vision call.

    Starts from whatever field subset the agent requested, then fills
    remaining slots with every field that has never been attempted — so
    un-tried fields get a free ride in the same vision call (cuts turns
    by ~60-70%).  The caller gates this function with ``batch_auto_expand``
    from config so it can be disabled when exact field control is needed.

    Args:
        kwargs:      Raw tool-call keyword arguments from the agent.
        full_schema: All fields defined for the current invoice type.
        state:       Current agent state (used to check retry counts).
        agent_cfg:   The ``agent`` section of config (for ``micro_tools_phase2``
                     and the MAX_BATCH_FIELDS cap).

    Returns:
        A filtered schema dict (subset of ``full_schema``).
    """
    MAX_BATCH_FIELDS = 8 if agent_cfg.get("micro_tools_phase2") else 15
    _raw_subset = resolve_param(kwargs, "field_subset") or []
    if isinstance(_raw_subset, str):
        _raw_subset = [s.strip() for s in _raw_subset.split(",") if s.strip()]
    requested_subset_list = [x for x in _raw_subset if x]
    requested_subset = set(requested_subset_list)
    never_attempted = {
        k for k in full_schema
        if state.get_field_retry_count(k) == 0
        and k not in state.extracted_fields
    }
    if len(requested_subset) > MAX_BATCH_FIELDS:
        seen: set[str] = set()
        trimmed: list[str] = []
        for f in requested_subset_list:
            if f not in seen and f in requested_subset:
                trimmed.append(f)
                seen.add(f)
            if len(trimmed) >= MAX_BATCH_FIELDS:
                break
        requested_subset = set(trimmed)
        extras_allowed = 0
    else:
        extras_allowed = max(0, MAX_BATCH_FIELDS - len(requested_subset))
    extras = set(sorted(never_attempted - requested_subset)[:extras_allowed])
    effective_subset = requested_subset | extras
    if effective_subset and effective_subset != set(full_schema):
        schema = {k: v for k, v in full_schema.items() if k in effective_subset}
        if extras:
            logger.debug(
                f"  batch expansion: +{len(extras)} un-attempted fields "
                f"added to this call ({', '.join(sorted(extras))})"
            )
        return schema
    return full_schema


# ---------------------------------------------------------------------------
# Tool factory functions
# ---------------------------------------------------------------------------

def make_inspect(ctx: ToolContext):
    """Return the inspect_file tool callable. Reads PDF metadata and page count."""
    def _inspect(state: AgentState, **kwargs):
        return inspect_file(state)
    return _inspect


def make_compress(ctx: ToolContext):
    """Return the compress_pages tool callable. Renders low-res thumbnails for the SCAN phase."""
    def _compress(state: AgentState, **kwargs):
        return compress_pages(
            state,
            dpi=kwargs.get("dpi", 96),
            quality=kwargs.get("quality", 65),
            max_width=kwargs.get("max_width", 1400),
        )
    return _compress


def make_classify(ctx: ToolContext):
    """Return the classify_document_type tool callable.

    On successful reclassification the tool immediately hydrates learnings
    for the new invoice type so subsequent turns benefit from them.
    """
    def _classify(state: AgentState, **kwargs):
        old_type = state.invoice_type_id
        result = classify_document_type(
            state,
            ctx.store,
            ctx.ollama_url,
            ctx.vision_model,
            provider=ctx.provider,
            timeout_s=ctx.timeouts["generate_timeout_s"],
        )
        if result.get("success") and state.invoice_type_id != old_type:
            new_section = read_learnings(state.invoice_type_id, ctx.learnings_path)
            state.learnings_context = ctx.cap_learnings(new_section)
            logger.info(f"  learnings refreshed for reclassified type: {state.invoice_type_id}")
        return result
    return _classify


def make_convert_pdf(ctx: ToolContext):
    """Return the convert_pdf_to_images tool callable.

    When hybrid_extraction is enabled, also renders medium-res pages used
    for the first-pass extraction attempt.
    """
    def _convert_pdf(state: AgentState, **kwargs):
        res = convert_pdf_to_images(state, dpi=ctx.default_convert_dpi(state, kwargs))
        if (
            res.get("success")
            and bool(ctx.agent_cfg.get("hybrid_extraction", True))
            and state.page_image_paths
        ):
            m = render_medium_pages(state)
            if not m.get("success"):
                logger.warning("  render_medium_pages failed (non-fatal): %s", m.get("error"))
        return res
    return _convert_pdf


def make_crop(ctx: ToolContext):
    """Return the crop_region tool callable. Extracts a named region crop from a page image."""
    def _crop(state: AgentState, **kwargs):
        parsed = CropRegionParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        image_path, page_num, res_err = _resolve_image_path(kwargs, state, label="crop_region")
        if res_err:
            return {"success": False, "error": res_err}
        if not image_path:
            return {"success": False, "error": "image_path not provided and no pages rendered yet"}
        return crop_region(
            state,
            image_path=image_path,
            region=kwargs["region"],
            page_num=page_num,
            custom_bbox=kwargs.get("custom_bbox"),
        )
    return _crop


def make_extract(ctx: ToolContext):
    """Return the extract_fields_vision tool callable.

    Implements a three-step extraction pipeline:
    1. OCR-direct: high-confidence fields resolved without a vision call.
    2. OCR-crop:   localized fields extracted from a region crop via vision.
    3. Fallback:   remaining fields extracted from the full-page image via vision.

    Auto-expansion (see _auto_expand_fields) piggybacks un-attempted fields
    onto the same vision call to reduce total turn count.
    """
    def _extract(state: AgentState, **kwargs):
        parsed = ExtractFieldsParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        if not state.invoice_type_id:
            return {
                "success": False,
                "error": (
                    "Cannot extract fields before the document type is known. "
                    "Call classify_document_type first, then convert_pdf_to_images, "
                    "then extract_fields_vision."
                ),
            }
        image_path, page_num, res_err = _resolve_image_path(
            kwargs, state, label="extract_fields_vision", require_page_num=True,
        )
        if res_err:
            return {"success": False, "error": res_err}
        if not image_path:
            return {"success": False, "error": "No rendered pages — call convert_pdf_to_images first."}
        full_path = image_path
        if state.compressed and state.page_image_paths and state.page_image_paths == state.compressed_page_paths:
            _dpi = ctx.default_convert_dpi(state, {})
            return {
                "success": False,
                "error": (
                    "Images are still 48 DPI compressed thumbnails (from compress_pages). "
                    f"Call convert_pdf_to_images(dpi={_dpi}) first, then retry extract_fields_vision. "
                    "Compressed thumbnails are only suitable for inventory_pages and classify_document_type."
                ),
            }
        full_schema = ctx.store.build_extraction_schema(state.invoice_type_id)

        # ── Batch auto-expansion ──────────────────────────────────────────────
        if ctx.agent_cfg.get("batch_auto_expand", True):
            schema = _auto_expand_fields(kwargs, full_schema, state, ctx.agent_cfg)
        else:
            _raw = resolve_param(kwargs, "field_subset") or []
            if isinstance(_raw, str):
                _raw = [s.strip() for s in _raw.split(",") if s.strip()]
            schema = {k: v for k, v in full_schema.items() if k in set(_raw)} if _raw else full_schema
        # ─────────────────────────────────────────────────────────────────────

        def _needs_full_res_after_medium(r: dict) -> bool:
            if not r.get("success"):
                return True
            mr = r.get("merge_result") or {}
            nf = mr.get("null_fields", 0)
            return nf if isinstance(nf, int) else len(nf)

        def _run_extract_on_image(img_path: str) -> dict:
            # ── OCR-guided extraction ─────────────────────────────────────────
            OCR_DIRECT_THRESHOLD = 0.80
            ocr = _ocr_with_layout(img_path, ocr_engine=ctx.ocr_engine)
            if not ocr.is_empty():
                logger.debug(f"  OCR layout: {len(ocr.lines)} lines, {len(ocr.full_text)} chars")

            ocr_direct: dict = {}   # field_name → (value_text, confidence)
            ocr_crop: dict = {}     # field_name → FieldLocalization
            fallback: dict = {}     # field_name → field_meta

            for field_name, field_meta in schema.items():
                loc = _localize_field_in_ocr(field_meta, ocr)
                if loc is None:
                    fallback[field_name] = field_meta
                elif loc.value_confidence >= OCR_DIRECT_THRESHOLD and loc.value_text:
                    ocr_direct[field_name] = (loc.value_text, loc.value_confidence)
                else:
                    ocr_crop[field_name] = loc

            logger.debug(
                f"  extraction routing: {len(ocr_direct)} OCR-direct, "
                f"{len(ocr_crop)} OCR-crop, {len(fallback)} fallback-vision"
            )

            hints = kwargs.get("hints", "")
            combined_merge: dict = {"updated": [], "kept_existing": 0, "null_fields": 0, "skipped": 0}
            any_success = False

            def _accumulate(merge_r: dict) -> None:
                combined_merge["updated"].extend(merge_r.get("updated", []))
                combined_merge["kept_existing"] += merge_r.get("kept_existing", 0)
                _null_raw = merge_r.get("null_fields", 0)
                combined_merge["null_fields"] += (
                    _null_raw if isinstance(_null_raw, int) else len(_null_raw)
                )

            # ── Step 1: OCR-direct fields (no vision needed) ──────────────────
            if ocr_direct:
                direct_extraction = {}
                for fname, (val, conf) in ocr_direct.items():
                    direct_extraction[fname] = val
                    direct_extraction[f"{fname}_confidence"] = conf
                direct_schema = {k: schema[k] for k in ocr_direct}
                _accumulate(merge_extracted_fields(
                    state, direct_extraction, direct_schema,
                    source_page=page_num, source_region="ocr_direct",
                ))
                any_success = True

            # ── Step 2: OCR-crop fields (one vision call per region group) ─────
            if ocr_crop:
                from collections import defaultdict
                region_groups: dict = defaultdict(list)
                for fname, loc in ocr_crop.items():
                    region = schema[fname].get("region", "body")
                    region_groups[region].append((fname, loc))

                for region, fields_in_region in region_groups.items():
                    bboxes = [loc.value_bbox for _, loc in fields_in_region]
                    crop_bbox = _union_bboxes(bboxes, ocr.image_width, ocr.image_height)
                    crop_path = _save_image_crop(
                        img_path, crop_bbox, state.output_dir, f"p{page_num}_{region}",
                    )
                    sub_schema = {fname: schema[fname] for fname, _ in fields_in_region}
                    cx1, cy1, cx2, cy2 = crop_bbox
                    crop_ocr_lines = [
                        l for l in ocr.lines
                        if l.bbox[0] < cx2 and l.bbox[2] > cx1
                        and l.bbox[1] < cy2 and l.bbox[3] > cy1
                    ]
                    crop_text = "\n".join(l.text for l in crop_ocr_lines if l.text.strip())
                    crop_result = extract_fields_vision(
                        state,
                        image_path=crop_path,
                        schema=sub_schema,
                        hints=hints,
                        ollama_url=ctx.ollama_url,
                        model=ctx.vision_model,
                        text_context=ctx.cap_ocr_prompt_text(crop_text),
                        provider=ctx.provider,
                        timeout_s=ctx.timeouts["generate_timeout_s"],
                    )
                    if crop_result["success"]:
                        _accumulate(merge_extracted_fields(
                            state, crop_result["extracted"], sub_schema,
                            source_page=page_num, source_region=f"ocr_crop_{region}",
                        ))
                        any_success = True

            # ── Step 3: Fallback — full-page vision for un-localised fields ────
            last_result: dict = {"success": False, "error": "no fields to extract"}
            if fallback:
                fallback_schema = {k: schema[k] for k in fallback}
                last_result = extract_fields_vision(
                    state,
                    image_path=img_path,
                    schema=fallback_schema,
                    hints=hints,
                    ollama_url=ctx.ollama_url,
                    model=ctx.vision_model,
                    text_context=ctx.cap_ocr_prompt_text(ocr.full_text or ""),
                    provider=ctx.provider,
                    timeout_s=ctx.timeouts["generate_timeout_s"],
                )
                if last_result["success"]:
                    _accumulate(merge_extracted_fields(
                        state, last_result["extracted"], fallback_schema,
                        source_page=page_num, source_region=kwargs.get("region", "unknown"),
                    ))
                    any_success = True
            elif not any_success:
                last_result = {"success": True, "extracted": {}}

            combined_merge["updated"] = sorted(set(combined_merge["updated"]))
            result = {
                "success": any_success,
                "merge_result": combined_merge,
                "ocr_direct": list(ocr_direct.keys()),
                "ocr_crop_regions": list({schema[f].get("region") for f in ocr_crop}),
                "fallback_fields": list(fallback.keys()),
            }
            if not any_success and "error" in last_result:
                result["error"] = last_result["error"]
            return result

        hybrid = bool(ctx.agent_cfg.get("hybrid_extraction", True))
        medium_path = None
        if (
            hybrid
            and state.medium_page_paths
            and len(state.medium_page_paths) == len(state.page_image_paths)
            and 1 <= page_num <= len(state.medium_page_paths)
        ):
            mp = state.medium_page_paths[page_num - 1]
            if Path(mp).exists():
                medium_path = mp

        if medium_path and medium_path != full_path:
            out = _run_extract_on_image(medium_path)
            if _needs_full_res_after_medium(out):
                logger.info("  extract_fields_vision: hybrid promoting to full-res page image")
                out2 = _run_extract_on_image(full_path)
                out2["hybrid_promoted_full_res"] = True
                return out2
            out["hybrid_used_medium_only"] = True
            return out

        return _run_extract_on_image(full_path)
    return _extract


def make_check(ctx: ToolContext):
    """Return the check_compliance tool callable.

    Includes a redundancy guard: if the result is identical to the previous
    call and occurred within 3 turns, warns the agent not to repeat the call.
    """
    def _check(state: AgentState, **kwargs):
        import hashlib
        rules = ctx.store.get_rules(state.invoice_type_id, ctx.active_rule_groups)
        result = check_compliance(state, rules, store=ctx.store)

        result_hash = hashlib.md5(
            json.dumps(result, sort_keys=True, default=str).encode()
        ).hexdigest()
        if state.last_compliance_hash and result_hash == state.last_compliance_hash:
            state.compliance_same_result_streak += 1
        else:
            state.compliance_same_result_streak = 0
        if (
            result_hash == state.last_compliance_hash
            and state.last_compliance_turn >= 0
            and (state.turn - state.last_compliance_turn) <= 3
        ):
            note = (
                f"check_compliance returned identical results to turn "
                f"{state.last_compliance_turn} — nothing changed since then. "
                f"Do NOT call check_compliance again until new fields are extracted. "
                f"If no error-level rules are failing, call check_compliance_visual next."
            )
            state.session_notes.append(note)
            result["redundant_call_warning"] = note
        if state.compliance_same_result_streak >= 2:
            result["compliance_loop_guard"] = (
                "check_compliance produced the same outcome repeatedly. "
                "Do not call check_compliance again until new fields are extracted or flagged. "
                "Use extract_fields_vision / crop_region / flag_for_human_review, or call finish()."
            )

        state.last_compliance_hash = result_hash
        state.last_compliance_turn = state.turn
        return result
    return _check


def make_flag(ctx: ToolContext):
    """Return the flag_for_human_review tool callable.

    Accepts both singular (field_name) and plural (fields / fields_to_flag)
    param variants that the LLM tends to use interchangeably.
    """
    def _flag(state: AgentState, **kwargs):
        parsed = FlagParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        field_names = (
            kwargs.get("fields_to_flag")
            or kwargs.get("fields")
            or ([kwargs["field_name"]] if "field_name" in kwargs else [])
        )
        if isinstance(field_names, str):
            field_names = [field_names]
        reason = kwargs.get("reason", "flagged by agent")
        results = []
        for fn in field_names:
            results.append(flag_for_human_review(state, fn, reason))
        return {"flagged": field_names, "results": results}
    return _flag


def make_read_learnings(ctx: ToolContext):
    """Return the read_learnings tool callable. Loads and caches learnings into state."""
    def _read_learnings(state: AgentState, **kwargs):
        section = read_learnings(state.invoice_type_id, ctx.learnings_path)
        state.learnings_context = ctx.cap_learnings(section)
        return {"learnings": section or "No learnings yet."}
    return _read_learnings


def make_write_learning(ctx: ToolContext):
    """Return the write_learning tool callable. Appends a new learning entry."""
    def _write_learning(state: AgentState, **kwargs):
        type_id = kwargs.get("invoice_type_id") or state.invoice_type_id or "GENERAL"
        return write_learning(
            type_id,
            category=kwargs["category"],
            content=kwargs["content"],
            learnings_path=ctx.learnings_path,
        )
    return _write_learning


def make_edit_learning(ctx: ToolContext):
    """Return the edit_learning tool callable. Edits an existing learning by ID."""
    def _edit_learning(state: AgentState, **kwargs):
        lid = kwargs.get("learning_id") or kwargs.get("id") or ""
        if not lid:
            return {"success": False, "error": "learning_id is required"}
        return edit_learning(
            learning_id=lid,
            new_content=kwargs.get("new_content") or kwargs.get("content", ""),
            learnings_path=ctx.learnings_path,
        )
    return _edit_learning


def make_delete_learning(ctx: ToolContext):
    """Return the delete_learning tool callable. Removes a learning entry by ID."""
    def _delete_learning(state: AgentState, **kwargs):
        lid = kwargs.get("learning_id") or kwargs.get("id") or ""
        if not lid:
            return {"success": False, "error": "learning_id is required"}
        return delete_learning(learning_id=lid, learnings_path=ctx.learnings_path)
    return _delete_learning


def make_inventory(ctx: ToolContext):
    """Return the inventory_pages tool callable. Classifies each page's role (header, totals, etc.)."""
    def _inventory(state: AgentState, **kwargs):
        return inventory_pages(
            state,
            ollama_url=ctx.ollama_url,
            model=ctx.vision_model,
            provider=ctx.provider,
            timeout_s=ctx.timeouts["generate_timeout_s"],
        )
    return _inventory


def make_check_visual(ctx: ToolContext):
    """Return the check_compliance_visual tool callable.

    Handles page-num remapping: if the agent requests page 1 but the
    inventory shows stamps on a different page, the tool silently redirects
    to the SIGNATURE_STAMP page.
    """
    def _check_visual(state: AgentState, **kwargs):
        parsed = CheckVisualParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        image_path, page_num, res_err = _resolve_image_path(
            kwargs, state, label="check_compliance_visual", require_page_num=True,
        )
        if res_err:
            return {"success": False, "error": res_err}
        if not image_path:
            return {"success": False, "error": "No rendered pages — call convert_pdf_to_images first."}
        # Remap to SIGNATURE_STAMP page when the agent targets the wrong page.
        if state.page_inventory and state.page_image_paths:
            inv_by_page = {int(e["page"]): e.get("category", "") for e in state.page_inventory}
            sig_pages = sorted(
                int(e["page"]) for e in state.page_inventory
                if e.get("category") == "SIGNATURE_STAMP"
            )
            cur_cat = (inv_by_page.get(page_num) or "").upper()
            non_stamp_categories = {
                "LINE_ITEMS", "TOTALS", "BODY", "BLANK", "COVER_PAGE",
                "SUPPORTING_DOC", "UNKNOWN", "",
            }
            if sig_pages and cur_cat in non_stamp_categories:
                page_num = sig_pages[0]
                idx = max(0, min(page_num - 1, len(state.page_image_paths) - 1))
                image_path = state.page_image_paths[idx]
                logger.info(
                    f"  check_compliance_visual: remapped to page_num={page_num} "
                    f"(SIGNATURE_STAMP) for stamp/signature visual rules"
                )
        if state.compressed and state.page_image_paths and state.page_image_paths == state.compressed_page_paths:
            _dpi = ctx.default_convert_dpi(state, {})
            return {
                "success": False,
                "error": (
                    "Images are still 48 DPI compressed thumbnails (from compress_pages). "
                    f"Call convert_pdf_to_images(dpi={_dpi}) first, then retry check_compliance_visual. "
                    "Visual stamp/signature checks require full-quality images."
                ),
            }
        rules = ctx.store.get_rules(state.invoice_type_id, ctx.active_rule_groups)
        return check_compliance_visual(
            state,
            image_path,
            page_num,
            rules,
            ctx.ollama_url,
            ctx.vision_model,
            max_evidence_pages=ctx.visual_max_evidence_pages,
            provider=ctx.provider,
            timeout_s=ctx.timeouts["generate_timeout_s"],
            hybrid_visual=bool(ctx.agent_cfg.get("hybrid_extraction", True)),
            store=ctx.store,
        )
    return _check_visual


def make_install_package(ctx: ToolContext):
    """Return the install_package tool callable. Pip-installs a recovery package at runtime."""
    def _install_package(state: AgentState, **kwargs):
        return install_package(state, package=kwargs["package"])
    return _install_package


def make_note(ctx: ToolContext):
    """Return the note tool callable. Appends a free-text note to state.session_notes."""
    def _note(state: AgentState, **kwargs):
        text = kwargs.get("text", "").strip()
        if text:
            state.session_notes.append(text)
        return {"noted": text}
    return _note


def make_finish(ctx: ToolContext):
    """Return the finish tool callable.

    Guards against premature termination:
    - Blocks if visual checks are still pending.
    - Blocks if no fields were extracted at all.
    Sets ``state.status`` to PASSED / NEEDS_REVIEW / FAILED based on
    compliance rule results and human-review flags.
    """
    def _finish(state: AgentState, **kwargs):
        parsed = FinishParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        if state.visual_checks_pending:
            return {
                "finished": False,
                "error": (
                    f"Cannot finish while visual checks are still pending — "
                    f"{len(state.visual_checks_pending)} visual check(s) still pending: "
                    f"{state.visual_checks_pending}. "
                    f"Call check_compliance_visual(page_num=N) for the SIGNATURE_STAMP "
                    f"or INVOICE_HEADER page first, then retry finish."
                ),
            }
        if state.invoice_type_id and not state.rule_results:
            has_extracted = any(
                f.extracted_value is not None for f in state.extracted_fields.values()
            )
            if not has_extracted:
                return {
                    "finished": False,
                    "error": (
                        "Cannot finish with 0 fields extracted. "
                        "Call convert_pdf_to_images (if not done) then "
                        "extract_fields_vision before finishing."
                    ),
                }

        state.finish_reason = kwargs.get("reason", "done")
        has_flags = any(f.flagged_for_review for f in state.extracted_fields.values())

        error_failed = any(
            (rr.status == "failed" and rr.severity == "error") for rr in state.rule_results
        )
        error_skipped = any(
            (rr.status == "skipped" and rr.severity == "error") for rr in state.rule_results
        )
        unresolved_error_evidence = False
        for rr in state.rule_results:
            if rr.severity != "error":
                continue
            if rr.status == "passed":
                continue
            ev = state.rule_evidence.get(rr.rule_id, {})
            missing = ev.get("missing_slots", [])
            rs = state.rule_state.get(rr.rule_id, "")
            if missing or rs not in ("finalized_pass", "finalized_fail"):
                unresolved_error_evidence = True
                break

        all_errors_resolved = not error_failed and not error_skipped and not unresolved_error_evidence

        if all_errors_resolved:
            state.status = AgentStatus.NEEDS_REVIEW if has_flags else AgentStatus.PASSED
        else:
            state.status = AgentStatus.NEEDS_REVIEW if has_flags else AgentStatus.FAILED

        rv = rule_verdict_summary(state.rule_results)
        error_failures = rv["error_failed_rule_ids"]
        warning_failures = rv["warning_failed_rule_ids"]
        if not all_errors_resolved:
            status_explanation = (
                "Run status reflects blocking error-severity rule failures or incomplete error evidence."
            )
        elif warning_failures:
            status_explanation = (
                "No blocking error-severity failures; warning-level rules may still be non-pass "
                "(listed in warning_failures)."
            )
        elif state.status == AgentStatus.NEEDS_REVIEW:
            status_explanation = "No rule blockers; one or more fields are flagged for human review."
        else:
            status_explanation = "All evaluated rules passed at error severity; no flags."

        return {
            "finished": True,
            "status": state.status.value,
            "error_failures": error_failures,
            "warning_failures": warning_failures,
            "status_explanation": status_explanation,
            "evidence_gate": {
                "unresolved_error_evidence": unresolved_error_evidence,
                "error_failed": error_failed,
                "error_skipped": error_skipped,
            },
        }
    return _finish
