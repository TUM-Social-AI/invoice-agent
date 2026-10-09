"""classify_document_type: page inventory first, page 1 at readable resolution otherwise."""

import base64
import io
import json

import pytest
from PIL import Image

from src.agent.state import AgentState
from src.config.loader import load_config
from src.llm.base import LLMResult
from src.tools.vision_llm import CLASSIFY_PAGE_DPI, classify_document_type


class FakeProvider:
    provider_name = "openai"

    def __init__(self, answer: dict):
        self.answer = answer
        self.calls: list[dict] = []

    def generate_json(self, *, model, prompt, images_b64=None, temperature=0.1, timeout_s=240, response_format=None):
        self.calls.append({"prompt": prompt, "images": images_b64})
        return LLMResult(
            content_text=json.dumps(self.answer), content_json=self.answer, raw=None, model=model, provider="openai"
        )


@pytest.fixture(scope="module")
def store():
    return load_config("config/csv")


def _state(tmp_path):
    reportlab = pytest.importorskip("reportlab.pdfgen.canvas")
    pdf = tmp_path / "doc.pdf"
    c = reportlab.Canvas(str(pdf), pagesize=(595, 842))
    c.drawString(72, 760, "Demande de fonds")
    c.save()
    thumb = tmp_path / "thumb.jpg"
    Image.new("RGB", (40, 56), "white").save(thumb)
    st = AgentState(pdf_path=str(pdf), output_dir=str(tmp_path))
    st.page_image_paths = [str(thumb)]
    return st


def test_classifies_from_inventory_of_all_pages_without_an_image(tmp_path, store):
    st = _state(tmp_path)
    st.page_inventory = [
        {"page": 1, "category": "INVOICE_HEADER", "description": "JRS Demande de fonds form"},
        {"page": 2, "category": "LINE_ITEMS", "description": "Supplier invoice for printer cartridges"},
        {"page": 3, "category": "SUPPORTING_DOC", "description": "(no response)"},
    ]
    fake = FakeProvider({"invoice_type_id": "CONSUMIBLES", "confidence": 0.9, "reasoning": "page 2"})

    res = classify_document_type(st, store, "", "model", provider=fake)

    assert res["success"] and res["basis"] == "page_inventory"
    assert st.invoice_type_id == "CONSUMIBLES"
    call = fake.calls[0]
    assert call["images"] is None
    assert "page 2: LINE_ITEMS — Supplier invoice for printer cartridges" in call["prompt"]
    assert "(no response)" not in call["prompt"], "failed inventory entries are left out"


def test_without_inventory_page_one_is_rendered_readable(tmp_path, store):
    st = _state(tmp_path)
    fake = FakeProvider({"invoice_type_id": "VIAJES", "confidence": 0.8, "reasoning": "ticket"})

    res = classify_document_type(st, store, "", "model", provider=fake)

    assert res["success"] and res["basis"] == "first_page_image"
    img = Image.open(io.BytesIO(base64.b64decode(fake.calls[0]["images"][0])))
    # Rendered from the PDF, not the 40x56 thumbnail.
    assert abs(img.width - 595 * CLASSIFY_PAGE_DPI / 72) <= 2


def test_unknown_type_is_rejected(tmp_path, store):
    st = _state(tmp_path)
    st.page_inventory = [{"page": 1, "category": "LINE_ITEMS", "description": "fuel invoice"}]
    fake = FakeProvider({"invoice_type_id": "FUEL", "confidence": 0.9, "reasoning": "x"})

    res = classify_document_type(st, store, "", "model", provider=fake)

    assert not res["success"]
    assert st.invoice_type_id == ""
