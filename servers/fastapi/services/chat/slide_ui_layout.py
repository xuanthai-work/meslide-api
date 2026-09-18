"""Layout, group, and geometry helpers for Template V2 slide UI.

Extracted from ``services.chat.slide_ui_helpers``. The original module
re-exports these names for chat tools and memory-layer callers.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any

def _model_dict(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    return json.loads(json.dumps(value, ensure_ascii=False))


def _ungrouped_components_from_component(
    component: dict[str, Any],
    component_index: int,
    *,
    used_ids: set[str],
) -> list[dict[str, Any]]:
    component_box = _required_component_box(component)
    id_base = _normalize_component_id(
        str(
            component.get("id")
            or component.get("name")
            or component.get("description")
            or f"component_{component_index + 1}"
        )
    )
    entries: list[dict[str, Any]] = []
    elements = component.get("elements")
    if not isinstance(elements, list):
        return []

    valid_elements = [element for element in elements if isinstance(element, dict)]
    if len(valid_elements) == 1:
        element = valid_elements[0]
        box = _box_or_default(
            element,
            {
                "x": 0.0,
                "y": 0.0,
                "width": component_box["width"],
                "height": component_box["height"],
            },
        )
        entries.extend(
            _ungroup_element_one_level(
                element,
                {
                    "x": component_box["x"] + box["x"],
                    "y": component_box["y"] + box["y"],
                    "width": box["width"],
                    "height": box["height"],
                },
            )
        )
    else:
        for element in valid_elements:
            box = _box_or_default(
                element,
                {
                    "x": 0.0,
                    "y": 0.0,
                    "width": component_box["width"],
                    "height": component_box["height"],
                },
            )
            entries.append(
                {
                    "element": element,
                    "box": {
                        "x": component_box["x"] + box["x"],
                        "y": component_box["y"] + box["y"],
                        "width": box["width"],
                        "height": box["height"],
                    },
                }
            )

    parts: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        box = entry["box"]
        element = copy.deepcopy(entry["element"])
        _normalize_ungrouped_element_frame(element, box)
        component_id = _unique_component_id(f"{id_base}_part_{index + 1}", used_ids)
        parts.append(
            {
                "id": component_id,
                "description": "Ungrouped component element",
                "position": {"x": box["x"], "y": box["y"]},
                "elements": [element],
            }
        )
    return parts


def _ungroup_element_one_level(
    element: dict[str, Any],
    box: dict[str, float],
) -> list[dict[str, Any]]:
    children = _child_items(element)
    if not children:
        return []

    entries = []
    for child, child_box in _layout_child_boxes(element, children, box):
        entries.append(
            {
                "element": child,
                "box": {
                    "x": box["x"] + child_box["x"],
                    "y": box["y"] + child_box["y"],
                    "width": child_box["width"],
                    "height": child_box["height"],
                },
            }
        )
    return entries


def _child_items(element: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("children", "elements"):
        value = element.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    child = element.get("child")
    return [child] if isinstance(child, dict) else []


def _layout_child_boxes(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    parent_box: dict[str, float],
) -> list[tuple[dict[str, Any], dict[str, float]]]:
    parent_type = str(parent.get("type") or "")
    if parent_type in {"flex", "list-view"}:
        return _layout_flex_child_boxes(parent, children, parent_box)
    if parent_type in {"grid", "grid-view"}:
        return _layout_grid_child_boxes(parent, children, parent_box)

    padding = _padding(parent.get("padding")) if parent_type == "container" else {}
    content = {
        "x": float(padding.get("left", 0.0)),
        "y": float(padding.get("top", 0.0)),
        "width": max(
            1.0,
            parent_box["width"]
            - float(padding.get("left", 0.0))
            - float(padding.get("right", 0.0)),
        ),
        "height": max(
            1.0,
            parent_box["height"]
            - float(padding.get("top", 0.0))
            - float(padding.get("bottom", 0.0)),
        ),
    }
    return [
        (
            child,
            _box_or_default(
                child,
                {
                    "x": content["x"],
                    "y": content["y"],
                    "width": content["width"],
                    "height": content["height"],
                },
            ),
        )
        for child in children
    ]


def _layout_flex_child_boxes(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    parent_box: dict[str, float],
) -> list[tuple[dict[str, Any], dict[str, float]]]:
    padding = _padding(parent.get("padding"))
    direction = "row" if parent.get("direction") == "row" else "column"
    gap = _number(parent.get("gap")) or 0.0
    content_x = padding["left"]
    content_y = padding["top"]
    content_w = max(1.0, parent_box["width"] - padding["left"] - padding["right"])
    content_h = max(1.0, parent_box["height"] - padding["top"] - padding["bottom"])
    count = max(1, len(children))
    if direction == "row":
        width = max(1.0, (content_w - gap * (count - 1)) / count)
        return [
            (
                child,
                _box_or_default(
                    child,
                    {
                        "x": content_x + index * (width + gap),
                        "y": content_y,
                        "width": width,
                        "height": content_h,
                    },
                ),
            )
            for index, child in enumerate(children)
        ]
    height = max(1.0, (content_h - gap * (count - 1)) / count)
    return [
        (
            child,
            _box_or_default(
                child,
                {
                    "x": content_x,
                    "y": content_y + index * (height + gap),
                    "width": content_w,
                    "height": height,
                },
            ),
        )
        for index, child in enumerate(children)
    ]


def _layout_grid_child_boxes(
    parent: dict[str, Any],
    children: list[dict[str, Any]],
    parent_box: dict[str, float],
) -> list[tuple[dict[str, Any], dict[str, float]]]:
    padding = _padding(parent.get("padding"))
    gap = _number(parent.get("gap")) or 0.0
    column_gap = _number(parent.get("column_gap"))
    row_gap = _number(parent.get("row_gap"))
    column_gap = gap if column_gap is None else column_gap
    row_gap = gap if row_gap is None else row_gap
    column_count = _number(parent.get("columns"))
    if column_count is None and isinstance(parent.get("columns"), list):
        column_count = len(parent["columns"])
    columns = max(1, int(column_count or 1))
    rows = max(1, math.ceil(len(children) / columns))
    content_w = max(1.0, parent_box["width"] - padding["left"] - padding["right"])
    content_h = max(1.0, parent_box["height"] - padding["top"] - padding["bottom"])
    cell_w = max(1.0, (content_w - column_gap * (columns - 1)) / columns)
    cell_h = max(1.0, (content_h - row_gap * (rows - 1)) / rows)

    child_sizes = [_element_size_or_default(child, cell_w, cell_h) for child in children]
    column_widths = [cell_w for _ in range(columns)]
    row_heights = [cell_h for _ in range(rows)]
    for index, child_size in enumerate(child_sizes):
        column_index = index % columns
        row_index = index // columns
        column_widths[column_index] = max(column_widths[column_index], child_size["width"])
        row_heights[row_index] = max(row_heights[row_index], child_size["height"])

    column_offsets = [padding["left"]]
    for index in range(1, columns):
        column_offsets.append(column_offsets[index - 1] + column_widths[index - 1] + column_gap)
    row_offsets = [padding["top"]]
    for index in range(1, rows):
        row_offsets.append(row_offsets[index - 1] + row_heights[index - 1] + row_gap)

    return [
        (
            child,
            _box_or_default(
                child,
                {
                    "x": column_offsets[index % columns],
                    "y": row_offsets[index // columns],
                    "width": child_sizes[index]["width"],
                    "height": child_sizes[index]["height"],
                },
            ),
        )
        for index, child in enumerate(children)
    ]


def _element_size_or_default(
    element: dict[str, Any],
    default_width: float,
    default_height: float,
) -> dict[str, float]:
    size = element.get("size")
    width = _finite_number(size.get("width")) if isinstance(size, dict) else None
    height = _finite_number(size.get("height")) if isinstance(size, dict) else None
    return {
        "width": width if width is not None and width > 0 else default_width,
        "height": height if height is not None and height > 0 else default_height,
    }


def _required_component_box(value: dict[str, Any]) -> dict[str, float]:
    box = _component_box(value)
    if box is None:
        raise ValueError("Cannot safely ungroup component without position and elements.")
    return box


def _box_or_default(
    value: dict[str, Any],
    default: dict[str, float],
) -> dict[str, float]:
    vector_box = _vector_box(value)
    if vector_box is not None:
        return vector_box
    position = value.get("position")
    size = value.get("size")
    x = _finite_number(position.get("x")) if isinstance(position, dict) else None
    y = _finite_number(position.get("y")) if isinstance(position, dict) else None
    width = _finite_number(size.get("width")) if isinstance(size, dict) else None
    height = _finite_number(size.get("height")) if isinstance(size, dict) else None
    return {
        "x": x if x is not None else default["x"],
        "y": y if y is not None else default["y"],
        "width": width if width is not None and width > 0 else default["width"],
        "height": height if height is not None and height > 0 else default["height"],
    }


def _component_box(value: dict[str, Any]) -> dict[str, float] | None:
    position = value.get("position")
    if not isinstance(position, dict):
        return None
    x = _finite_number(position.get("x"))
    y = _finite_number(position.get("y"))
    content_size = _component_content_size(value)
    if x is None or y is None or content_size is None:
        return None
    return {
        "x": x,
        "y": y,
        "width": content_size["width"],
        "height": content_size["height"],
    }


def _component_content_size(value: dict[str, Any]) -> dict[str, float] | None:
    elements = value.get("elements")
    if not isinstance(elements, list):
        return None
    width = 1.0
    height = 1.0
    has_element = False
    for element in elements:
        if not isinstance(element, dict):
            continue
        box = _optional_box(element)
        if box is None:
            continue
        has_element = True
        width = max(width, box["x"] + box["width"])
        height = max(height, box["y"] + box["height"])
    if not has_element:
        return None
    return {"width": width, "height": height}


def _optional_box(value: dict[str, Any]) -> dict[str, float] | None:
    vector_box = _vector_box(value)
    if vector_box is not None:
        return vector_box
    position = value.get("position")
    size = value.get("size")
    if not isinstance(position, dict) or not isinstance(size, dict):
        return None
    x = _finite_number(position.get("x"))
    y = _finite_number(position.get("y"))
    width = _finite_number(size.get("width"))
    height = _finite_number(size.get("height"))
    if x is None or y is None or width is None or height is None:
        return None
    if width <= 0 or height <= 0:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def _normalize_ungrouped_element_frame(
    element: dict[str, Any],
    box: dict[str, float],
) -> None:
    if str(element.get("type") or "").lower() == "vector":
        vector_box = _vector_box(element)
        if vector_box is not None:
            _translate_vector_points(element, -vector_box["x"], -vector_box["y"])
        element.pop("size", None)
        element["position"] = {"x": 0, "y": 0}
        return

    element["position"] = {"x": 0, "y": 0}
    element["size"] = {"width": box["width"], "height": box["height"]}


def _vector_box(value: dict[str, Any]) -> dict[str, float] | None:
    if str(value.get("type") or "").lower() != "vector":
        return None
    points = value.get("points")
    if not isinstance(points, list):
        return None
    parsed: list[tuple[float, float]] = []
    for point in points:
        if not isinstance(point, dict):
            continue
        x = _finite_number(point.get("x"))
        y = _finite_number(point.get("y"))
        if x is not None and y is not None:
            parsed.append((x, y))
    if not parsed:
        return None
    min_x = min(point[0] for point in parsed)
    min_y = min(point[1] for point in parsed)
    max_x = max(point[0] for point in parsed)
    max_y = max(point[1] for point in parsed)
    stroke = value.get("stroke")
    stroke_width = 1.0
    if isinstance(stroke, dict):
        stroke_value = _finite_number(stroke.get("width"))
        if stroke_value is not None:
            stroke_width = max(0.0, stroke_value)
    return {
        "x": min_x,
        "y": min_y,
        "width": max(max_x - min_x, stroke_width, 1.0),
        "height": max(max_y - min_y, stroke_width, 1.0),
    }


def _translate_vector_points(element: dict[str, Any], dx: float, dy: float) -> None:
    points = element.get("points")
    if not isinstance(points, list):
        return
    next_points = []
    for point in points:
        if not isinstance(point, dict):
            next_points.append(point)
            continue
        x = _finite_number(point.get("x"))
        y = _finite_number(point.get("y"))
        if x is None or y is None:
            next_points.append(point)
            continue
        next_points.append({**point, "x": x + dx, "y": y + dy})
    element["points"] = next_points


def _finite_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    return None


def _number(value: Any) -> float | None:
    return _finite_number(value)


def _padding(value: Any) -> dict[str, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        return {"top": number, "right": number, "bottom": number, "left": number}
    if not isinstance(value, dict):
        return {"top": 0.0, "right": 0.0, "bottom": 0.0, "left": 0.0}
    x = _number(value.get("x")) or _number(value.get("horizontal")) or 0.0
    y = _number(value.get("y")) or _number(value.get("vertical")) or 0.0
    return {
        "top": _number(value.get("top")) or y,
        "right": _number(value.get("right")) or x,
        "bottom": _number(value.get("bottom")) or y,
        "left": _number(value.get("left")) or x,
    }


def _normalize_component_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized or "component"


def _unique_component_id(base: str, used_ids: set[str]) -> str:
    stem = base[:80].strip("_") or "component"
    candidate = stem
    suffix = 2
    while candidate in used_ids:
        suffix_text = f"_{suffix}"
        candidate = f"{stem[: 80 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    used_ids.add(candidate)
    return candidate


def _compact_components(layout_dict: dict[str, Any]) -> list[dict[str, Any]]:
    components: list[dict[str, Any]] = []
    for component in layout_dict.get("components", []):
        if not isinstance(component, dict):
            continue
        components.append(
            {
                "component_id": component.get("id"),
                "description": component.get("description"),
                "element_count": len(component.get("elements", []))
                if isinstance(component.get("elements"), list)
                else 0,
                "element_types": [
                    element.get("type")
                    for element in component.get("elements", [])
                    if isinstance(element, dict)
                ],
            }
        )
    return components
