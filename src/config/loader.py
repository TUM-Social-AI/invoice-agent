"""
Config loader — reads all CSV configuration files and exposes
typed objects to the rest of the system. No hardcoded rules anywhere else.
"""

import csv
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class InvoiceType:
    invoice_type_id: str
    display_name: str
    description: str
    agent_context: str
    enabled: bool


@dataclass
class ExtractionField:
    field_id: str
    invoice_type_id: str
    field_name: str
    field_label: str
    data_type: str          # string | decimal | date | boolean
    required: bool
    extraction_hint: str
    page_region: str        # header | footer | body | totals | address_block | line_items
    aliases: list[str]      # label variants the agent should look for


@dataclass
class ComplianceRule:
    rule_id: str
    invoice_type_id: str
    rule_name: str
    field_id: str
    check_type: str         # required | regex | range | enum | cross_field | conditional_check | required_one_of
    check_value: str        # depends on check_type
    severity: str           # error | warning
    agent_hint: str         # plain language guidance for the agent's reasoning
    error_message: str      # message written to output CSV on failure
    page_region: str
    enabled: bool


@dataclass
class ConfigStore:
    invoice_types: dict[str, InvoiceType] = field(default_factory=dict)
    extraction_fields: dict[str, list[ExtractionField]] = field(default_factory=dict)   # keyed by invoice_type_id
    compliance_rules: dict[str, list[ComplianceRule]] = field(default_factory=dict)     # keyed by invoice_type_id
    _schema_cache: dict[str, dict] = field(default_factory=dict, repr=False)            # cache for build_extraction_schema

    def get_type(self, invoice_type_id: str) -> Optional[InvoiceType]:
        return self.invoice_types.get(invoice_type_id)

    def get_fields(self, invoice_type_id: str) -> list[ExtractionField]:
        return self.extraction_fields.get(invoice_type_id, [])

    def get_rules(self, invoice_type_id: str) -> list[ComplianceRule]:
        return self.compliance_rules.get(invoice_type_id, [])

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
        if invoice_type_id in self._schema_cache:
            return self._schema_cache[invoice_type_id]
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
        self._schema_cache[invoice_type_id] = schema
        return schema

    def build_agent_context(self, invoice_type_id: str) -> str:
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
        for r in self.get_rules(invoice_type_id):
            if r.enabled:
                lines.append(
                    f"- [{r.severity.upper()}] **{r.rule_id}** ({r.rule_name}): "
                    f"{r.agent_hint}"
                )

        return "\n".join(lines)


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
            )
            store.compliance_rules.setdefault(type_id, []).append(cr)
    total_rules = sum(len(v) for v in store.compliance_rules.values())
    logger.info(f"Loaded {total_rules} compliance rules")

    return store
