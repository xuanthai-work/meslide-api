from __future__ import annotations

import copy
import json
import re
from typing import Any

from templates.v2.content import template_asset_prompt
from utils.latex_text import replace_text_runs, text_runs_to_tagged_text
from services.chat import slide_ui_layout as _slide_ui_layout
from services.chat.slide_ui_charts import (
    CHART_UPDATE_KEYS,
    _apply_chart_content_update,
    _looks_like_asset_reference,
    _normalize_generated_image_fit,
    _template_asset_url,
    _validate_infographic_element,
    _validate_vector_element,
)

_PATH_SEGMENT_RE = re.compile(r"^(?P<key>components|elements|children)\[(?P<index>\d+)\]$")
CONTENT_EDITABLE_ELEMENT_TYPES = {
    "text",
    "math",
    "text-list",
    "table",
    "image",
    "chart",
    "vector",
    "infographic",
}
VISIBLE_ELEMENT_TYPES = CONTENT_EDITABLE_ELEMENT_TYPES | {
    "container",
    "svg",
    "flex",
    "grid",
    "grid-view",
    "group",
}

_model_dict = _slide_ui_layout._model_dict
_ungrouped_components_from_component = (
    _slide_ui_layout._ungrouped_components_from_component
)
_ungroup_element_one_level = _slide_ui_layout._ungroup_element_one_level
_child_items = _slide_ui_layout._child_items
_layout_child_boxes = _slide_ui_layout._layout_child_boxes
_layout_flex_child_boxes = _slide_ui_layout._layout_flex_child_boxes
_layout_grid_child_boxes = _slide_ui_layout._layout_grid_child_boxes
_element_size_or_default = _slide_ui_layout._element_size_or_default
_required_component_box = _slide_ui_layout._required_component_box
_box_or_default = _slide_ui_layout._box_or_default
_component_box = _slide_ui_layout._component_box
_component_content_size = _slide_ui_layout._component_content_size
_optional_box = _slide_ui_layout._optional_box
_normalize_ungrouped_element_frame = (
    _slide_ui_layout._normalize_ungrouped_element_frame
)
_vector_box = _slide_ui_layout._vector_box
_translate_vector_points = _slide_ui_layout._translate_vector_points
_finite_number = _slide_ui_layout._finite_number
_number = _slide_ui_layout._number
_padding = _slide_ui_layout._padding
_normalize_component_id = _slide_ui_layout._normalize_component_id
_unique_component_id = _slide_ui_layout._unique_component_id
_compact_components = _slide_ui_layout._compact_components




def _collect_editable_elements(
    layout_dict: dict[str, Any],
    *,
    include_visual_elements: bool = False,
) -> list[dict[str, Any]]:
    editable: list[dict[str, Any]] = []
    root_elements = layout_dict.get("elements")
    if isinstance(root_elements, list):
        for element_index, element in enumerate(root_elements):
            if isinstance(element, dict):
                _visit_editable_element(
                    element=element,
                    path=f"elements[{element_index}]",
                    component_id="",
                    editable=editable,
                    include_visual_elements=include_visual_elements,
                )

    components = layout_dict.get("components", [])
    if not isinstance(components, list):
        return editable

    for component_index, component in enumerate(components):
        if not isinstance(component, dict):
            continue
        component_id = str(component.get("id") or "")
        elements = component.get("elements", [])
        if not isinstance(elements, list):
            continue
        for element_index, element in enumerate(elements):
            if isinstance(element, dict):
                _visit_editable_element(
                    element=element,
                    path=f"components[{component_index}].elements[{element_index}]",
                    component_id=component_id,
                    editable=editable,
                    include_visual_elements=include_visual_elements,
                )
    return editable


def _visit_editable_element(
    *,
    element: dict[str, Any],
    path: str,
    component_id: str,
    editable: list[dict[str, Any]],
    include_visual_elements: bool,
) -> None:
    element_type = str(element.get("type") or "")
    is_content_editable = element_type in CONTENT_EDITABLE_ELEMENT_TYPES
    if is_content_editable or (
        include_visual_elements and element_type in VISIBLE_ELEMENT_TYPES
    ):
        editable.append(
            {
                "path": path,
                "component_id": component_id,
                "type": element_type,
                "name": element.get("name"),
                "decorative": element.get("decorative"),
                "content_editable": is_content_editable,
                "geometry_editable": True,
                "content": _element_content(element),
                "style": _element_style(element),
                "limits": _element_limits(element),
            }
        )

    child = element.get("child")
    if isinstance(child, dict):
        _visit_editable_element(
            element=child,
            path=f"{path}.child",
            component_id=component_id,
            editable=editable,
            include_visual_elements=include_visual_elements,
        )

    children = element.get("children")
    if isinstance(children, list):
        for index, nested in enumerate(children):
            if isinstance(nested, dict):
                _visit_editable_element(
                    element=nested,
                    path=f"{path}.children[{index}]",
                    component_id=component_id,
                    editable=editable,
                    include_visual_elements=include_visual_elements,
                )


def _element_limits(element: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "max_length",
        "min_length",
        "max_items",
        "min_items",
        "max_item_length",
        "min_item_length",
        "max_columns",
        "min_columns",
        "max_rows",
        "min_rows",
        "max_children",
        "min_children",
        "max_value",
        "min_value",
    )
    return {key: element[key] for key in keys if key in element}


def _element_content(element: dict[str, Any]) -> Any:
    element_type = element.get("type")
    if element_type == "text":
        return {"text": _runs_text(element.get("runs"))}
    if element_type == "math":
        return {
            "latex": element.get("latex"),
            "display_mode": element.get("display_mode", True),
        }
    if element_type == "text-list":
        items = element.get("items") if isinstance(element.get("items"), list) else []
        return {"items": [_runs_text(item) for item in items]}
    if element_type == "table":
        columns = element.get("columns")
        if not isinstance(columns, list) and isinstance(element.get("headers"), list):
            columns = element["headers"]
        if not isinstance(columns, list):
            columns = []
        return {
            "columns": [_table_value_text(cell) for cell in columns],
            "rows": [
                [_table_value_text(cell) for cell in row]
                for row in element.get("rows", [])
                if isinstance(row, list)
            ],
        }
    if element_type == "chart":
        return {
            "chart_type": element.get("chart_type"),
            "title": element.get("title"),
            "title_color": element.get("title_color"),
            "legend_color": element.get("legend_color"),
            "categories": element.get("categories"),
            "series": element.get("series"),
            "colors": element.get("colors"),
            "axis_color": element.get("axis_color"),
            "grid_color": element.get("grid_color"),
            "x_axis": element.get("x_axis"),
            "y_axis": element.get("y_axis"),
            "x_axis_grid": element.get("x_axis_grid"),
            "y_axis_grid": element.get("y_axis_grid"),
            "x_axis_title": element.get("x_axis_title"),
            "y_axis_title": element.get("y_axis_title"),
            "data_labels": element.get("data_labels"),
            "legend": element.get("legend"),
        }
    if element_type == "image":
        return {
            "data": element.get("data"),
            "is_icon": element.get("is_icon"),
        }
    if element_type == "vector":
        return {
            "shape": element.get("shape") or "polygon",
            "points": copy.deepcopy(element.get("points")),
            "closed": element.get("closed"),
            "curve": copy.deepcopy(element.get("curve")),
            "corner_radii": copy.deepcopy(element.get("corner_radii")),
        }
    if element_type == "infographic":
        return {
            "data": copy.deepcopy(element.get("data")),
            "colors": copy.deepcopy(element.get("colors")),
        }
    if element_type == "svg":
        svg = element.get("svg")
        return {
            "svg_length": len(svg) if isinstance(svg, str) else 0,
        }
    return None


def _element_style(element: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "font",
        "alignment",
        "fill",
        "stroke",
        "color",
        "opacity",
        "shadow",
        "border_radius",
        "borderRadius",
        "padding",
        "marker",
        "fit",
        "focus_x",
        "focus_y",
        "crop_scale",
    )
    style = {key: copy.deepcopy(element[key]) for key in keys if key in element}
    base_font = style.get("font") if isinstance(style.get("font"), dict) else {}
    run_font = _first_text_font(element) or {}
    if base_font or run_font:
        style["font"] = {**copy.deepcopy(base_font), **run_font}
    return style


def _first_text_font(element: dict[str, Any]) -> dict[str, Any] | None:
    element_type = element.get("type")
    if element_type == "text":
        return _first_run_font(element.get("runs"))
    if element_type == "text-list":
        items = element.get("items")
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, list):
                font = _first_run_font(item)
            elif isinstance(item, dict):
                font = (
                    copy.deepcopy(item["font"])
                    if isinstance(item.get("font"), dict)
                    else _first_run_font(item.get("runs"))
                )
            else:
                font = None
            if font:
                return font
    if element_type == "table":
        columns = element.get("columns")
        if isinstance(columns, list):
            for cell in columns:
                font = _table_cell_font(cell)
                if font:
                    return font
        rows = element.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, list):
                    continue
                for cell in row:
                    font = _table_cell_font(cell)
                    if font:
                        return font
    return None


def _table_cell_font(cell: Any) -> dict[str, Any] | None:
    if not isinstance(cell, dict):
        return None
    if isinstance(cell.get("font"), dict):
        return copy.deepcopy(cell["font"])
    return _first_run_font(cell.get("runs"))


def _first_run_font(runs: Any) -> dict[str, Any] | None:
    if not isinstance(runs, list):
        return None
    for run in runs:
        if isinstance(run, dict) and isinstance(run.get("font"), dict):
            return copy.deepcopy(run["font"])
    return None


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _runs_text(value: Any) -> str:
    return text_runs_to_tagged_text(value)


def _searchable_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except Exception:
        return str(content)


def _snippet(text: str, query: str, limit: int = 220) -> str:
    normalized = text.replace("\n", " ")
    index = normalized.casefold().find(query.casefold())
    if index < 0:
        return normalized[:limit]
    start = max(0, index - 70)
    end = min(len(normalized), index + len(query) + 140)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(normalized) else ""
    return f"{prefix}{normalized[start:end]}{suffix}"


def _resolve_element_path(layout_dict: dict[str, Any], path: str) -> dict[str, Any]:
    current: Any = layout_dict
    for segment in path.split("."):
        if segment == "child":
            if not isinstance(current, dict) or not isinstance(current.get("child"), dict):
                raise ValueError(f"Invalid element path segment: {segment}")
            current = current["child"]
            continue

        match = _PATH_SEGMENT_RE.match(segment)
        if not match:
            raise ValueError(f"Invalid element path segment: {segment}")
        key = match.group("key")
        index = int(match.group("index"))
        if not isinstance(current, dict) or not isinstance(current.get(key), list):
            raise ValueError(f"Invalid element path segment: {segment}")
        values = current[key]
        if index >= len(values) or not isinstance(values[index], dict):
            raise ValueError(f"Invalid element path index: {segment}")
        current = values[index]

    if not isinstance(current, dict) or not isinstance(current.get("type"), str):
        raise ValueError("Path does not resolve to an element.")
    return current


def _resolve_layout_item_parent(
    layout_dict: dict[str, Any],
    path: str,
) -> tuple[list[Any], int]:
    current: Any = layout_dict
    segments = path.split(".")
    if not segments:
        raise ValueError("Layout item path is required.")

    for segment in segments[:-1]:
        if segment == "child":
            if not isinstance(current, dict) or not isinstance(current.get("child"), dict):
                raise ValueError(f"Invalid layout item path segment: {segment}")
            current = current["child"]
            continue

        match = _PATH_SEGMENT_RE.match(segment)
        if not match:
            raise ValueError(f"Invalid layout item path segment: {segment}")
        key = match.group("key")
        index = int(match.group("index"))
        if not isinstance(current, dict) or not isinstance(current.get(key), list):
            raise ValueError(f"Invalid layout item path segment: {segment}")
        values = current[key]
        if index >= len(values) or not isinstance(values[index], dict):
            raise ValueError(f"Invalid layout item path index: {segment}")
        current = values[index]

    match = _PATH_SEGMENT_RE.match(segments[-1])
    if not match:
        raise ValueError(f"Invalid layout item path segment: {segments[-1]}")
    key = match.group("key")
    index = int(match.group("index"))
    if not isinstance(current, dict) or not isinstance(current.get(key), list):
        raise ValueError(f"Invalid layout item path segment: {segments[-1]}")
    values = current[key]
    if index >= len(values):
        raise ValueError(f"Invalid layout item path index: {segments[-1]}")
    return values, index


def _preserve_slot_fields(
    target: dict[str, Any],
    slot: dict[str, Any],
    *,
    keep_id: bool,
) -> None:
    for key in ("position", "size", "rotation"):
        if key in slot:
            target[key] = copy.deepcopy(slot[key])
        else:
            target.pop(key, None)
    if keep_id:
        target["id"] = slot.get("id")


def _is_top_level_component_path(path: str) -> bool:
    match = _PATH_SEGMENT_RE.match(path)
    return bool(
        match
        and match.group("key") == "components"
        and match.group(0) == path
    )


def _component_id_for_path(layout_dict: dict[str, Any], path: str) -> str | None:
    first = path.split(".", 1)[0]
    match = _PATH_SEGMENT_RE.match(first)
    if not match or match.group("key") != "components":
        return None
    index = int(match.group("index"))
    components = layout_dict.get("components")
    if not isinstance(components, list) or index >= len(components):
        return None
    component = components[index]
    return str(component.get("id")) if isinstance(component, dict) else None


def _looks_like_chart_request(value: str) -> bool:
    if _looks_like_asset_reference(value):
        return False
    normalized = " ".join(value.casefold().split())
    if not normalized:
        return False
    chart_phrases = (
        "pie chart",
        "bar chart",
        "line chart",
        "area chart",
        "donut chart",
        "horizontal bar",
        "horizontal stack bar",
        "horizontal stacked bar",
        "stacked bar",
        "radial chart",
        "chart with",
        "chart showing",
        "dummy metrics",
        "dummy chart",
    )
    if any(phrase in normalized for phrase in chart_phrases):
        return True
    if "chart" not in normalized:
        return False
    return any(
        token in normalized
        for token in ("pie", "bar", "line", "donut", "metrics", "graph", "visualization")
    )


def _chart_request_on_image_error() -> ValueError:
    return ValueError(
        "Chart requests must use a chart element, not an image. If the target is an "
        "image/icon, delete that component and addComponent with type chart "
        "(chart_type bar|horizontal_bar|stacked_bar|line|area|pie|donut|"
        "scatter|bubble|radar|polar_area, title, categories, series with values, "
        "and optional colors). If the target is already chart, use updateElement "
        "with chart."
    )


def _chart_update_has_content(chart: dict[str, Any] | None) -> bool:
    if chart is None:
        return False
    return any(key in chart and chart.get(key) is not None for key in CHART_UPDATE_KEYS)


def _structured_update_has_content(value: dict[str, Any] | None) -> bool:
    return isinstance(value, dict) and bool(value)


def _resolve_image_update_payload(
    text: str | None,
    items: list[str] | None,
) -> str | dict[str, Any] | None:
    if text is not None:
        stripped = text.strip()
        if not stripped:
            return None
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
        return text
    if isinstance(items, list) and len(items) == 1:
        candidate = str(items[0] or "").strip()
        if candidate:
            return candidate
    return None


def _apply_image_element_value(element: dict[str, Any], value: Any) -> None:
    asset_url = _template_asset_url(value)
    if not asset_url:
        raise ValueError(
            "Image/icon updates require `text` with an image or icon URL."
        )
    element["data"] = asset_url
    _normalize_generated_image_fit(element, asset_url)
    prompt = template_asset_prompt(
        value,
        is_icon=element.get("is_icon") is True,
    )
    if prompt:
        element["prompt"] = prompt


def _apply_image_element_update(
    element: dict[str, Any],
    *,
    text: str | None,
    items: list[str] | None,
) -> None:
    payload = _resolve_image_update_payload(text, items)
    if payload is None:
        raise ValueError(
            "Image/icon updates require `text` with an image or icon URL."
        )
    if isinstance(payload, str) and _looks_like_chart_request(payload):
        raise _chart_request_on_image_error()
    _apply_image_element_value(element, payload)


def _content_update_requested_for_type(
    element_type: str,
    *,
    text: str | None,
    items: list[str] | None,
    table_cell: dict[str, Any] | None,
    table: dict[str, Any] | None,
    chart: dict[str, Any] | None,
    vector: dict[str, Any] | None,
    infographic: dict[str, Any] | None,
) -> bool:
    if element_type in {"text", "math"}:
        return text is not None
    if element_type == "text-list":
        return items is not None
    if element_type == "table":
        return table is not None or table_cell is not None
    if element_type == "chart":
        return _chart_update_has_content(chart)
    if element_type == "vector":
        return _structured_update_has_content(vector)
    if element_type == "infographic":
        return _structured_update_has_content(infographic)
    if element_type == "image":
        return _resolve_image_update_payload(text, items) is not None
    return any(
        value is not None
        for value in (text, items, table_cell, table, chart, vector, infographic)
    )


def _update_vector_element(element: dict[str, Any], vector: dict[str, Any]) -> None:
    for key in ("shape", "points", "closed", "curve", "corner_radii"):
        if key in vector:
            element[key] = copy.deepcopy(vector[key])
    _validate_vector_element(element)


def _update_infographic_element(
    element: dict[str, Any], infographic: dict[str, Any]
) -> None:
    if "data" in infographic:
        current_data = element.get("data")
        element["data"] = {
            **(copy.deepcopy(current_data) if isinstance(current_data, dict) else {}),
            **copy.deepcopy(infographic["data"]),
        }
    if "colors" in infographic:
        element["colors"] = copy.deepcopy(infographic["colors"])
    _validate_infographic_element(element)


def _update_text_element(element: dict[str, Any], text: str) -> None:
    _validate_text_length(
        text,
        min_length=element.get("min_length"),
        max_length=element.get("max_length"),
        label=str(element.get("name") or "text"),
    )
    element["runs"] = _replacement_runs(
        existing_runs=element.get("runs"),
        text=text,
        fallback_font=element.get("font"),
    )
    if any(run.get("type") == "latex" for run in element["runs"]):
        element.pop("text", None)
    else:
        # The Konva renderer reads flattened plain text in preference to runs.
        element["text"] = text


def _update_text_list_element(element: dict[str, Any], items: list[str]) -> None:
    min_items = _int_or_none(element.get("min_items"))
    max_items = _int_or_none(element.get("max_items"))
    if min_items is not None and len(items) < min_items:
        raise ValueError(f"Text list requires at least {min_items} item(s).")
    if max_items is not None and len(items) > max_items:
        raise ValueError(f"Text list allows at most {max_items} item(s).")

    for index, item in enumerate(items):
        _validate_text_length(
            item,
            min_length=element.get("min_item_length"),
            max_length=element.get("max_item_length"),
            label=f"list item {index + 1}",
        )

    source_items = element.get("items") if isinstance(element.get("items"), list) else []
    element["items"] = [
        _replacement_runs(
            existing_runs=source_items[index] if index < len(source_items) else None,
            text=item,
            fallback_font=element.get("font"),
        )
        for index, item in enumerate(items)
    ]


def _update_table_cell(element: dict[str, Any], table_cell: dict[str, Any]) -> None:
    section = table_cell["section"]
    column_index = int(table_cell["column_index"])
    text = str(table_cell.get("text") or "")
    if section == "columns":
        columns = element.get("columns")
        if not isinstance(columns, list) or column_index >= len(columns):
            raise ValueError("Invalid table column index.")
        target = columns[column_index]
    else:
        row_index = table_cell.get("row_index")
        rows = element.get("rows")
        if not isinstance(row_index, int) or not isinstance(rows, list) or row_index >= len(rows):
            raise ValueError("Invalid table row index.")
        row = rows[row_index]
        if not isinstance(row, list) or column_index >= len(row):
            raise ValueError("Invalid table cell column index.")
        target = row[column_index]

    if not isinstance(target, dict):
        raise ValueError("Target table cell is invalid.")
    target["runs"] = _replacement_runs(
        existing_runs=target.get("runs"),
        text=text,
        fallback_font=target.get("font"),
    )


def _update_table_element(element: dict[str, Any], table: dict[str, Any]) -> None:
    columns = table.get("columns") or table.get("headers")
    rows = table.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        raise ValueError("table update requires columns/headers and rows.")
    if not all(isinstance(row, list) for row in rows):
        raise ValueError("table rows must be lists.")

    column_count = len(columns)
    if column_count == 0:
        raise ValueError("table must contain at least one column.")
    if any(len(row) != column_count for row in rows):
        raise ValueError("each table row must match the column count.")

    min_columns = int(element.get("min_columns") or 0)
    max_columns = int(element.get("max_columns") or max(column_count, 100))
    min_rows = int(element.get("min_rows") or 0)
    max_rows = int(element.get("max_rows") or max(len(rows), 100))
    if column_count < min_columns or column_count > max_columns:
        raise ValueError("table column count is outside this element's limits.")
    if len(rows) < min_rows or len(rows) > max_rows:
        raise ValueError("table row count is outside this element's limits.")

    existing_columns = _dicts(element.get("columns"))
    existing_rows = [
        _dicts(row) for row in element.get("rows", []) if isinstance(row, list)
    ]

    element["columns"] = [
        _replacement_table_cell(
            value=value,
            existing=existing_columns[index] if index < len(existing_columns) else None,
        )
        for index, value in enumerate(columns)
    ]
    element["rows"] = [
        [
            _replacement_table_cell(
                value=value,
                existing=(
                    existing_rows[row_index][column_index]
                    if row_index < len(existing_rows)
                    and column_index < len(existing_rows[row_index])
                    else None
                ),
            )
            for column_index, value in enumerate(row)
        ]
        for row_index, row in enumerate(rows)
    ]


def _replacement_table_cell(value: Any, existing: dict[str, Any] | None) -> dict[str, Any]:
    cell = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    cell["runs"] = _replacement_runs(
        existing_runs=cell.get("runs"),
        text=_table_value_text(value),
        fallback_font=cell.get("font"),
    )
    return cell


def _table_value_text(value: Any) -> str:
    if isinstance(value, dict):
        runs = value.get("runs")
        if isinstance(runs, list):
            text = _runs_text(runs)
            if text:
                return text
        for key in ("text", "content", "value", "label", "data"):
            if key in value:
                return _table_value_text(value[key])
        return ""
    if isinstance(value, list):
        return "".join(_table_value_text(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _update_chart_element(
    element: dict[str, Any],
    chart: dict[str, Any],
    theme: dict[str, Any] | None = None,
) -> None:
    _apply_chart_content_update(element, chart, theme)

    categories = element.get("categories")
    series = element.get("series")
    if isinstance(categories, list) and isinstance(series, list):
        category_count = len(categories)
        for item in series:
            if not isinstance(item, dict):
                continue
            values = item.get("values")
            if isinstance(values, list) and len(values) != category_count:
                raise ValueError("Each chart series must match the category count.")


TEXT_STYLE_ELEMENT_TYPES = {"text", "math", "text-list", "table"}


def _apply_element_style_patch(
    element: dict[str, Any],
    patch: dict[str, Any],
) -> None:
    element_type = str(element.get("type") or "")
    if "color" in patch:
        _apply_direct_color_patch(element, element_type, patch.get("color"), patch)

    font_patch = _text_font_patch_from_element_patch(element_type, patch)
    if not font_patch:
        return

    if element_type == "text":
        _apply_font_patch_to_text_element(element, font_patch)
    elif element_type == "math":
        _merge_font_patch(element, font_patch)
    elif element_type == "text-list":
        _apply_font_patch_to_text_list_element(element, font_patch)
    elif element_type == "table":
        _apply_font_patch_to_table_element(element, font_patch)


def _apply_direct_color_patch(
    element: dict[str, Any],
    element_type: str,
    value: Any,
    patch: dict[str, Any],
) -> None:
    if not isinstance(value, str) or not value.strip():
        return
    if element_type in TEXT_STYLE_ELEMENT_TYPES:
        font = element.get("font") if isinstance(element.get("font"), dict) else {}
        element["font"] = {**font, "color": value}
        return
    if element_type == "vector":
        points = element.get("points") if isinstance(element.get("points"), list) else []
        explicit_closed = element.get("closed")
        is_closed = element.get("shape") == "ellipse" or (
            explicit_closed if isinstance(explicit_closed, bool) else len(points) > 2
        )
        style_key = "fill" if is_closed else "stroke"
        style = element.get(style_key) if isinstance(element.get(style_key), dict) else {}
        element[style_key] = {**style, "color": value}
        return
    if element_type == "infographic":
        colors = element.get("colors") if isinstance(element.get("colors"), list) else []
        element["colors"] = [*(colors[:1] or ["#E5E7EB"]), value, *colors[2:]]
        return
    if "fill" not in patch and element_type in {
        "container",
        "flex",
        "grid",
        "grid-view",
        "group",
    }:
        fill = element.get("fill") if isinstance(element.get("fill"), dict) else {}
        element["fill"] = {**fill, "color": value}


def _text_font_patch_from_element_patch(
    element_type: str,
    patch: dict[str, Any],
) -> dict[str, Any]:
    if element_type not in TEXT_STYLE_ELEMENT_TYPES:
        return {}

    font_patch: dict[str, Any] = {}
    raw_font = patch.get("font")
    if isinstance(raw_font, dict):
        font_patch.update(_normalize_font_patch(raw_font))
    direct_color = patch.get("color")
    if isinstance(direct_color, str) and direct_color.strip():
        font_patch["color"] = direct_color
    return {
        key: copy.deepcopy(value)
        for key, value in font_patch.items()
        if value is not None
    }


def _normalize_font_patch(value: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    aliases = {
        "family": "family",
        "fontFamily": "family",
        "font_family": "family",
        "fontName": "family",
        "font_name": "family",
        "name": "family",
        "size": "size",
        "fontSize": "size",
        "font_size": "size",
        "color": "color",
        "fontColor": "color",
        "font_color": "color",
        "textColor": "color",
        "text_color": "color",
        "bold": "bold",
        "italic": "italic",
        "underline": "underline",
        "line_height": "line_height",
        "lineHeight": "line_height",
        "letter_spacing": "letter_spacing",
        "letterSpacing": "letter_spacing",
        "wrap": "wrap",
        "ellipsis": "ellipsis",
        "opacity": "opacity",
    }
    for source_key, target_key in aliases.items():
        if source_key in value:
            normalized[target_key] = value[source_key]
    return normalized


def _apply_font_patch_to_text_element(
    element: dict[str, Any],
    font_patch: dict[str, Any],
) -> None:
    _merge_font_patch(element, font_patch)
    runs = element.get("runs")
    if not isinstance(runs, list) or not runs:
        text = str(element.get("text") or "")
        element["runs"] = [{"text": text, "font": copy.deepcopy(element["font"])}]
        return
    for run in runs:
        if isinstance(run, dict):
            _merge_font_patch(run, font_patch)


def _apply_font_patch_to_text_list_element(
    element: dict[str, Any],
    font_patch: dict[str, Any],
) -> None:
    _merge_font_patch(element, font_patch)
    items = element.get("items")
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if isinstance(item, list):
            for run in item:
                if isinstance(run, dict):
                    _merge_font_patch(run, font_patch)
        elif isinstance(item, dict):
            _merge_font_patch(item, font_patch)
            runs = item.get("runs")
            if isinstance(runs, list):
                for run in runs:
                    if isinstance(run, dict):
                        _merge_font_patch(run, font_patch)
        elif isinstance(item, str):
            items[index] = [{"text": item, "font": copy.deepcopy(element["font"])}]


def _apply_font_patch_to_table_element(
    element: dict[str, Any],
    font_patch: dict[str, Any],
) -> None:
    _merge_font_patch(element, font_patch)
    columns = element.get("columns")
    if isinstance(columns, list):
        for cell in columns:
            _apply_font_patch_to_table_cell(cell, font_patch)
    rows = element.get("rows")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                for cell in row:
                    _apply_font_patch_to_table_cell(cell, font_patch)


def _apply_font_patch_to_table_cell(
    cell: Any,
    font_patch: dict[str, Any],
) -> None:
    if not isinstance(cell, dict):
        return
    _merge_font_patch(cell, font_patch)
    runs = cell.get("runs")
    if not isinstance(runs, list) or not runs:
        return
    for run in runs:
        if isinstance(run, dict):
            _merge_font_patch(run, font_patch)


def _merge_font_patch(target: dict[str, Any], font_patch: dict[str, Any]) -> None:
    source = target.get("font") if isinstance(target.get("font"), dict) else {}
    target["font"] = {
        **source,
        **copy.deepcopy(font_patch),
    }


def _replacement_runs(
    *,
    existing_runs: Any,
    text: str,
    fallback_font: Any,
) -> list[dict[str, Any]]:
    return replace_text_runs(existing_runs, text, fallback_font)


def _validate_text_length(
    text: str,
    *,
    min_length: Any,
    max_length: Any,
    label: str,
) -> None:
    min_value = _int_or_none(min_length)
    max_value = _int_or_none(max_length)
    if min_value is not None and len(text) < min_value:
        raise ValueError(f"{label} must be at least {min_value} character(s).")
    if max_value is not None and len(text) > max_value:
        raise ValueError(f"{label} must be at most {max_value} character(s).")


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) else None
