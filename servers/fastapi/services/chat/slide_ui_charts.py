"""Chart/image/visual-insert helpers for Template V2 slide UI.

Extracted from ``services.chat.slide_ui_helpers`` so chart normalization and
asset-fit logic can be tested and reused without the layout/group helpers.
``slide_ui_helpers`` re-exports the public names used by chat tools.
"""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from templates.v2.content import template_asset_prompt

REMOVED_GEOMETRY_ELEMENT_TYPES = {
    "circle",
    "ellipse",
    "line",
    "polygon",
    "rectangle",
    "vector_shape",
}
SUPPORTED_CHART_TYPES = {
    "area",
    "bar",
    "bubble",
    "donut",
    "horizontal_bar",
    "horizontal_stacked_bar",
    "line",
    "pie",
    "polar_area",
    "radar",
    "scatter",
    "stacked_bar",
}
DATA_LABEL_POSITIONS = {"base", "mid", "top", "outside"}
DEFAULT_CHART_COLORS = [
    "#7F22FE",
    "#155DFC",
    "#F59E0B",
    "#12B76A",
    "#EF4444",
    "#06B6D4",
    "#8B5CF6",
    "#64748B",
]
THEME_GRAPH_COLOR_KEYS = tuple(f"graph_{index}" for index in range(10))
CHART_UPDATE_KEYS = {
    "axis_color",
    "categories",
    "chart_type",
    "colors",
    "data_labels",
    "grid_color",
    "legend",
    "legend_color",
    "series",
    "title",
    "title_color",
    "x_axis",
    "x_axis_grid",
    "x_axis_title",
    "y_axis",
    "y_axis_grid",
    "y_axis_title",
}


def _normalize_chart_tree(node: Any, theme: dict[str, Any] | None = None) -> None:
    if isinstance(node, dict):
        if node.get("type") == "chart":
            _normalize_chart_element(node, theme)
        for value in node.values():
            _normalize_chart_tree(value, theme)
    elif isinstance(node, list):
        for value in node:
            _normalize_chart_tree(value, theme)


def _normalize_chart_element(
    element: dict[str, Any],
    theme: dict[str, Any] | None = None,
) -> None:
    element.pop("data_labels_color", None)
    element.pop("dataLabelsColor", None)
    element.pop("grid", None)
    if "title_color" not in element and "titleColor" in element:
        element["title_color"] = element.get("titleColor")
    if "legend_color" not in element and "legendColor" in element:
        element["legend_color"] = element.get("legendColor")

    chart_type = _normalize_chart_type(
        element.get("chart_type"),
        fallback=str(element.get("chart_type") or "bar"),
    )
    element["chart_type"] = chart_type

    legacy_data = _normalize_legacy_chart_data(element.get("data"))
    series = _normalize_chart_series(
        element.get("series"),
        fallback_name=str(element.get("title") or "Series 1"),
    )
    if not series and legacy_data:
        series = [
            {
                "name": str(element.get("title") or "Series 1"),
                "values": [item["value"] for item in legacy_data],
            }
        ]
    if not series:
        series = [{"name": "Series 1", "values": [0]}]

    legacy_categories = [item["label"] for item in legacy_data]
    categories = _normalize_chart_categories(
        element.get("categories") or legacy_categories,
        _max_chart_value_length(series),
    )
    _validate_chart_shape(chart_type, categories, series)
    category_count = max(1, len(categories), _max_chart_value_length(series))
    categories = _normalize_chart_categories(categories, category_count)
    for item in series:
        item["values"] = _pad_chart_values(
            item.get("values"),
            category_count,
        )

    if not _read_chart_colors(element.get("colors")):
        legacy_colors = [
            color
            for item in legacy_data
            if (color := _normalize_chart_color(item.get("color")))
        ]
        if legacy_colors:
            element["colors"] = legacy_colors

    color_count = _chart_color_target_count(chart_type, categories, series)
    colors = _resolve_chart_colors(
        element,
        theme=theme,
        count=color_count,
    )
    element["colors"] = colors
    element["color"] = colors[0]
    element["categories"] = categories
    element["series"] = series
    element["data"] = _chart_data_from_series(
        categories=categories,
        series=series,
        colors=colors,
        chart_type=chart_type,
    )

    theme_colors = _theme_colors(theme)
    if not _normalize_chart_color(element.get("axis_color")):
        element["axis_color"] = (
            _normalize_chart_color(theme_colors.get("background_text"))
            or _normalize_chart_color(theme_colors.get("primary"))
            or "#475467"
        )
    else:
        element["axis_color"] = _normalize_chart_color(element.get("axis_color"))
    if not _normalize_chart_color(element.get("grid_color")):
        element["grid_color"] = (
            _normalize_chart_color(theme_colors.get("stroke"))
            or _normalize_chart_color(theme_colors.get("card"))
            or "#D0D5DD"
        )
    else:
        element["grid_color"] = _normalize_chart_color(element.get("grid_color"))
    if _normalize_chart_color(element.get("title_color")):
        element["title_color"] = _normalize_chart_color(element.get("title_color"))
    if _normalize_chart_color(element.get("legend_color")):
        element["legend_color"] = _normalize_chart_color(element.get("legend_color"))

    if (
        "x_axis" not in element
        and chart_type not in {"pie", "donut", "polar_area", "radar"}
    ):
        element["x_axis"] = True
    if (
        "y_axis" not in element
        and chart_type not in {"pie", "donut", "polar_area", "radar"}
    ):
        element["y_axis"] = True
    if "x_axis_grid" not in element and chart_type not in {"pie", "donut"}:
        element["x_axis_grid"] = True
    if "y_axis_grid" not in element and chart_type not in {"pie", "donut"}:
        element["y_axis_grid"] = True
    if "legend" not in element:
        element["legend"] = chart_type in {"pie", "donut"} or len(series) > 1
    element["data_labels"] = _normalize_chart_data_labels(element.get("data_labels"))


def _apply_chart_content_update(
    element: dict[str, Any],
    chart: dict[str, Any],
    theme: dict[str, Any] | None = None,
) -> None:
    for source_key, target_key in (
        ("chart_type", "chart_type"),
        ("title", "title"),
        ("title_color", "title_color"),
        ("legend_color", "legend_color"),
        ("categories", "categories"),
        ("series", "series"),
        ("colors", "colors"),
        ("axis_color", "axis_color"),
        ("grid_color", "grid_color"),
        ("x_axis_title", "x_axis_title"),
        ("y_axis_title", "y_axis_title"),
    ):
        if source_key in chart:
            element[target_key] = copy.deepcopy(chart[source_key])

    for source_key, target_key in (
        ("x_axis", "x_axis"),
        ("y_axis", "y_axis"),
        ("x_axis_grid", "x_axis_grid"),
        ("y_axis_grid", "y_axis_grid"),
        ("data_labels", "data_labels"),
        ("legend", "legend"),
    ):
        if source_key in chart:
            element[target_key] = chart[source_key]

    _normalize_chart_element(element, theme)


def _normalize_chart_data_labels(value: Any) -> str | None:
    if value is True:
        return "top"
    if value is False or value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in DATA_LABEL_POSITIONS:
            return normalized
    return None


def _validate_visual_insert_tree(node: Any) -> None:
    if isinstance(node, dict):
        _validate_current_element_model(node)
        if node.get("type") == "chart" and not _chart_element_has_explicit_data(node):
            raise ValueError(
                "Chart elements must include numeric data via series.values or "
                "data before they can be added."
            )
        if node.get("type") == "table" and not _table_element_has_explicit_data(node):
            raise ValueError(
                "Table elements must include headers/columns and rows before "
                "they can be added."
            )
        if node.get("type") == "image" and not _image_element_has_explicit_data(node):
            raise ValueError(
                "Image elements must include an image/icon URL in data before "
                "they can be added."
            )
        for value in node.values():
            _validate_visual_insert_tree(value)
    elif isinstance(node, list):
        for value in node:
            _validate_visual_insert_tree(value)


def _validate_chart_insert_tree(node: Any) -> None:
    _validate_visual_insert_tree(node)


def _validate_current_element_model(element: dict[str, Any]) -> None:
    element_type = element.get("type")
    if element_type in REMOVED_GEOMETRY_ELEMENT_TYPES:
        raise ValueError(
            f"Element type '{element_type}' was removed. Use type='vector' with "
            "points, shape='ellipse' for circles/ellipses or shape='polygon' for "
            "other shapes, and closed=false for lines."
        )
    if element_type == "vector":
        _validate_vector_element(element)
    if element_type == "infographic":
        _validate_infographic_element(element)


def _validate_vector_element(element: dict[str, Any]) -> None:
    points = element.get("points")
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("Vector elements require at least two numeric points.")
    for point in points:
        if not isinstance(point, dict) or not all(
            isinstance(point.get(axis), (int, float)) for axis in ("x", "y")
        ):
            raise ValueError("Each vector point must contain numeric x and y values.")

    shape = element.get("shape")
    if shape is not None and shape not in {"polygon", "ellipse"}:
        raise ValueError("vector.shape must be 'polygon' or 'ellipse'.")
    curve = element.get("curve")
    if curve is not None and (
        not isinstance(curve, dict) or curve.get("type") != "smooth"
    ):
        raise ValueError("vector.curve supports only type='smooth'.")
    corner_radii = element.get("corner_radii")
    if isinstance(corner_radii, list) and len(corner_radii) != len(points):
        raise ValueError("vector.corner_radii must match the number of points.")


def _validate_infographic_element(element: dict[str, Any]) -> None:
    data = element.get("data")
    if not isinstance(data, dict):
        raise ValueError("Infographic elements require a nested data object.")
    if data.get("type") not in {"progress_bar", "gauge"}:
        raise ValueError("infographic.data.type must be 'progress_bar' or 'gauge'.")
    for key in ("min_value", "max_value", "value"):
        if not isinstance(data.get(key), (int, float)):
            raise ValueError(f"infographic.data.{key} must be numeric.")
    if float(data["max_value"]) <= float(data["min_value"]):
        raise ValueError("infographic.data.max_value must exceed min_value.")


def _chart_element_has_explicit_data(element: dict[str, Any]) -> bool:
    return bool(
        _normalize_chart_series(
            element.get("series"),
            fallback_name=str(element.get("title") or "Series 1"),
        )
        or _normalize_legacy_chart_data(element.get("data"))
    )


def _table_element_has_explicit_data(element: dict[str, Any]) -> bool:
    columns = element.get("columns")
    headers = element.get("headers")
    rows = element.get("rows")
    has_columns = isinstance(columns, list) and len(columns) > 0
    has_headers = isinstance(headers, list) and len(headers) > 0
    has_rows = isinstance(rows, list) and len(rows) > 0
    return (has_columns or has_headers) and has_rows


def _image_element_has_explicit_data(element: dict[str, Any]) -> bool:
    asset_url = _template_asset_url(element)
    return bool(asset_url and _looks_like_asset_reference(asset_url))


def _normalize_image_tree(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "image":
            _normalize_image_element(node)
        for value in node.values():
            _normalize_image_tree(value)
    elif isinstance(node, list):
        for value in node:
            _normalize_image_tree(value)


def _normalize_image_element(element: dict[str, Any]) -> None:
    asset_url = _template_asset_url(element)
    if asset_url and _looks_like_asset_reference(asset_url):
        element["data"] = asset_url
    element.setdefault("is_icon", False)
    prompt = template_asset_prompt(
        element,
        is_icon=element.get("is_icon") is True,
    )
    if prompt:
        element.setdefault("prompt", prompt)


def _normalize_generated_image_fit(
    element: dict[str, Any],
    asset_url: str | None,
) -> None:
    if element.get("is_icon") is True or element.get("fit") == "cover":
        return
    if _has_image_clip_path(element):
        return
    if _looks_like_svg_asset_reference(asset_url):
        return
    element["fit"] = "cover"


def _has_image_clip_path(element: dict[str, Any]) -> bool:
    for key in ("clip_path", "clipPath", "clippath"):
        value = element.get(key)
        if isinstance(value, str) and value.strip() and value.strip().lower() != "none":
            return True
    return False


def _looks_like_svg_asset_reference(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().lower()
    if normalized.startswith("data:image/svg+xml"):
        return True
    return normalized.split("?", 1)[0].split("#", 1)[0].endswith(".svg")


def _normalize_chart_type(value: Any, *, fallback: str = "bar") -> str:
    raw = value if isinstance(value, str) else fallback
    normalized = raw.strip().lower()
    return normalized if normalized in SUPPORTED_CHART_TYPES else "bar"


def _normalize_chart_series(
    value: Any,
    *,
    fallback_name: str,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    series: list[dict[str, Any]] = []
    for index, item in enumerate(value[:20]):
        if isinstance(item, dict):
            raw_values = item.get("values")
            if not isinstance(raw_values, list):
                raw_values = item.get("data")
            if not isinstance(raw_values, list):
                continue
            name = item.get("name")
            series_name = (
                str(name).strip()
                if isinstance(name, str) and name.strip()
                else fallback_name
                if index == 0 and fallback_name
                else f"Series {index + 1}"
            )
        elif isinstance(item, list):
            raw_values = item
            series_name = (
                fallback_name
                if index == 0 and fallback_name
                else f"Series {index + 1}"
            )
        else:
            continue

        values = [
            number
            for raw_value in raw_values[:100]
            if (number := _chart_number(raw_value)) is not None
        ]
        if values:
            series.append({"name": series_name, "values": values})
    return series


def _chart_number(value: Any) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        number = float(value)
        return int(number) if number.is_integer() else number
    if isinstance(value, str):
        match = re.search(
            r"[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][-+]?\d+)?",
            value.strip(),
        )
        if not match:
            return None
        try:
            number = float(match.group(0).replace(",", ""))
        except ValueError:
            return None
        if math.isfinite(number):
            return int(number) if number.is_integer() else number
    return None


def _normalize_legacy_chart_data(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    rows: list[dict[str, Any]] = []
    for item in value[:100]:
        direct_number = _chart_number(item)
        if direct_number is not None:
            rows.append({"label": "", "value": direct_number})
            continue

        if not isinstance(item, dict):
            continue
        number = _first_chart_number(item.get("value"), item.get("data"), item.get("y"))
        if number is None:
            continue

        row: dict[str, Any] = {
            "label": _first_chart_label(
                item.get("label"),
                item.get("name"),
                item.get("category"),
                item.get("x"),
            ),
            "value": number,
        }
        color = _normalize_chart_color(item.get("color"))
        if color:
            row["color"] = color
        rows.append(row)
    return rows


def _first_chart_number(*values: Any) -> float | int | None:
    for value in values:
        number = _chart_number(value)
        if number is not None:
            return number
    return None


def _first_chart_label(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        label = str(value).strip()
        if label:
            return label
    return ""


def _normalize_chart_categories(
    value: Any,
    length: int,
) -> list[str]:
    raw_values = value if isinstance(value, list) and value else []
    target_length = max(1, length, len(raw_values))
    return [
        str(raw_values[index]).strip()
        if index < len(raw_values) and str(raw_values[index]).strip()
        else f"Item {index + 1}"
        for index in range(target_length)
    ]


def _max_chart_value_length(series: list[dict[str, Any]]) -> int:
    return max(
        0,
        *[
            len(item.get("values"))
            for item in series
            if isinstance(item.get("values"), list)
        ],
    )


def _validate_chart_shape(
    chart_type: str,
    categories: list[str],
    series: list[dict[str, Any]],
) -> None:
    if chart_type in {"pie", "donut"} and len(series) > 1:
        raise ValueError("Pie and donut charts support exactly one series.")
    if not categories:
        return
    category_count = len(categories)
    for item in series:
        values = item.get("values")
        if isinstance(values, list) and len(values) != category_count:
            raise ValueError("Each chart series must match the category count.")


def _pad_chart_values(value: Any, length: int) -> list[Any]:
    values = value if isinstance(value, list) else []
    padded = values[:length]
    while len(padded) < length:
        padded.append(0)
    return padded


def _resolve_chart_colors(
    element: dict[str, Any],
    *,
    theme: dict[str, Any] | None,
    count: int,
) -> list[str]:
    source_colors = _read_chart_colors(element.get("colors"))
    if not source_colors:
        source_colors = _theme_chart_palette(theme)
    if not source_colors:
        source_colors = [
            _normalize_chart_color(element.get("color")) or DEFAULT_CHART_COLORS[0]
        ]

    target_count = min(12, max(1, count, len(source_colors)))
    return [
        source_colors[index % len(source_colors)]
        for index in range(target_count)
    ]


def _read_chart_colors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    colors = [_normalize_chart_color(item) for item in value]
    return [color for color in colors if color]


def _chart_color_target_count(
    chart_type: str,
    categories: list[str],
    series: list[dict[str, Any]],
) -> int:
    if chart_type not in {"pie", "donut"} and len(series) > 1:
        return len(series)
    return len(categories)


def _chart_data_from_series(
    *,
    categories: list[str],
    series: list[dict[str, Any]],
    colors: list[str],
    chart_type: str,
) -> list[dict[str, Any]]:
    first = series[0] if series else {"values": [0]}
    values = first.get("values") if isinstance(first.get("values"), list) else [0]
    category_colors = chart_type in {"pie", "donut"} or len(series) == 1
    return [
        {
            "label": (
                categories[index] if index < len(categories) else f"Item {index + 1}"
            ),
            "value": _chart_number(values[index] if index < len(values) else 0) or 0,
            "color": colors[index % len(colors)] if category_colors else colors[0],
        }
        for index in range(min(8, max(len(categories), len(values), 1)))
    ]


def _theme_chart_palette(theme: dict[str, Any] | None) -> list[str]:
    colors = _theme_colors(theme)
    palette = [
        color
        for color in (
            _normalize_chart_color(colors.get(key)) for key in THEME_GRAPH_COLOR_KEYS
        )
        if color
    ]
    if palette:
        return palette
    fallback_keys = ("primary", "card", "stroke", "background_text", "primary_text")
    return [
        color
        for color in (_normalize_chart_color(colors.get(key)) for key in fallback_keys)
        if color
    ]


def _theme_colors(theme: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(theme, dict):
        return {}
    data = theme.get("data")
    if isinstance(data, dict):
        colors = data.get("colors")
        if isinstance(colors, dict):
            return colors
    colors = theme.get("colors")
    return colors if isinstance(colors, dict) else {}


def _normalize_chart_color(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    color = value.strip()
    if not color:
        return None
    hex_match = re.match(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$", color)
    if hex_match:
        raw = hex_match.group(1)
        if len(raw) == 3:
            raw = "".join(char + char for char in raw)
        return f"#{raw.upper()}"
    if re.match(r"^rgba?\([^)]+\)$", color, re.IGNORECASE):
        return color
    return color


def _looks_like_asset_reference(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(
        ("http://", "https://", "/app_data/", "/static/", "data:", "blob:")
    )


def _template_asset_url(value: Any) -> str | None:
    from utils.asset_directory_utils import normalize_slide_asset_url

    if isinstance(value, str):
        return normalize_slide_asset_url(value)
    if not isinstance(value, dict):
        return None

    fallback_url: str | None = None
    for key in (
        "data",
        "url",
        "image_url",
        "icon_url",
        "__image_url__",
        "__icon_url__",
    ):
        asset_url = value.get(key)
        if isinstance(asset_url, str) and asset_url.strip():
            normalized_url = normalize_slide_asset_url(asset_url)
            if _looks_like_asset_reference(normalized_url):
                return normalized_url
            if fallback_url is None:
                fallback_url = normalized_url
    return fallback_url

