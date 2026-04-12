from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VisualObservationFallbackModel(BaseModel):
    """When a visual rule passes, optionally parse observation text to backfill an extraction field."""

    model_config = ConfigDict(extra="forbid")

    invoice_type_id: str
    source_rule_id: str
    target_field_name: str
    target_field_id: str
    parser_kind: Literal["employee_name_quote", "payment_phrase"]
    reevaluate_rule_id: str = ""
    enabled: bool = True


class InvoiceTypeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_type_id: str
    display_name: str
    description: str
    agent_context: str
    enabled: bool = True


class ExtractionFieldModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_id: str
    invoice_type_id: str
    field_name: str
    field_label: str
    data_type: Literal["string", "decimal", "date", "boolean"]
    required: bool = False
    extraction_hint: str
    page_region: Literal["header", "footer", "body", "totals", "address_block", "line_items"]
    aliases: list[str] = Field(default_factory=list)


class ComplianceRuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    invoice_type_id: str
    rule_name: str
    field_id: str
    check_type: Literal[
        "required",
        "regex",
        "range",
        "enum",
        "cross_field",
        "conditional_check",
        "required_one_of",
        "visual_check",
    ]
    check_value: str
    severity: Literal["error", "warning"]
    agent_hint: str
    error_message: str
    page_region: str
    enabled: bool = True
    # general = any project; xunta_galicia = Galicia grant stamp / 2023 / PR811A / caps (see config active_rule_groups)
    rule_group: str = "general"

    @field_validator("page_region")
    @classmethod
    def normalize_page_region(cls, value: str) -> str:
        return (value or "").strip() or "body"

