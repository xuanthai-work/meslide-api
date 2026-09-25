"""Build payloads for export runtime tasks."""

from typing import Any, Mapping


def json_to_image_payload(
    localized_data: list[dict[str, Any]],
    width: int,
    height: int,
    fonts: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "json-to-image",
        "data": localized_data,
        "width": width,
        "height": height,
    }
    if fonts:
        payload["fonts"] = dict(fonts)
    return payload


def pptx_to_json_payload(
    pptx_path: str, slide_concurrency: int | None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "pptx-to-json",
        "pptx_path": pptx_path,
    }
    if slide_concurrency is not None:
        payload["slide_concurrency"] = slide_concurrency
    return payload
