from dataclasses import dataclass

import requests

from src.agent.state import AgentState
from src.config.loader import ComplianceRule
from src.tools.compliance_visual import check_compliance_visual


@dataclass
class FakeLLMResult:
    content_text: str
    content_json: dict | None
    raw: dict
    model: str
    provider: str


class FakeProvider:
    provider_name = "fake"

    def __init__(self):
        self.last_images_count = 0

    def generate_json(self, **kwargs):
        self.last_images_count = len(kwargs.get("images_b64", []))
        return FakeLLMResult(
            content_text='{"R_VIS_001":{"passes":true,"confidence":0.9,"observation":"seen on page_num=1 and page_num=2"}}',
            content_json={
                "R_VIS_001": {
                    "passes": True,
                    "confidence": 0.9,
                    "observation": "seen on page_num=1 and page_num=2",
                }
            },
            raw={},
            model=kwargs.get("model", "fake"),
            provider=self.provider_name,
        )


def test_check_compliance_visual_uses_multiple_evidence_pages(tmp_path):
    # tiny png placeholders so base64 encoding works
    import base64
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+R0kAAAAASUVORK5CYII="
    )
    p1 = tmp_path / "p1.png"
    p2 = tmp_path / "p2.png"
    p1.write_bytes(png)
    p2.write_bytes(png)

    state = AgentState(pdf_path="x.pdf", output_dir=str(tmp_path), invoice_type_id="VIAJES")
    state.page_image_paths = [str(p1), str(p2)]
    state.page_inventory = [
        {"page": 1, "category": "INVOICE_HEADER", "description": "invoice header with amount"},
        {"page": 2, "category": "SUPPORTING_DOC", "description": "payment proof receipt"},
    ]
    state.page_facts = {
        1: {"category": "INVOICE_HEADER", "entities": {"amounts": [100.0], "dates": ["2025-01-01"], "references": ["INV1"]}},
        2: {"category": "SUPPORTING_DOC", "entities": {"amounts": [100.0], "dates": ["2025-01-01"], "references": ["INV1"], "payment_markers": ["payment"]}},
    }

    rules = [
        ComplianceRule(
            rule_id="R_VIS_001",
            invoice_type_id="VIAJES",
            rule_name="proof_of_payment_attached",
            field_id="x",
            check_type="visual_check",
            check_value="payment proof must be visible",
            severity="error",
            agent_hint="",
            error_message="",
            page_region="body",
            enabled=True,
        )
    ]

    provider = FakeProvider()
    res = check_compliance_visual(
        state=state,
        image_path=str(p1),
        page_num=1,
        rules=rules,
        ollama_url="http://unused",
        model="fake",
        provider=provider,
        max_evidence_pages=4,
    )

    assert res["success"] is True
    assert provider.last_images_count >= 2
    assert len(res.get("evidence_pages", [])) >= 2


def test_check_compliance_visual_retries_until_single_image_after_server_errors(tmp_path):
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7+R0kAAAAASUVORK5CYII="
    )
    p1 = tmp_path / "p1.png"
    p2 = tmp_path / "p2.png"
    p3 = tmp_path / "p3.png"
    for p in (p1, p2, p3):
        p.write_bytes(png)

    state = AgentState(pdf_path="x.pdf", output_dir=str(tmp_path), invoice_type_id="VIAJES")
    state.page_image_paths = [str(p1), str(p2), str(p3)]
    state.page_inventory = [
        {"page": 1, "category": "INVOICE_HEADER", "description": "header"},
        {"page": 2, "category": "LINE_ITEMS", "description": "lines"},
        {"page": 3, "category": "SIGNATURE_STAMP", "description": "stamps"},
    ]
    state.page_facts = {}

    rules = [
        ComplianceRule(
            rule_id="R_VIS_001",
            invoice_type_id="VIAJES",
            rule_name="stamp_present",
            field_id="x",
            check_type="visual_check",
            check_value="official stamp must be visible",
            severity="error",
            agent_hint="",
            error_message="",
            page_region="body",
            enabled=True,
        )
    ]

    ok = FakeLLMResult(
        content_text='{"R_VIS_001":{"passes":false,"confidence":0.8,"observation":"no stamp on page_num=3"}}',
        content_json=None,
        raw={},
        model="fake",
        provider="fake",
    )

    class FlakyProvider:
        provider_name = "fake"

        def __init__(self):
            self.image_counts: list[int] = []

        def generate_json(self, **kwargs):
            n = len(kwargs.get("images_b64", []))
            self.image_counts.append(n)
            if n > 1:
                raise requests.HTTPError(response=requests.Response())
            return ok

    prov = FlakyProvider()
    res = check_compliance_visual(
        state=state,
        image_path=str(p1),
        page_num=1,
        rules=rules,
        ollama_url="http://unused",
        model="fake",
        provider=prov,
        max_evidence_pages=4,
    )

    assert res["success"] is True
    assert prov.image_counts[0] > 1
    assert prov.image_counts[-1] == 1
    assert res.get("evidence_pages") == [1]

