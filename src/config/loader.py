"""
Config loader — reads all CSV configuration files and exposes
typed objects to the rest of the system. No hardcoded rules anywhere else.
"""

import csv
import logging
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.models.config_models import (
    ComplianceRuleModel,
    ExtractionFieldModel,
    InvoiceTypeModel,
    VisualObservationFallbackModel,
)

logger = logging.getLogger(__name__)


class InvoiceType(InvoiceTypeModel):
    pass


class ExtractionField(ExtractionFieldModel):
    pass


class ComplianceRule(ComplianceRuleModel):
    pass


class VisualObservationFallback(VisualObservationFallbackModel):
    pass


def _load_employee_name_role_denylist(base: Path) -> list[str]:
    path = base / "employee_name_role_denylist.txt"
    if not path.exists():
        logger.warning("Missing %s — employee_name role filtering uses no phrases", path.name)
        return []
    phrases: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        phrases.append(line.lower())
    return phrases


def filter_compliance_rules_by_groups(
    rules: list[ComplianceRule],
    active_groups: list[str] | None,
) -> list[ComplianceRule]:
    """If active_groups is None, return all rules (backward compatible)."""
    if active_groups is None:
        return list(rules)
    gset = {g.strip().lower() for g in active_groups if g and str(g).strip()}
    if not gset:
        return list(rules)
    out: list[ComplianceRule] = []
    for r in rules:
        rg = (getattr(r, "rule_group", None) or "general").strip().lower() or "general"
        if rg in gset:
            out.append(r)
    return out


class ConfigStore(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    invoice_types: dict[str, InvoiceType] = Field(default_factory=dict)
    extraction_fields: dict[str, list[ExtractionField]] = Field(default_factory=dict)   # keyed by invoice_type_id
    compliance_rules: dict[str, list[ComplianceRule]] = Field(default_factory=dict)     # keyed by invoice_type_id
    schema_cache: dict[str, dict] = Field(default_factory=dict, repr=False)            # cache for build_extraction_schema
    employee_name_role_denylist: list[str] = Field(default_factory=list, repr=False)
    visual_observation_fallbacks: list[VisualObservationFallback] = Field(default_factory=list, repr=False)

    def get_type(self, invoice_type_id: str) -> Optional[InvoiceType]:
        return self.invoice_types.get(invoice_type_id)

    def get_fields(self, invoice_type_id: str) -> list[ExtractionField]:
        return self.extraction_fields.get(invoice_type_id, [])

    def get_rules(
        self,
        invoice_type_id: str,
        active_rule_groups: list[str] | None = None,
    ) -> list[ComplianceRule]:
        rules = list(self.compliance_rules.get(invoice_type_id, []))
        return filter_compliance_rules_by_groups(rules, active_rule_groups)

    def get_field_by_id(self, field_id: str) -> Optional[ExtractionField]:
        for fields in self.extraction_fields.values():
            for f in fields:
                if f.field_id == field_id:
                    return f
        return None

    def build_extraction_schema(self, invoice_type_id: str) -> dict:
        """
        Builds the JSON schema dict passed to the vision model for structured extraction.
        Each field becomes a key with type + description for the model prompt.
        Result is cached — the schema is pure and deterministic for a given type.
        """
        if invoice_type_id in self.schema_cache:
            return self.schema_cache[invoice_type_id]
        fields = self.get_fields(invoice_type_id)
        schema = {}
        for f in fields:
            schema[f.field_name] = {
                "field_id": f.field_id,
                "type": f.data_type,
                "label": f.field_label,
                "required": f.required,
                "hint": f.extraction_hint,
                "region": f.page_region,
                "aliases": f.aliases,
            }
        self.schema_cache[invoice_type_id] = schema
        return schema

    def build_agent_context(
        self,
        invoice_type_id: str,
        active_rule_groups: list[str] | None = None,
    ) -> str:
        """
        Builds the full context string injected into the agent's system prompt.
        Combines invoice type context + field hints + rule hints.
        """
        inv_type = self.get_type(invoice_type_id)
        if not inv_type:
            return ""

        lines = [
            f"## Invoice Type: {inv_type.display_name}",
            f"{inv_type.description}",
            f"\n### Document Context\n{inv_type.agent_context}",
            "\n### Fields to Extract",
        ]

        for f in self.get_fields(invoice_type_id):
            req = "REQUIRED" if f.required else "optional"
            lines.append(
                f"- **{f.field_name}** ({f.field_label}, {req}, region={f.page_region}): "
                f"{f.extraction_hint} | Aliases: {', '.join(f.aliases)}"
            )

        lines.append("\n### Compliance Rules to Satisfy")
        for r in self.get_rules(invoice_type_id, active_rule_groups):
            if r.enabled:
                lines.append(
                    f"- [{r.severity.upper()}] **{r.rule_id}** ({r.rule_name}): "
                    f"{r.agent_hint}"
                )

        return "\n".join(lines)

    def observation_fallbacks_for(self, invoice_type_id: str) -> list[VisualObservationFallback]:
        return [x for x in self.visual_observation_fallbacks if x.invoice_type_id == invoice_type_id and x.enabled]


def load_config(config_dir: str = "config/csv") -> ConfigStore:
    store = ConfigStore()
    base = Path(config_dir)

    # --- Invoice types ---
    types_path = base / "invoice_types.csv"
    with open(types_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["enabled"].lower() != "true":
                continue
            t = InvoiceType(
                invoice_type_id=row["invoice_type_id"].strip(),
                display_name=row["display_name"].strip(),
                description=row["description"].strip(),
                agent_context=row["agent_context"].strip(),
                enabled=True,
            )
            store.invoice_types[t.invoice_type_id] = t
    logger.info(f"Loaded {len(store.invoice_types)} invoice types")

    # --- Extraction fields ---
    fields_path = base / "extraction_fields.csv"
    with open(fields_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            type_id = row["invoice_type_id"].strip()
            if type_id not in store.invoice_types:
                continue
            aliases_raw = row.get("aliases", "")
            aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
            ef = ExtractionField(
                field_id=row["field_id"].strip(),
                invoice_type_id=type_id,
                field_name=row["field_name"].strip(),
                field_label=row["field_label"].strip(),
                data_type=row["data_type"].strip(),
                required=row["required"].lower() == "true",
                extraction_hint=row["extraction_hint"].strip(),
                page_region=row["page_region"].strip(),
                aliases=aliases,
            )
            store.extraction_fields.setdefault(type_id, []).append(ef)
    total_fields = sum(len(v) for v in store.extraction_fields.values())
    logger.info(f"Loaded {total_fields} extraction fields")

    # --- Compliance rules ---
    rules_path = base / "compliance_rules.csv"
    with open(rules_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            type_id = row["invoice_type_id"].strip()
            if type_id not in store.invoice_types:
                continue
            if row["enabled"].lower() != "true":
                continue
            rg_raw = (row.get("rule_group") or "general").strip().lower() or "general"
            cr = ComplianceRule(
                rule_id=row["rule_id"].strip(),
                invoice_type_id=type_id,
                rule_name=row["rule_name"].strip(),
                field_id=row["field_id"].strip(),
                check_type=row["check_type"].strip(),
                check_value=row["check_value"].strip(),
                severity=row["severity"].strip(),
                agent_hint=row["agent_hint"].strip(),
                error_message=row["error_message"].strip(),
                page_region=row["page_region"].strip(),
                enabled=True,
                rule_group=rg_raw,
            )
            store.compliance_rules.setdefault(type_id, []).append(cr)
    total_rules = sum(len(v) for v in store.compliance_rules.values())
    logger.info(f"Loaded {total_rules} compliance rules")

    store.employee_name_role_denylist = _load_employee_name_role_denylist(base)
    logger.info(f"Loaded {len(store.employee_name_role_denylist)} employee_name role denylist phrases")

    fb_path = base / "visual_observation_fallback.csv"
    if fb_path.exists():
        with open(fb_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("enabled") or "true").strip().lower() != "true":
                    continue
                tid = (row.get("invoice_type_id") or "").strip()
                if tid not in store.invoice_types:
                    continue
                src = (row.get("source_rule_id") or "").strip()
                tfn = (row.get("target_field_name") or "").strip()
                tfid = (row.get("target_field_id") or "").strip()
                pkind = (row.get("parser_kind") or "").strip()
                if not src or not tfn or not tfid or pkind not in ("employee_name_quote", "payment_phrase"):
                    logger.warning("Skipping invalid visual_observation_fallback row: %s", row)
                    continue
                try:
                    store.visual_observation_fallbacks.append(
                        VisualObservationFallback(
                            invoice_type_id=tid,
                            source_rule_id=src,
                            target_field_name=tfn,
                            target_field_id=tfid,
                            parser_kind=pkind,  # type: ignore[arg-type]
                            reevaluate_rule_id=(row.get("reevaluate_rule_id") or "").strip(),
                            enabled=True,
                        )
                    )
                except Exception as e:
                    logger.warning("Skipping visual_observation_fallback row: %s (%s)", row, e)
        logger.info(f"Loaded {len(store.visual_observation_fallbacks)} visual observation fallback rows")

    return store
