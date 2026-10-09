"""classify_document_type: page inventory first, page 1 at readable resolution otherwise."""

import base64
import io
import json

import pytest
from PIL import Image

from src.agent.state import AgentState
from src.config.loader import load_config
from src.llm.base import LLMResult
from src.sources.models import SourceProvenance
from src.tools.vision_llm import CLASSIFY_PAGE_DPI, budget_line_hint, classify_document_type


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


def _inventory_state(tmp_path, pdf_name="doc.pdf"):
    """Inventory-based classification never renders the PDF, so a thumbnail is enough."""
    thumb = tmp_path / "thumb.jpg"
    Image.new("RGB", (40, 56), "white").save(thumb)
    st = AgentState(pdf_path=str(tmp_path / pdf_name), output_dir=str(tmp_path))
    st.page_image_paths = [str(thumb)]
    return st


def test_prompt_lists_all_configured_types(tmp_path, store):
    st = _inventory_state(tmp_path)
    st.page_inventory = [{"page": 1, "category": "LINE_ITEMS", "description": "Décharge de prime animateur"}]
    fake = FakeProvider({"invoice_type_id": "VOLUNTARIOS", "confidence": 0.9, "reasoning": "page 1"})

    res = classify_document_type(st, store, "", "model", provider=fake)

    assert res["success"] and st.invoice_type_id == "VOLUNTARIOS"
    for type_id in ("VOLUNTARIOS", "SERV_TECNICOS", "FUNCIONAMIENTO", "VIAJES", "CONSUMIBLES"):
        assert f'"{type_id}"' in fake.calls[0]["prompt"]
    assert "File name hint" not in fake.calls[0]["prompt"], "doc.pdf has no budget-line prefix"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("A.7. Serv técn y prof-U0256-25.pdf", "SERV_TECNICOS"),
        ("A.8.- Fonctionnement-U0324-25.pdf", "FUNCIONAMIENTO"),
        ("A.5.d.- Personnel volontaire-U0223-25.pdf", "VOLUNTARIOS"),
        ("A.4.c.- Consumibles-A2745-25.pdf", "CONSUMIBLES"),
        ("A.6.- Viajes, alojamientos y dietas-A3693-25.pdf", "VIAJES"),
        ("a-7-serv-t-cn-y-prof-u0256-25.pdf", "SERV_TECNICOS"),
        ("A.10.- Auditoria-X1.pdf", None),
        ("A.5.- Personal-X1.pdf", None),
        ("invoice.pdf", None),
    ],
)
def test_budget_line_hint_from_file_name(store, name, expected):
    st = AgentState(pdf_path=f"/tmp/{name}", output_dir="/tmp/out")
    hint = budget_line_hint(st, store)
    if expected is None:
        assert hint == ""
    else:
        assert f'"{expected}"' in hint and "prior" in hint


def test_budget_line_hint_prefers_original_name_and_reaches_prompt(tmp_path, store):
    st = _inventory_state(tmp_path, "a-7-serv-t-cn-y-prof-pc0060-25.pdf")
    st.source_provenance = SourceProvenance.from_local_path_minimal(st.pdf_path).model_copy(
        update={"display_name": "A.7. Serv técn y prof-PC0060-25.pdf"}
    )
    st.page_inventory = [{"page": 1, "category": "LINE_ITEMS", "description": "Airtel SIM cards and data credit"}]
    fake = FakeProvider({"invoice_type_id": "FUNCIONAMIENTO", "confidence": 0.8, "reasoning": "SIM cards"})

    res = classify_document_type(st, store, "", "model", provider=fake)

    assert res["success"] and st.invoice_type_id == "FUNCIONAMIENTO", "the hint does not override the model"
    assert 'budget line A.7, which maps to "SERV_TECNICOS"' in fake.calls[0]["prompt"]
