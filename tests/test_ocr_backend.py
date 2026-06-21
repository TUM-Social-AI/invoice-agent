from PIL import Image

from src.tools.ocr_layout import OcrEngine, PaddleOcrModels, _ocr_with_layout


class _FakePaddleOcrV3:
    def predict(self, image_path):
        return [
            {
                "res": {
                    "rec_texts": ["Invoice: 123", "Total: 42.00"],
                    "rec_scores": [0.93, 0.88],
                    "rec_boxes": [[10, 15, 110, 35], [10, 50, 130, 70]],
                }
            }
        ]


class _FakePaddleOcrLegacy:
    def ocr(self, image_path, cls=False):
        return [
            [
                [
                    [[10, 20], [90, 20], [90, 40], [10, 40]],
                    ("Vendor ABC", 0.91),
                ]
            ]
        ]


def test_paddleocr_v3_result_is_normalized(tmp_path):
    img_path = tmp_path / "page.png"
    Image.new("RGB", (200, 120), "white").save(img_path)
    engine = OcrEngine(
        backend="paddleocr",
        models=PaddleOcrModels(ocr=_FakePaddleOcrV3(), lang="latin"),
        langs=["es", "en", "fr"],
    )

    result = _ocr_with_layout(str(img_path), ocr_engine=engine)

    assert result.image_width == 200
    assert result.image_height == 120
    assert [line.text for line in result.lines] == ["Invoice: 123", "Total: 42.00"]
    assert result.lines[0].confidence == 0.93
    assert result.lines[0].bbox == (10, 15, 110, 35)


def test_paddleocr_legacy_result_is_normalized(tmp_path):
    img_path = tmp_path / "page.png"
    Image.new("RGB", (200, 120), "white").save(img_path)
    engine = OcrEngine(
        backend="paddleocr",
        models=PaddleOcrModels(ocr=_FakePaddleOcrLegacy(), lang="latin"),
        langs=["es", "en", "fr"],
    )

    result = _ocr_with_layout(str(img_path), ocr_engine=engine)

    assert [line.text for line in result.lines] == ["Vendor ABC"]
    assert result.lines[0].confidence == 0.91
    assert result.lines[0].bbox == (10, 20, 90, 40)
