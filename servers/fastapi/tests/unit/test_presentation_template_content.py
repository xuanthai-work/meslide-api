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
    _sanitize_template_markdown_text,
    _template_slide_ui,
    _template_text_runs_from_markdown,
)


def test_sanitize_template_markdown_text_converts_html_lists_to_bullets():
    raw = "<ul><li>Triệu chứng: Khát nước</li><li>Nguyên nhân: Kháng insulin</li><ul>"

    cleaned = _sanitize_template_markdown_text(raw)

    assert cleaned == "• Triệu chứng: Khát nước\n• Nguyên nhân: Kháng insulin"
    assert "<" not in cleaned
    assert ">" not in cleaned


def test_template_text_runs_from_markdown_strips_html_and_keeps_bullets():
    runs = _template_text_runs_from_markdown(
        "<p>Triệu chứng</p><ul><li>Khát nước</li></ul>",
        {"font": {"family": "Inter"}},
    )

    assert runs == [
        {"text": "Triệu chứng\n• Khát nước", "font": {"family": "Inter"}},
    ]


def test_sanitize_template_markdown_text_preserves_latex_tags():
    raw = r"Area is <latex>\pi r^2</latex>."

    assert _sanitize_template_markdown_text(raw) == raw


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


def test_template_slide_ui_defaults_content_images_to_contain():
    layout_payload = {
        "layouts": [
            {
                "id": "cover",
                "components": [
                    {
                        "id": "hero",
                        "elements": [
                            {
                                "type": "image",
                                "name": "photo",
                                "data": "/static/images/placeholder.jpg",
                                "fit": "cover",
                            },
                            {
                                "type": "image",
                                "name": "badge",
                                "data": "/static/icons/badge.svg",
                                "is_icon": True,
                            },
                            {
                                "type": "image",
                                "name": "background",
                                "data": "/static/images/bg.png",
                                "fit": "fill",
                                "clip_path": "path('M 0 0 L 1 1 Z')",
                            },
                            {
                                "type": "image",
                                "name": "diagram",
                                "data": "/static/images/diagram.svg",
                                "fit": "fill",
                            },
                        ],
                    }
                ],
            }
        ]
    }

    ui = _template_slide_ui(layout_payload, "cover")
    photo, badge, background, diagram = ui["components"][0]["elements"]

    # Content photo (even a previously "cover" one) becomes "contain".
    assert photo["fit"] == "contain"
    # Icons, clipped backgrounds, and SVG assets are left untouched.
    assert "fit" not in badge
    assert background["fit"] == "fill"
    assert diagram["fit"] == "fill"
    # The source payload is not mutated.
    assert layout_payload["layouts"][0]["components"][0]["elements"][0]["fit"] == "cover"