"""
Config loader — reads all CSV configuration files and exposes
typed objects to the rest of the system. No hardcoded rules anywhere else.
"""

import csv
import logging
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from src.models.config_models import (
    ComplianceRuleModel,
    ExtractionFieldModel,
    InvoiceTypeModel,
)

logger = logging.getLogger(__name__)


class InvoiceType(InvoiceTypeModel):
    pass


class ExtractionField(ExtractionFieldModel):
    pass


class ComplianceRule(ComplianceRuleModel):
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


# Config sections a rule's check_value may reference as {section.key} placeholders.
RULE_PARAM_SECTIONS = ("project", "compliance")

# {project.start_date}, {project.file_number|fallback text}, {eur:compliance.cash_limit_eur}
_PLACEHOLDER_RE = re.compile(
    r"\{(?:(?P<fmt>eur):)?(?P<path>[a-z_][a-z0-9_]*(?:\.[a-z0-9_]+)+)(?:\|(?P<fallback>[^{}]*))?\}"
)


def _render_param(value: Any) -> str | None:
    """Render a config value for rule text. None when the value is unset/empty."""
    if value is None:
        return None
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        items = [r for r in (_render_param(v) for v in value) if r]
        return " or ".join(f'"{i}"' for i in items) if items else None
    if isinstance(value, dict):
        items = [f"{k} {r}" for k, r in ((k, _render_param(v)) for k, v in value.items()) if r]
        return ", ".join(items) if items else None
    s = str(value).strip()
    return s or None


def _lookup_param(params: dict, path: str) -> Any:
    node: Any = params
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def _render_eur_amount(amount: Any, params: dict) -> str | None:
    """'2500 EUR (= 1,639,892 XAF/XOF)' using compliance.currency_per_eur for the equivalents."""
    try:
        eur = float(amount)
    except (TypeError, ValueError):
        return None
    rates = _lookup_param(params, "compliance.currency_per_eur") or {}
    by_rate: dict[float, list[str]] = {}  # currencies sharing a rate (pegged aliases) are listed once
    for cur, rate in rates.items():
        try:
            r = float(rate)
        except (TypeError, ValueError):
            continue
        if str(cur).upper() != "EUR" and r > 0:
            by_rate.setdefault(r, []).append(str(cur))
    equivalents = []
    for r, curs in by_rate.items():
        local = eur * r
        amount_s = f"{local:,.0f}" if local >= 1000 else f"{local:,.2f}"
        equivalents.append(f"{amount_s} {'/'.join(curs)}")
    base = f"{_render_param(eur)} EUR"
    return f"{base} (= {', '.join(equivalents)})" if equivalents else base


def resolve_rule_text(text: str, params: dict) -> tuple[str, list[str]]:
    """
    Substitute {section.key} placeholders in rule text with config values.
    Returns (text, unresolved placeholder paths). An unset value with a |fallback uses the fallback;
    without one the placeholder is left in place and reported as unresolved.
    """
    unresolved: list[str] = []

    def _sub(m: re.Match) -> str:
        path = m.group("path")
        raw = _lookup_param(params, path)
        if m.group("fmt") == "eur" and raw not in (None, ""):
            rendered = _render_eur_amount(raw, params)
        else:
            rendered = _render_param(raw)
        if rendered is not None:
            return rendered
        if m.group("fallback") is not None:
            return m.group("fallback").strip()
        unresolved.append(path)
        return m.group(0)

    return _PLACEHOLDER_RE.sub(_sub, text or ""), unresolved


def unresolved_rule_params(rule: ComplianceRule) -> list[str]:
    """Placeholder paths still left in a rule's check_value (config value not set)."""
    return [
        m.group("path")
        for m in _PLACEHOLDER_RE.finditer(rule.check_value or "")
        if m.group("fallback") is None
    ]


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
    # Config sections (RULE_PARAM_SECTIONS) substituted into {section.key} rule placeholders.
    rule_params: dict = Field(default_factory=dict, repr=False)

    def set_rule_params(self, app_config: dict) -> None:
        self.rule_params = {k: (app_config or {}).get(k) or {} for k in RULE_PARAM_SECTIONS}

    def get_type(self, invoice_type_id: str) -> Optional[InvoiceType]:
        return self.invoice_types.get(invoice_type_id)

    def get_fields(self, invoice_type_id: str) -> list[ExtractionField]:
        return self.extraction_fields.get(invoice_type_id, [])

    def get_rules(
        self,
        invoice_type_id: str,
        active_rule_groups: list[str] | None = None,
    ) -> list[ComplianceRule]:
        rules = filter_compliance_rules_by_groups(
            list(self.compliance_rules.get(invoice_type_id, [])), active_rule_groups
        )
        resolved = []
        for r in rules:
            text, _ = resolve_rule_text(r.check_value, self.rule_params)
            resolved.append(r if text == r.check_value else r.model_copy(update={"check_value": text}))
        return resolved

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
            entry: dict = {
                "field_id": f.field_id,
                "type": f.data_type,
                "label": f.field_label,
                "required": f.required,
                "hint": f.extraction_hint,
                "region": f.page_region,
                "aliases": f.aliases,
            }
            if f.allowed_values:
                entry["enum"] = f.allowed_values
            schema[f.field_name] = entry
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
            allowed_suffix = f" | Allowed values: {', '.join(f.allowed_values)}" if f.allowed_values else ""
            lines.append(
                f"- **{f.field_name}** ({f.field_label}, {req}, region={f.page_region}): "
                f"{f.extraction_hint} | Aliases: {', '.join(f.aliases)}{allowed_suffix}"
            )

        lines.append("\n### Compliance Rules to Satisfy")
        for r in self.get_rules(invoice_type_id, active_rule_groups):
            if r.enabled:
                lines.append(
                    f"- [{r.severity.upper()}] **{r.rule_id}** ({r.rule_name}): "
                    f"{r.agent_hint}"
                )

        return "\n".join(lines)

def _load_allowed_values(base: Path) -> dict[tuple[str, str], list[str]]:
    path = base / "allowed_values.csv"
    if not path.exists():
        logger.warning("Missing %s — no enum constraints will be applied to extracted fields", path.name)
        return {}
    lookup: dict[tuple[str, str], list[str]] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            field_name = row["field_name"].strip()
            type_id = row.get("invoice_type_id", "").strip()
            value = row["value"].strip()
            if value:
                lookup.setdefault((field_name, type_id), []).append(value)
    return lookup


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
                budget_line=(row.get("budget_line") or "").strip(),
            )
            store.invoice_types[t.invoice_type_id] = t
    logger.info(f"Loaded {len(store.invoice_types)} invoice types")

    # --- Allowed values (must load before extraction fields) ---
    allowed_lookup = _load_allowed_values(base)
    logger.info(f"Loaded allowed_values for {len(allowed_lookup)} (field_name, invoice_type_id) pairs")

    # --- Extraction fields ---
    fields_path = base / "extraction_fields.csv"
    with open(fields_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            type_id = row["invoice_type_id"].strip()
            if type_id not in store.invoice_types:
                continue
            field_name = row["field_name"].strip()
            aliases_raw = row.get("aliases", "")
            aliases = [a.strip() for a in aliases_raw.split(",") if a.strip()]
            allowed = (
                allowed_lookup.get((field_name, type_id))
                or allowed_lookup.get((field_name, ""))
                or []
            )
            ef = ExtractionField(
                field_id=row["field_id"].strip(),
                invoice_type_id=type_id,
                field_name=field_name,
                field_label=row["field_label"].strip(),
                data_type=row["data_type"].strip(),
                required=row["required"].lower() == "true",
                extraction_hint=row["extraction_hint"].strip(),
                page_region=row["page_region"].strip(),
                aliases=aliases,
                allowed_values=allowed,
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
                issue_code=(row.get("issue_code") or "").strip(),
            )
            store.compliance_rules.setdefault(type_id, []).append(cr)
    total_rules = sum(len(v) for v in store.compliance_rules.values())
    logger.info(f"Loaded {total_rules} compliance rules")

    store.employee_name_role_denylist = _load_employee_name_role_denylist(base)
    logger.info(f"Loaded {len(store.employee_name_role_denylist)} employee_name role denylist phrases")

    return store
