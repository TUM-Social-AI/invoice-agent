"""Action contract requires page_num for vision page tools."""

from src.agent.action_contract import validate_action_contract


def test_extract_fields_vision_requires_page_num():
    err = validate_action_contract(
        {
            "tool": "extract_fields_vision",
            "params": {"region": "header", "field_subset": ["a"]},
            "reasoning": "x",
        }
    )
    assert err is not None
    assert "page_num" in err


def test_check_compliance_visual_requires_page_num():
    err = validate_action_contract(
        {
            "tool": "check_compliance_visual",
            "params": {},
            "reasoning": "x",
        }
    )
    assert err is not None
    assert "page_num" in err


def test_extract_fields_vision_accepts_page_num_only():
    assert (
        validate_action_contract(
            {
                "tool": "extract_fields_vision",
                "params": {"page_num": 1},
                "reasoning": "x",
            }
        )
        is None
    )


def test_crop_region_still_allows_image_path_without_page_num():
    assert (
        validate_action_contract(
            {
                "tool": "crop_region",
                "params": {"image_path": "/tmp/x.jpg", "region": "header"},
                "reasoning": "x",
            }
        )
        is None
    )
