"""Facade re-export for tool implementations."""

from src.tools.pdf_pages import (
    inspect_file,
    compress_pages,
    convert_pdf_to_images,
    render_medium_pages,
    REGION_CROPS,
    crop_region,
    _image_to_base64,
)
from src.tools.ocr_layout import SuryaModels, load_surya_models, OcrLine, OcrResult, FieldLocalization, _ocr_with_layout, _localize_field_in_ocr, _union_bboxes, _save_image_crop
from src.tools.vision_llm import classify_document_type, extract_fields_vision, merge_extracted_fields
from src.tools.compliance_eval import _extract_entities_from_text, _policy_refs_for_rule, _normalize_numeric, _safe_eval_numeric, _get_field_value, _evaluate_rule, _field_name_for_id, check_compliance
from src.tools.compliance_visual import check_compliance_visual
from src.tools.page_inventory import inventory_pages
from src.tools.learnings_store import _LEARNING_ID_RE, _next_learning_id, _extract_section, read_learnings, write_learning, edit_learning, delete_learning, reset_learnings
from src.tools.misc_tools import flag_for_human_review, install_package

__all__ = [k for k in globals().keys() if not k.startswith('__')]
