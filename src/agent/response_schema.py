"""Response schema builder for the agent turn loop.

Builds the JSON Schema (``oneOf`` branches) used to constrain LLM output
to valid tool-call objects.  Centralising schema construction here means:

- ``turn.py`` stays focused on retry logic and provider dispatch.
- Adding or changing a tool's param schema requires editing only this file.
- The schema is testable in isolation without invoking the agent loop.

Schema constraints applied per turn
------------------------------------
- ``tool``          → enum of currently active tool names
- ``page_num``      → enum of [1..N] once pages have been rendered
- ``field_*``       → enum of field names for the classified invoice type
- ``region``        → fixed set of crop-slice names (header, footer, …)
- ``category``      → fixed set of learning category names
- per-tool branches → ``additionalProperties: false`` prevents cross-tool
                      param leakage (e.g. ``write_learning.category`` on
                      ``extract_fields_vision``).
"""
from __future__ import annotations

from src.agent.state import AgentState
from src.config.loader import ConfigStore


def build_response_schema(
    state: AgentState,
    store: ConfigStore,
    tool_names: list[str] | None = None,
) -> dict:
    """Return the ``oneOf`` JSON Schema for the current agent turn.

    Args:
        state:      Current agent state (used for page count and invoice type).
        store:      Config store (used to resolve field names for the schema).
        tool_names: Subset of tool names the agent is allowed to call this
                    turn.  When ``None`` all known tools are included.

    Returns:
        A JSON Schema dict with a ``oneOf`` key whose branches each
        describe a single valid tool-call object.
    """
    # -- page_num: constrained to [1..N] once any page list is populated ---------
    page_count = (
        len(state.page_image_paths)
        or len(state.compressed_page_paths)
        or state.page_count
    )
    page_num_schema: dict = (
        {"type": "integer", "enum": list(range(1, page_count + 1))}
        if page_count > 0 else {"type": "integer"}
    )

    # -- field names: dynamic enum once invoice type is classified ---------------
    field_names: list[str] = []
    if state.invoice_type_id:
        try:
            field_names = sorted(store.build_extraction_schema(state.invoice_type_id).keys())
        except Exception:
            pass
    field_name_schema: dict = (
        {"type": "string", "enum": field_names} if field_names else {"type": "string"}
    )
    field_list_schema: dict = (
        {"type": "array", "items": {"type": "string", "enum": field_names}}
        if field_names else {"type": "array", "items": {"type": "string"}}
    )

    region_schema = {
        "type": "string",
        "enum": ["header", "footer", "address_block", "totals", "line_items", "body"],
    }
    category_schema = {
        "type": "string",
        "enum": [
            "approaches",
            "extraction_patterns",
            "common_failures",
            "compliance_edge_cases",
            "tool_suggestions",
        ],
    }

    # Tool-specific action branches prevent cross-tool param leakage
    # (e.g. write_learning.category on extract_fields_vision).
    tool_param_schemas: dict[str, dict] = {
        "inspect_file": {"type": "object", "properties": {}, "additionalProperties": False},
        "compress_pages": {
            "type": "object",
            "properties": {
                "dpi": {"type": "integer", "enum": [48, 96, 150, 200]},
                "quality": {"type": "integer", "minimum": 30, "maximum": 90},
                "max_width": {"type": "integer", "enum": [800, 1000, 1200, 1400, 1600, 2000]},
            },
            "additionalProperties": False,
        },
        "classify_document_type": {"type": "object", "properties": {}, "additionalProperties": False},
        "convert_pdf_to_images": {
            "type": "object",
            "properties": {"dpi": {"type": "integer", "enum": [48, 96, 150, 200]}},
            "additionalProperties": False,
        },
        "crop_region": {
            "type": "object",
            "properties": {
                "page_num": page_num_schema,
                "image_path": {"type": "string"},
                "region": region_schema,
                "custom_bbox": {
                    "type": "array",
                    "items": {"type": "number"},
                    "minItems": 4,
                    "maxItems": 4,
                },
            },
            "additionalProperties": False,
        },
        "extract_fields_vision": {
            "type": "object",
            "properties": {
                "page_num": page_num_schema,
                "region": region_schema,
                "hints": {"type": "string"},
                "field_subset": field_list_schema,
                "fields": field_list_schema,
                "field_names": field_list_schema,
            },
            "additionalProperties": False,
        }
        | ({"required": ["page_num"]} if page_count > 0 else {}),
        "check_compliance": {"type": "object", "properties": {}, "additionalProperties": False},
        "check_compliance_visual": {
            "type": "object",
            "properties": {
                "page_num": page_num_schema,
            },
            "additionalProperties": False,
        }
        | ({"required": ["page_num"]} if page_count > 0 else {}),
        "flag_for_human_review": {
            "type": "object",
            "properties": {
                "field_name": field_name_schema,
                "fields": field_list_schema,
                "fields_to_flag": field_list_schema,
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "flag_fields_for_review": {
            "type": "object",
            "properties": {
                "field_name": field_name_schema,
                "fields": field_list_schema,
                "fields_to_flag": field_list_schema,
                "reason": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "inventory_pages": {"type": "object", "properties": {}, "additionalProperties": False},
        "install_package": {
            "type": "object",
            "properties": {"package": {"type": "string"}},
            "required": ["package"],
            "additionalProperties": False,
        },
        "note": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
        "read_learnings": {"type": "object", "properties": {}, "additionalProperties": False},
        "write_learning": {
            "type": "object",
            "properties": {
                "invoice_type_id": {"type": "string"},
                "category": category_schema,
                "content": {"type": "string"},
            },
            "required": ["category", "content"],
            "additionalProperties": False,
        },
        "edit_learning": {
            "type": "object",
            "properties": {
                "learning_id": {"type": "string"},
                "id": {"type": "string"},
                "new_content": {"type": "string"},
                "content": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "delete_learning": {
            "type": "object",
            "properties": {"learning_id": {"type": "string"}, "id": {"type": "string"}},
            "additionalProperties": False,
        },
        "finish": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "all_errors_resolved": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    }

    allowed_tool_list = sorted(tool_names) if tool_names else sorted(tool_param_schemas.keys())
    branches = []
    for tname in allowed_tool_list:
        params_branch = tool_param_schemas.get(tname, {"type": "object", "additionalProperties": True})
        branches.append(
            {
                "type": "object",
                "properties": {
                    "reasoning": {"type": "string"},
                    "tool": {"type": "string", "const": tname},
                    "params": params_branch,
                },
                "required": ["reasoning", "tool", "params"],
                "additionalProperties": False,
            }
        )

    return {"oneOf": branches}
