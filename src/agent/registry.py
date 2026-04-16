"""Tool registry builder."""
## TODO: for all files, explain at the top more what they are for
## TODO: are you sure all of those functions fit the registry file?

import json
import logging
import re
import requests
from pathlib import Path
from typing import Any

from src.agent.state import AgentState, AgentStatus, rule_verdict_summary
from src.config.loader import ConfigStore
from src.llm.base import LLMProvider
from src.llm.config_resolve import (
    active_rule_groups_from_config,
    ollama_base_url,
    prompt_limits_for_config,
    vision_model_for_config,
)
from src.agent.loop_utils import timeout_cfg as _timeout_cfg
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
    flag_for_human_review,
    read_learnings,
    write_learning,
    edit_learning,
    delete_learning,
    install_package,
    inventory_pages,
    check_compliance_visual,
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


def _resolve_ollama_url(provider: "LLMProvider | None", config: dict) -> str:
    """Return the Ollama base URL, preferring the provider over config.

    When ``provider`` is an ``OllamaProvider`` the URL was already resolved
    at startup and stored on the object — using it here avoids a second
    config read and makes the registry independent of Ollama-specific config
    keys.  For non-Ollama providers (e.g. Gemini) ``base_url`` is absent, so
    we fall back to reading the config as before.
    """
    if provider is not None and getattr(provider, "base_url", None):
        return provider.base_url  # type: ignore[attr-defined]
    return ollama_base_url(config)


def _auto_expand_fields(
    kwargs: dict,
    full_schema: dict,
    state: "AgentState",
    agent_cfg: dict,
) -> dict:
    """Return the effective extraction schema for one vision call.

    Starts from whatever field subset the agent requested, then fills
    remaining slots with every field that has never been attempted — so
    un-tried fields get a free ride in the same vision call (cuts turns
    by ~60-70%).  The caller gates this function with ``batch_auto_expand``
    from config so it can be disabled when exact field control is needed.

    Args:
        kwargs:     Raw tool-call keyword arguments from the agent.
        full_schema: All fields defined for the current invoice type.
        state:      Current agent state (used to check retry counts).
        agent_cfg:  The ``agent`` section of config (for ``micro_tools_phase2``
                    and the MAX_BATCH_FIELDS cap).

    Returns:
        A filtered schema dict (subset of ``full_schema``).
    """
    MAX_BATCH_FIELDS = 8 if agent_cfg.get("micro_tools_phase2") else 15
    _raw_subset = resolve_param(kwargs, "field_subset") or []
    # Guard: if the LLM passed a comma-separated string instead of a list, split it.
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


def build_tool_registry(
    config: dict,
    store: ConfigStore,
    surya_models: "Optional[SuryaModels]" = None,
    provider: "LLMProvider | None" = None,
):
    ollama_url = _resolve_ollama_url(provider, config)
    vision_model = vision_model_for_config(config)
    learnings_path = config.get("learnings_path", "learnings/learnings.md")
    agent_cfg = config.get("agent", {})
    _plim = prompt_limits_for_config(config)
    learnings_max_chars = _plim["learnings_max_chars"]
    visual_max_evidence_pages = int(agent_cfg.get("visual_max_evidence_pages", 6))
    ocr_prompt_max_chars = int(agent_cfg.get("ocr_prompt_max_chars", 24000) or 0)
    ocr_langs: list[str] = config.get("ocr", {}).get("langs", ["es", "en"])
    timeouts = _timeout_cfg(config)
    _active_rule_groups = active_rule_groups_from_config(config)

    def _default_convert_dpi(state: AgentState, kwargs: dict) -> int:
        if kwargs.get("dpi") is not None:
            return int(kwargs["dpi"])
        return int(getattr(state, "page_render_dpi", None) or agent_cfg.get("page_dpi", 150))

    def _cap_ocr_prompt_text(text: str) -> str:
        if ocr_prompt_max_chars <= 0 or len(text) <= ocr_prompt_max_chars:
            return text
        head = ocr_prompt_max_chars // 2
        tail_budget = ocr_prompt_max_chars - head - 72
        if tail_budget < 1:
            return text[:ocr_prompt_max_chars]
        sep = "\n...[OCR text truncated for prompt size]...\n"
        return text[:head] + sep + text[-tail_budget:]

    def _cap_learnings(text: str) -> str:
        if not text:
            return ""
        if learnings_max_chars <= 0:
            return text
        return text if len(text) <= learnings_max_chars else text[:learnings_max_chars]

    def _inspect(state, **kwargs):
        return inspect_file(state)

    def _compress(state, **kwargs):
        return compress_pages(
            state,
            dpi=kwargs.get("dpi", 96),
            quality=kwargs.get("quality", 65),
            max_width=kwargs.get("max_width", 1400),
        )

    def _classify(state, **kwargs):
        old_type = state.invoice_type_id
        result = classify_document_type(
            state,
            store,
            ollama_url,
            vision_model,
            provider=provider,
            timeout_s=timeouts["generate_timeout_s"],
        )
        if result.get("success") and state.invoice_type_id != old_type:
            # Type changed mid-run — load learnings for the new type immediately
            new_section = read_learnings(state.invoice_type_id, learnings_path)
            state.learnings_context = _cap_learnings(new_section)
            logger.info(f"  learnings refreshed for reclassified type: {state.invoice_type_id}")
        return result

    def _convert_pdf(state, **kwargs):
        res = convert_pdf_to_images(state, dpi=_default_convert_dpi(state, kwargs))
        if (
            res.get("success")
            and bool(agent_cfg.get("hybrid_extraction", True))
            and state.page_image_paths
        ):
            m = render_medium_pages(state)
            if not m.get("success"):
                logger.warning("  render_medium_pages failed (non-fatal): %s", m.get("error"))
        return res

    def _coerce_page_num(raw) -> int:
        if raw is None:
            return 1
        if isinstance(raw, list):
            raw = raw[0] if raw else 1
        first = str(raw).split(",")[0].strip()
        digits = re.sub(r"[^\d]", "", first)
        return int(digits) if digits else 1

    def _resolve_image_path(
        kwargs: dict,
        state,
        *,
        label: str = "",
        require_page_num: bool = False,
    ) -> "tuple[str|None, int, str|None]":
        """
        Resolve (image_path, page_num, error).

        When require_page_num is True (extract_fields_vision, check_compliance_visual),
        only state.page_image_paths[page_num] is used — LLM image_path is ignored.
        """
        _IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
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

        # crop_region: allow explicit path if it exists and looks valid.
        # Named aliases are resolved via param_resolver; extension-based
        # fallback handles ad-hoc path keys the LLM may invent.
        image_path = resolve_param(kwargs, "image_path") or next(
            (v for v in kwargs.values()
             if isinstance(v, str) and v.lower().endswith(_IMG_EXTS)),
            None,
        ) or next(
            (v[0] for v in kwargs.values()
             if isinstance(v, list) and v
             and isinstance(v[0], str) and v[0].lower().endswith(_IMG_EXTS)),
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

    def _crop(state, **kwargs):
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

    def _extract(state, **kwargs):
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
        # Warn if we're about to run extraction on compressed thumbnails.
        # convert_pdf_to_images must be called first for accurate vision extraction.
        if state.compressed and state.page_image_paths and state.page_image_paths == state.compressed_page_paths:
            _dpi = _default_convert_dpi(state, {})
            return {
                "success": False,
                "error": (
                    "Images are still 48 DPI compressed thumbnails (from compress_pages). "
                    f"Call convert_pdf_to_images(dpi={_dpi}) first, then retry extract_fields_vision. "
                    "Compressed thumbnails are only suitable for inventory_pages and classify_document_type."
                ),
            }
        invoice_type_id = state.invoice_type_id
        full_schema = store.build_extraction_schema(invoice_type_id)


        # ── Batch auto-expansion ──────────────────────────────────────────────
        # Adds un-attempted fields to the current vision call so they get a
        # free ride without an extra LLM request (cuts turns by ~60-70%).
        # Controlled by agent.batch_auto_expand in config; see _auto_expand_fields.
        if agent_cfg.get("batch_auto_expand", True):
            schema = _auto_expand_fields(kwargs, full_schema, state, agent_cfg)
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
            n = nf if isinstance(nf, int) else len(nf)
            return n > 0

        def _run_extract_on_image(image_path: str) -> dict:
            # ── OCR-guided extraction ─────────────────────────────────────────
            OCR_DIRECT_THRESHOLD = 0.80

            ocr = _ocr_with_layout(image_path, surya_models=surya_models)
            if not ocr.is_empty():
                logger.debug(f"  OCR layout: {len(ocr.lines)} lines, {len(ocr.full_text)} chars")

            ocr_direct: dict = {}          # field_name → (value_text, confidence)
            ocr_crop: dict = {}            # field_name → FieldLocalization
            fallback: dict = {}            # field_name → field_meta

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

            # ── Step 1: OCR-direct fields (no vision needed) ──────────────────────
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

            # ── Step 2: OCR-crop fields (one vision call per schema-region group) ──
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
                        image_path, crop_bbox, state.output_dir,
                        f"p{page_num}_{region}",
                    )
                    sub_schema = {fname: schema[fname] for fname, _ in fields_in_region}
                    cx1, cy1, cx2, cy2 = crop_bbox
                    crop_ocr_lines = [
                        l for l in ocr.lines
                        if l.bbox[0] < cx2 and l.bbox[2] > cx1
                        and l.bbox[1] < cy2 and l.bbox[3] > cy1
                    ]
                    crop_text = "\n".join(l.text for l in crop_ocr_lines if l.text.strip())
                    crop_text_capped = _cap_ocr_prompt_text(crop_text)

                    crop_result = extract_fields_vision(
                        state,
                        image_path=crop_path,
                        schema=sub_schema,
                        hints=hints,
                        ollama_url=ollama_url,
                        model=vision_model,
                        text_context=crop_text_capped,
                        provider=provider,
                        timeout_s=timeouts["generate_timeout_s"],
                    )
                    if crop_result["success"]:
                        _accumulate(merge_extracted_fields(
                            state, crop_result["extracted"], sub_schema,
                            source_page=page_num, source_region=f"ocr_crop_{region}",
                        ))
                        any_success = True

            # ── Step 3: Fallback — full-page vision for un-localised fields ────────
            last_result: dict = {"success": False, "error": "no fields to extract"}
            if fallback:
                fallback_schema = {k: schema[k] for k in fallback}
                ocr_text_capped = _cap_ocr_prompt_text(ocr.full_text or "")
                last_result = extract_fields_vision(
                    state,
                    image_path=image_path,
                    schema=fallback_schema,
                    hints=hints,
                    ollama_url=ollama_url,
                    model=vision_model,
                    text_context=ocr_text_capped,
                    provider=provider,
                    timeout_s=timeouts["generate_timeout_s"],
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

        hybrid = bool(agent_cfg.get("hybrid_extraction", True))
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

    def _check(state, **kwargs):
        import hashlib
        rules = store.get_rules(state.invoice_type_id, _active_rule_groups)
        result = check_compliance(state, rules, store=store)

        # Detect redundant calls: if result is identical to the last call AND
        # it happened recently (≤3 turns ago), warn the agent to stop repeating.
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

    def _flag(state, **kwargs):
        parsed = FlagParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        # Accept singular or plural field name, with or without a reason
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

    def _read_learnings(state, **kwargs):
        section = read_learnings(state.invoice_type_id, learnings_path)
        state.learnings_context = _cap_learnings(section)
        return {"learnings": section or "No learnings yet."}

    def _write_learning(state, **kwargs):
        type_id = kwargs.get("invoice_type_id") or state.invoice_type_id or "GENERAL"
        return write_learning(
            type_id,
            category=kwargs["category"],
            content=kwargs["content"],
            learnings_path=learnings_path,
        )

    def _edit_learning(state, **kwargs):
        lid = kwargs.get("learning_id") or kwargs.get("id") or ""
        if not lid:
            return {"success": False, "error": "learning_id is required"}
        return edit_learning(
            learning_id=lid,
            new_content=kwargs.get("new_content") or kwargs.get("content", ""),
            learnings_path=learnings_path,
        )

    def _delete_learning(state, **kwargs):
        lid = kwargs.get("learning_id") or kwargs.get("id") or ""
        if not lid:
            return {"success": False, "error": "learning_id is required"}
        return delete_learning(learning_id=lid, learnings_path=learnings_path)

    def _inventory(state, **kwargs):
        return inventory_pages(
            state,
            ollama_url=ollama_url,
            model=vision_model,
            provider=provider,
            timeout_s=timeouts["generate_timeout_s"],
        )

    def _check_visual(state, **kwargs):
        parsed = CheckVisualParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        image_path, page_num, res_err = _resolve_image_path(
            kwargs, state, label="check_compliance_visual", require_page_num=True,
        )
        if res_err:
            return {"success": False, "error": res_err}
        if not image_path:
            return {"success": False, "error": "No rendered pages — call convert_pdf_to_images first."}
        # Stamp/signature rules need the SIGNATURE_STAMP page. Models often pass page_num=1;
        # remap when inventory shows stamps on another page.
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
        # Warn if we're about to run visual compliance on compressed thumbnails.
        if state.compressed and state.page_image_paths and state.page_image_paths == state.compressed_page_paths:
            _dpi = _default_convert_dpi(state, {})
            return {
                "success": False,
                "error": (
                    "Images are still 48 DPI compressed thumbnails (from compress_pages). "
                    f"Call convert_pdf_to_images(dpi={_dpi}) first, then retry check_compliance_visual. "
                    "Visual stamp/signature checks require full-quality images."
                ),
            }
        rules = store.get_rules(state.invoice_type_id, _active_rule_groups)
        return check_compliance_visual(
            state,
            image_path,
            page_num,
            rules,
            ollama_url,
            vision_model,
            max_evidence_pages=visual_max_evidence_pages,
            provider=provider,
            timeout_s=timeouts["generate_timeout_s"],
            hybrid_visual=bool(agent_cfg.get("hybrid_extraction", True)),
            store=store,
        )

    def _install_package(state, **kwargs):
        return install_package(state, package=kwargs["package"])

    def _note(state, **kwargs):
        text = kwargs.get("text", "").strip()
        if text:
            state.session_notes.append(text)
        return {"noted": text}

    def _finish(state, **kwargs):
        parsed = FinishParams.model_validate(kwargs)
        kwargs = parsed.model_dump(exclude_none=True)
        # Guard 1 (highest priority): visual checks must be evaluated before finish.
        # Checked first so it always produces the correct "visual checks" error
        # regardless of how many fields were extracted.
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

        # Guard 2: refuse early finish when no work has been done at all.
        # Only applies when rule_results is also empty — if compliance has already
        # run, extraction happened and this guard would be a false positive.
        if state.invoice_type_id and not state.rule_results:
            has_extracted = any(
                f.extracted_value is not None
                for f in state.extracted_fields.values()
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
            (rr.status == "failed" and rr.severity == "error")
            for rr in state.rule_results
        )
        error_skipped = any(
            (rr.status == "skipped" and rr.severity == "error")
            for rr in state.rule_results
        )
        # Evidence slots can be incomplete even when the rule already has a definitive
        # PASSED verdict (e.g. visual rules whose required_slots heuristics expect
        # cross-page linkage slots that are not filled). A pass is still final for finish.
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

    return {
        "inspect_file": _inspect,
        "compress_pages": _compress,
        "classify_document_type": _classify,
        "convert_pdf_to_images": _convert_pdf,
        "crop_region": _crop,
        "extract_fields_vision": _extract,
        "check_compliance": _check,
        "flag_for_human_review": _flag,
        "flag_fields_for_review": _flag,   # common alias the model tends to invent
        "inventory_pages": _inventory,
        "check_compliance_visual": _check_visual,
        "install_package": _install_package,
        "note": _note,
        "read_learnings": _read_learnings,
        "write_learning": _write_learning,
        "edit_learning": _edit_learning,
        "delete_learning": _delete_learning,
        "finish": _finish,
    }


## TODO: this file seems to define all the tools, that shouldn't be it's purpose, also for all functions that are not self explanatory comments should be added regarding what the function is for and how it is used