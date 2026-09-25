"""Font choice for slides created by AI."""

import copy
from typing import Any


GENERATION_FONT_FAMILY = "Be Vietnam Pro"
GENERATION_FONT_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Be+Vietnam+Pro:ital,wght@"
    "0,100;0,200;0,300;0,400;0,500;0,600;0,700;0,800;0,900;"
    "1,100;1,200;1,300;1,400;1,500;1,600;1,700;1,800;1,900&display=swap"
)


def generation_font_assets(template_fonts: dict[str, str] | None = None) -> dict[str, str]:
    return {**(template_fonts or {}), GENERATION_FONT_FAMILY: GENERATION_FONT_URL}


def with_generation_font(value: Any) -> Any:
    """Copy slide data and set the font of every text style to the AI default."""
    result = copy.deepcopy(value)
    _set_text_font(result)
    return result


def _set_text_font(value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _set_text_font(item)
        return
    if not isinstance(value, dict):
        return

    if value.get("type") in {"text", "text-list", "chart"} and not isinstance(
        value.get("font"), dict
    ):
        value["font"] = {"family": GENERATION_FONT_FAMILY}
    font = value.get("font")
    if isinstance(font, dict):
        font["family"] = GENERATION_FONT_FAMILY
    for child in value.values():
        _set_text_font(child)
