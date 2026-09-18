"""Unit tests for the presentation template content module seam.

The template slide-UI construction and content-application helpers used to
live inside ``presentation.py``; they now live in
``presentation_template_content`` so the router module only orchestrates.
These tests exercise the module boundary directly (behavior is covered in
depth by ``tests/unit/test_presentation_template_v2_ui.py`` through the
endpoint re-export).
"""

from api.v1.ppt.endpoints.presentation_template_content import (
    _apply_template_content_to_ui,
    _parse_template_markdown_text,
    _template_slide_ui,
)


def test_parse_template_markdown_text_splits_bold_run():
    parsed = _parse_template_markdown_text("plain **bold** tail")

    assert parsed == [("plain ", {}), ("bold", {"bold": True}), (" tail", {})]


def test_apply_template_content_to_ui_hydrates_text_element():
    ui = {
        "components": [
            {
                "id": "component_0",
                "elements": [
                    {
                        "type": "text",
                        "name": "title",
                        "decorative": False,
                        "runs": [
                            {
                                "text": "placeholder",
                                "font": {"family": "Inter", "size": 24},
                            }
                        ],
                    }
                ],
            }
        ]
    }
    content = {"component_0": {"title": "hello world"}}

    hydrated = _apply_template_content_to_ui(ui, content)

    runs = hydrated["components"][0]["elements"][0]["runs"]
    assert runs == [{"text": "hello world", "font": {"family": "Inter", "size": 24}}]


def test_template_slide_ui_deep_copies_matching_layout():
    layout_payload = {
        "layouts": [
            {
                "id": "layout-id",
                "components": [{"type": "text", "name": "heading"}],
            }
        ]
    }

    ui = _template_slide_ui(layout_payload, "layout-id")

    assert ui["id"] == "layout-id"
    assert ui["components"] == [{"type": "text", "name": "heading"}]
    # Source payload must be deep-copied, not shared.
    assert ui["components"] is not layout_payload["layouts"][0]["components"]


def test_template_slide_ui_returns_none_for_unknown_layout():
    layout_payload = {"layouts": [{"id": "other"}]}

    assert _template_slide_ui(layout_payload, "layout-id") is None