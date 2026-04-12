"""Moved implementations for learnings_store.py."""

import base64
import ast
import json
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests
from PIL import Image

from src.agent.state import AgentState, FieldResult, RuleResult
from src.compliance.evidence import required_slots_for_rule, link_pages
from src.config.loader import ConfigStore, ComplianceRule
from src.llm.base import LLMProvider

logger = logging.getLogger(__name__)


_LEARNING_ID_RE = re.compile(r"\[L(\d+)\]")

def _next_learning_id(content: str) -> str:
    """Return the next available learning ID as a zero-padded string, e.g. 'L042'."""
    ids = [int(m.group(1)) for m in _LEARNING_ID_RE.finditer(content)]
    return f"L{(max(ids) + 1 if ids else 1):03d}"

def _extract_section(content: str, section_id: str) -> str:
    marker = f"## {section_id}"
    if marker not in content:
        return ""
    start = content.index(marker)
    next_section = content.find("\n## ", start + 1)
    section = content[start:next_section] if next_section != -1 else content[start:]
    return section.strip()

def read_learnings(invoice_type_id: str, learnings_path: str = "learnings/learnings.md") -> str:
    path = Path(learnings_path)
    if not path.exists():
        return ""

    content = path.read_text(encoding="utf-8")

    general = _extract_section(content, "GENERAL")
    specific = _extract_section(content, invoice_type_id) if invoice_type_id else ""

    parts = [p for p in [general, specific] if p]
    return "\n\n".join(parts)

def write_learning(
    invoice_type_id: str,
    category: str,
    content: str,
    learnings_path: str = "learnings/learnings.md",
) -> dict:
    """
    Append a learning to the markdown file under the appropriate section.
    Each entry is assigned a unique ID (e.g. [L001]) so it can later be edited
    or deleted by the agent using edit_learning / delete_learning.

    Skips the write if an identical entry already exists (exact dedup).
    Categories: approaches | extraction_patterns | vision_model_extraction | common_failures | compliance_edge_cases | tool_suggestions
    """
    path = Path(learnings_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    # Dedup: skip if this exact content already exists anywhere in the file
    content_stripped = content.strip()
    if content_stripped.lower() in existing.lower():
        logger.debug(f"write_learning: skipping duplicate — content already exists")
        return {"written": False, "skipped": True, "reason": "duplicate content already exists"}

    learning_id = _next_learning_id(existing)
    timestamp = time.strftime("%Y-%m-%d")
    new_entry = f"- [{learning_id}] [{timestamp}] {content_stripped}"

    type_marker = f"## {invoice_type_id}"
    cat_marker = f"### {category}"

    if type_marker not in existing:
        existing += f"\n\n{type_marker}\n\n{cat_marker}\n{new_entry}\n"
    else:
        type_start = existing.index(type_marker)
        next_type = existing.find("\n## ", type_start + 1)
        type_section = existing[type_start:next_type] if next_type != -1 else existing[type_start:]

        if cat_marker in type_section:
            cat_start = existing.index(cat_marker, type_start)
            next_cat = existing.find("\n### ", cat_start + 1)
            insert_pos = (
                next_cat if (next_cat != -1 and (next_type == -1 or next_cat < next_type))
                else (next_type if next_type != -1 else len(existing))
            )
            existing = existing[:insert_pos] + new_entry + "\n" + existing[insert_pos:]
        else:
            end_of_type = next_type if next_type != -1 else len(existing)
            existing = existing[:end_of_type] + f"\n{cat_marker}\n{new_entry}\n" + existing[end_of_type:]

    path.write_text(existing, encoding="utf-8")
    logger.debug(f"write_learning: wrote [{learning_id}] to {invoice_type_id}/{category}")
    return {"written": True, "learning_id": learning_id, "invoice_type": invoice_type_id, "category": category}

def edit_learning(
    learning_id: str,
    new_content: str,
    learnings_path: str = "learnings/learnings.md",
) -> dict:
    """
    Replace the content of an existing learning entry identified by its ID (e.g. 'L042').
    The original date is preserved; an [edited YYYY-MM-DD] suffix is appended.
    Use when a previously written learning is wrong or outdated.
    """
    path = Path(learnings_path)
    if not path.exists():
        return {"success": False, "error": "Learnings file not found"}

    # Normalise: strip brackets, uppercase, ensure L prefix
    norm_id = learning_id.strip("[]").upper()
    if not norm_id.startswith("L"):
        norm_id = f"L{norm_id}"

    content = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^(- \[{re.escape(norm_id)}\] \[\d{{4}}-\d{{2}}-\d{{2}}\] )(.*)$",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        return {"success": False, "error": f"Learning {norm_id} not found"}

    edited_date = time.strftime("%Y-%m-%d")
    new_line = f"{match.group(1)}{new_content.strip()} [edited {edited_date}]"
    updated = content[:match.start()] + new_line + content[match.end():]
    path.write_text(updated, encoding="utf-8")
    logger.debug(f"edit_learning: updated [{norm_id}]")
    return {"success": True, "learning_id": norm_id, "updated_content": new_content.strip()}

def delete_learning(
    learning_id: str,
    learnings_path: str = "learnings/learnings.md",
) -> dict:
    """
    Remove a learning entry by ID (e.g. 'L042'). The ID is retired — it will not
    be reused. Use when a learning is completely wrong or no longer applicable.
    """
    path = Path(learnings_path)
    if not path.exists():
        return {"success": False, "error": "Learnings file not found"}

    norm_id = learning_id.strip("[]").upper()
    if not norm_id.startswith("L"):
        norm_id = f"L{norm_id}"

    content = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"^- \[{re.escape(norm_id)}\] \[\d{{4}}-\d{{2}}-\d{{2}}\] .*\n?",
        re.MULTILINE,
    )
    if not pattern.search(content):
        return {"success": False, "error": f"Learning {norm_id} not found"}

    updated = pattern.sub("", content)
    path.write_text(updated, encoding="utf-8")
    logger.debug(f"delete_learning: removed [{norm_id}]")
    return {"success": True, "learning_id": norm_id}

def reset_learnings(
    learnings_path: str = "learnings/learnings.md",
    invoice_type_id: str = "",
    category: str = "",
) -> dict:
    """
    Human-facing reset utility (called from CLI, not by the agent).
    - No args: wipe the entire file (backs up to .bak first)
    - invoice_type_id only: remove that type's section
    - invoice_type_id + category: remove that category within the type section
    Returns a summary of what was removed.
    """
    path = Path(learnings_path)
    if not path.exists():
        return {"removed": 0, "note": "File does not exist — nothing to reset"}

    content = path.read_text(encoding="utf-8")
    original_ids = set(_LEARNING_ID_RE.findall(content))

    if not invoice_type_id:
        # Full wipe — back up first
        bak = path.with_suffix(".md.bak")
        bak.write_text(content, encoding="utf-8")
        path.write_text("", encoding="utf-8")
        return {"removed": len(original_ids), "backup": str(bak)}

    type_marker = f"## {invoice_type_id}"
    if type_marker not in content:
        return {"removed": 0, "note": f"Type '{invoice_type_id}' not found in learnings file"}

    type_start = content.index(type_marker)
    next_type = content.find("\n## ", type_start + 1)
    type_end = next_type if next_type != -1 else len(content)

    if not category:
        # Remove entire type section
        bak = path.with_suffix(".md.bak")
        bak.write_text(content, encoding="utf-8")
        section = content[type_start:type_end]
        removed_ids = set(_LEARNING_ID_RE.findall(section))
        updated = content[:type_start] + content[type_end:]
        path.write_text(updated.strip() + "\n", encoding="utf-8")
        return {"removed": len(removed_ids), "backup": str(bak)}

    # Remove one category within a type section
    cat_marker = f"### {category}"
    type_section = content[type_start:type_end]
    if cat_marker not in type_section:
        return {"removed": 0, "note": f"Category '{category}' not found under '{invoice_type_id}'"}

    # Find category bounds within the type section
    cat_rel = type_section.index(cat_marker)
    cat_abs = type_start + cat_rel
    next_cat_abs = content.find("\n### ", cat_abs + 1)
    # Cat ends at next ### within the same ## block, or at end of ## block
    cat_end = min(
        next_cat_abs if next_cat_abs != -1 else len(content),
        type_end,
    )
    section = content[cat_abs:cat_end]
    removed_ids = set(_LEARNING_ID_RE.findall(section))
    bak = path.with_suffix(".md.bak")
    bak.write_text(content, encoding="utf-8")
    updated = content[:cat_abs] + content[cat_end:]
    path.write_text(updated.strip() + "\n", encoding="utf-8")
    return {"removed": len(removed_ids), "backup": str(bak)}
