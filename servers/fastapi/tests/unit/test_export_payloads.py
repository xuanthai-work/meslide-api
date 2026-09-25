from services.export_payloads import json_to_image_payload, pptx_to_json_payload


def test_pptx_payload_omits_optional_concurrency_when_none():
    assert pptx_to_json_payload("/tmp/deck.pptx", None) == {
        "type": "pptx-to-json",
        "pptx_path": "/tmp/deck.pptx",
    }


def test_pptx_payload_keeps_explicit_concurrency():
    assert pptx_to_json_payload("/tmp/deck.pptx", 0) == {
        "type": "pptx-to-json",
        "pptx_path": "/tmp/deck.pptx",
        "slide_concurrency": 0,
    }


def test_json_image_payload_keeps_fonts_and_dimensions():
    data = [{"type": "text", "data": "hello"}]
    fonts = {"A": "/a"}

    assert json_to_image_payload(data, 800, 600, fonts) == {
        "type": "json-to-image",
        "data": data,
        "width": 800,
        "height": 600,
        "fonts": {"A": "/a"},
    }


def test_json_image_payload_omits_empty_fonts():
    data = [{"type": "image", "data": "data:image/png;base64,cG5n"}]

    assert json_to_image_payload(data, 800, 600, {}) == {
        "type": "json-to-image",
        "data": data,
        "width": 800,
        "height": 600,
    }
