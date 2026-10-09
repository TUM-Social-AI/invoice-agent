from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
    allowed_values: list[str] = Field(default_factory=list)


# Page-inventory categories a visual rule can ask for as evidence ("ALL" = every page).
EVIDENCE_CATEGORIES = (
    "INVOICE_HEADER",
    "LINE_ITEMS",
    "TOTALS",
    "SIGNATURE_STAMP",
    "SUPPORTING_DOC",
    "COVER_PAGE",
    "ALL",
)


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
    # Optional page categories the visual check needs as evidence; empty = keyword heuristic.
    evidence_categories: list[str] = Field(default_factory=list)

    @field_validator("evidence_categories", mode="before")
    @classmethod
    def parse_evidence_categories(cls, value: Any) -> list[str]:
        if value is None:
            return []
        parts = value.split(";") if isinstance(value, str) else list(value)
        cats: list[str] = []
        for part in parts:
            cat = str(part).strip().upper()
            if not cat:
                continue
            if cat not in EVIDENCE_CATEGORIES:
                raise ValueError(f"unknown evidence category {cat!r}; expected one of {EVIDENCE_CATEGORIES}")
            if cat not in cats:
                cats.append(cat)
        return cats

    @field_validator("page_region")
    @classmethod
    def normalize_page_region(cls, value: str) -> str:
        return (value or "").strip() or "body"

