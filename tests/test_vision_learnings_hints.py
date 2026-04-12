from src.learnings.vision_hints import vision_model_extraction_bullets
from src.prompts.llm_prompts import format_extraction_accuracy_block
from src.agent.state import AgentState


def test_parses_vision_subsection_bullets():
    md = """
## GENERAL
### vision_model_extraction
- [L001] [2026-04-12] First hint for vision only.

## PERS_LOCAL
### vision_model_extraction
- Second hint unique to type.
- [initial] Duplicate of first hint for vision only.
"""
    bullets = vision_model_extraction_bullets(md)
    assert "First hint for vision only." in bullets
    assert "Second hint unique to type." in bullets
    assert bullets.count("First hint for vision only.") == 1


def test_extraction_suffix_includes_learnings():
    st = AgentState(
        pdf_path="/x.pdf",
        invoice_type_id="PERS_LOCAL",
        output_dir="/o",
        learnings_context="""
## PERS_LOCAL
### vision_model_extraction
- From learnings: look at the timesheet.
""",
    )
    acc = format_extraction_accuracy_block(st)
    assert "From learnings: look at the timesheet." in acc
    assert "Document-specific vision hints (from learnings):" in acc


def test_extraction_suffix_without_learnings():
    st = AgentState(pdf_path="/x.pdf", invoice_type_id="PERS_LOCAL", output_dir="/o", learnings_context="")
    acc = format_extraction_accuracy_block(st)
    assert "Document-specific vision hints" not in acc
