from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ClassificationResultModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    invoice_type_id: str
    confidence: float = 0.0
    reasoning: str = ""


class InventoryItemModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    category: Literal[
        "INVOICE_HEADER",
        "LINE_ITEMS",
        "TOTALS",
        "SIGNATURE_STAMP",
        "SUPPORTING_DOC",
        "COVER_PAGE",
        "BLANK",
        "UNKNOWN",
    ] = "UNKNOWN"
    description: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def normalize_category(cls, value: Any) -> str:
        return str(value or "UNKNOWN").strip().upper()


class VisualVerdictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    passes: bool = False
    confidence: float = 0.5
    observation: str = "No observation"
    # Optional: extraction field names → values the vision model is confident about for this rule.
    field_updates: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _coerce_field_updates(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = data.get("field_updates")
        if raw is None or raw is False:
            data = dict(data)
            data["field_updates"] = {}
            return data
        if not isinstance(raw, dict):
            data = dict(data)
            data["field_updates"] = {}
            return data
        cleaned: dict[str, str] = {}
        for k, v in raw.items():
            ks = str(k).strip()
            if not ks or v is None:
                continue
            cleaned[ks] = str(v).strip()
        data = dict(data)
        data["field_updates"] = cleaned
        return data


class ExtractionPayloadModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def validate_confidence_ranges(cls, payload: dict[str, Any]) -> dict[str, Any]:
        for key, value in payload.items():
            if not key.endswith("_confidence"):
                continue
            try:
                c = float(value)
            except (TypeError, ValueError):
                continue
            payload[key] = min(1.0, max(0.0, c))
        return payload

