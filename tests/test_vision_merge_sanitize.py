from src.agent.state import AgentState, FieldResult
from src.config.loader import load_config
from src.tools.vision_llm import _sanitize_extracted_string_value, merge_extracted_fields


def test_sanitize_strips_html():
    assert _sanitize_extracted_string_value("<b>FUNDACIO</b>", "string") == "FUNDACIO"
    assert _sanitize_extracted_string_value("  x  ", "string") == "x"
    assert _sanitize_extracted_string_value("<span></span>", "string") is None


def test_merge_applies_sanitize_to_string_fields():
    store = load_config("config/csv")
    schema_full = store.build_extraction_schema("PERS_LOCAL")
    schema = {"role": schema_full["role"]}
    state = AgentState(
        pdf_path="/tmp/x.pdf",
        invoice_type_id="PERS_LOCAL",
        output_dir="/tmp/out",
    )
    merge_extracted_fields(
        state,
        {"role": "<b>Relais</b>", "role_confidence": 0.9},
        schema,
        source_page=1,
        source_region="test",
    )
    assert state.extracted_fields["role"].extracted_value == "Relais"
