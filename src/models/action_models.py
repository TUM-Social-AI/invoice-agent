from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InspectParams(_StrictModel):
    pass


class CompressParams(_StrictModel):
    dpi: int = 96
    quality: int = 65
    max_width: int = 1400


class ClassifyParams(_StrictModel):
    pass


class ConvertParams(_StrictModel):
    dpi: int = 150


class CropRegionParams(_StrictModel):
    page_num: int | None = None
    image_path: str | None = None
    region: str
    custom_bbox: list[float] | None = None


class ExtractFieldsParams(_StrictModel):
    page_num: int
    region: str | None = None
    hints: str | None = None
    field_subset: list[str] = Field(default_factory=list)
    fields: list[str] = Field(default_factory=list)
    field_names: list[str] = Field(default_factory=list)

    @field_validator("page_num", mode="before")
    @classmethod
    def coerce_page_num(cls, value: Any) -> int:
        if isinstance(value, list):
            value = value[0] if value else 1
        if isinstance(value, str):
            digits = "".join(ch for ch in value if ch.isdigit())
            if digits:
                return int(digits)
        return int(value)

    @field_validator("field_subset", mode="before")
    @classmethod
    def coerce_field_subset(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [v.strip() for v in value.split(",") if v.strip()]
        return list(value)


class CheckComplianceParams(_StrictModel):
    pass


class CheckVisualParams(_StrictModel):
    page_num: int


class FlagParams(_StrictModel):
    field_name: str | None = None
    fields: list[str] = Field(default_factory=list)
    fields_to_flag: list[str] = Field(default_factory=list)
    reason: str | None = None


class InventoryParams(_StrictModel):
    pass


class InstallParams(_StrictModel):
    package: str


class NoteParams(_StrictModel):
    text: str


class ReadLearnParams(_StrictModel):
    pass


class WriteLearnParams(_StrictModel):
    invoice_type_id: str | None = None
    category: Literal[
        "approaches",
        "extraction_patterns",
        "common_failures",
        "compliance_edge_cases",
        "tool_suggestions",
    ]
    content: str


class EditLearnParams(_StrictModel):
    learning_id: str | None = None
    id: str | None = None
    new_content: str | None = None
    content: str | None = None


class DeleteLearnParams(_StrictModel):
    learning_id: str | None = None
    id: str | None = None


class FinishParams(_StrictModel):
    reason: str | None = None
    all_errors_resolved: bool | None = None


TOOL_PARAM_MODELS = {
    "inspect_file": InspectParams,
    "compress_pages": CompressParams,
    "classify_document_type": ClassifyParams,
    "convert_pdf_to_images": ConvertParams,
    "crop_region": CropRegionParams,
    "extract_fields_vision": ExtractFieldsParams,
    "check_compliance": CheckComplianceParams,
    "check_compliance_visual": CheckVisualParams,
    "flag_for_human_review": FlagParams,
    "flag_fields_for_review": FlagParams,
    "inventory_pages": InventoryParams,
    "install_package": InstallParams,
    "note": NoteParams,
    "read_learnings": ReadLearnParams,
    "write_learning": WriteLearnParams,
    "edit_learning": EditLearnParams,
    "delete_learning": DeleteLearnParams,
    "finish": FinishParams,
}

