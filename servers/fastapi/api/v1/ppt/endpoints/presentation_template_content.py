"""Template slide-UI construction and content application helpers.

Extracted from ``api.v1.ppt.endpoints.presentation`` so the router module only
orchestrates; the functions here are pure transformations over template
layout payloads and generated slide content. Behavior is pinned by
``tests/unit/test_presentation_template_content.py`` and the template-V2 UI
test suite that exercises them through the endpoint re-export.
"""

import copy
from typing import Any, Optional
from services.chat.slide_ui_charts import _normalize_generated_image_fit
from utils.dict_utils import to_plain_data

from utils.latex_text import parse_latex_tags, replace_text_runs
from templates.v2.content import (
    hydrate_repeated_top_level_groups,
    lookup_template_content,
    read_template_text,
    repeated_content_keys_for_name,
    template_asset_prompt,
)

def _is_template_layout_payload(layout_payload: Any) -> bool:
    return (
        isinstance(layout_payload, dict)
        and isinstance(layout_payload.get("layouts"), list)
    )


def _template_slide_ui(
    layout_payload: Any,
    layout_id: str,
) -> Optional[dict[str, Any]]:
    if not _is_template_layout_payload(layout_payload):
        return None

    for layout in layout_payload["layouts"]:
        if isinstance(layout, dict) and str(layout.get("id")) == str(layout_id):
            return copy.deepcopy(layout)
    return None


GENERATED_VALUE_ELEMENT_TYPES = {
    "text",
    "math",
    "image",
    "text-list",
    "table",
    "chart",
}
GENERATED_TABLE_TEXT_FONT = {
    "family": "Sniglet",
    "size": 12,
    "color": "#082314",
}
GENERATED_TABLE_HEADER_FONT = {
    **GENERATED_TABLE_TEXT_FONT,
    "bold": True,
}
GENERATED_TABLE_CELL_FILL = {
    "color": "#F8F4E9",
    "opacity": 1,
}
GENERATED_TABLE_CELL_STROKE = {
    "color": "#D8D3C4",
    "opacity": 1,
    "width": 1,
}
TEMPLATE_STRONG_MARKDOWN_DELIMITERS = ("**", "__")
TEMPLATE_EMPHASIS_MARKDOWN_DELIMITERS = ("*", "_")
TEMPLATE_MARKDOWN_DELIMITERS = (
    *TEMPLATE_STRONG_MARKDOWN_DELIMITERS,
    *TEMPLATE_EMPHASIS_MARKDOWN_DELIMITERS,
)


def _template_component_content_keys(components: list[Any]) -> list[str]:
    ids: list[str] = []
    for index, component in enumerate(components):
        component_id = (
            component.get("id")
            if isinstance(component, dict) and isinstance(component.get("id"), str)
            else None
        )
        ids.append(component_id or f"component_{index}")

    counts: dict[str, int] = {}
    for component_id in ids:
        counts[component_id] = counts.get(component_id, 0) + 1

    indexes: dict[str, int] = {}
    used: set[str] = set()
    keys: list[str] = []
    for component_id in ids:
        occurrence_index = indexes.get(component_id, 0)
        indexes[component_id] = occurrence_index + 1
        base = (
            f"{component_id}_{occurrence_index}"
            if counts.get(component_id, 0) > 1
            else component_id
        )

        key = base
        suffix = 1
        while key in used:
            key = f"{base}_{suffix}"
            suffix += 1
        used.add(key)
        keys.append(key)

    return keys


def _apply_template_content_to_ui(
    ui: Optional[dict[str, Any]],
    content: dict[str, Any],
) -> Optional[dict[str, Any]]:
    if not isinstance(ui, dict):
        return ui

    components = ui.get("components")
    if not isinstance(components, list) or not components:
        return ui

    component_keys = _template_component_content_keys(components)
    hydrated_ui = to_plain_data(ui)
    if isinstance(content, dict):
        content = to_plain_data(content)
    hydrated_components = hydrated_ui.get("components")
    if not isinstance(hydrated_components, list):
        return hydrated_ui

    for index, component in enumerate(hydrated_components):
        if not isinstance(component, dict):
            continue

        component_id = component.get("id")
        component_content = content.get(component_keys[index])
        if not isinstance(component_content, dict) and isinstance(component_id, str):
            component_content = content.get(component_id)
        if not isinstance(component_content, dict):
            component_content = {}

        elements = component.get("elements")
        if isinstance(elements, list):
            repeated_groups = hydrate_repeated_top_level_groups(
                elements,
                component_content,
                apply_item=lambda element, item: _apply_template_content_to_element(
                    element,
                    item,
                    direct_value=True,
                ),
            )
            component["elements"] = (
                repeated_groups
                if repeated_groups is not None
                else _apply_template_content_to_element_list(
                    elements,
                    component_content,
                )
            )

    return hydrated_ui


def _apply_template_content_to_element(
    element: Any,
    content: Any,
    *,
    direct_value: bool = False,
    preferred_content_keys: list[str] | None = None,
    name_occurrences: dict[str, int] | None = None,
) -> Any:
    if not isinstance(element, dict):
        return element

    content_values = content if isinstance(content, dict) else {}
    element_type = element.get("type")
    name = element.get("name") if isinstance(element.get("name"), str) else None
    has_value = False
    value = None
    if name:
        if preferred_content_keys is None and name_occurrences is not None:
            preferred_content_keys = repeated_content_keys_for_name(
                name,
                content_values,
                name_occurrences,
            )
        has_value, value = lookup_template_content(
            content_values,
            name,
            preferred_keys=preferred_content_keys,
        )

    if (
        element.get("decorative") is False
        and name
        and has_value
        and element_type in GENERATED_VALUE_ELEMENT_TYPES
    ):
        return _apply_template_content_value(element, value)

    # Repeated flex/grid schemas omit the child-name wrapper when an item is a
    # direct generated value. For example, three image children are emitted as
    # [{"image_prompt": ..., "image_url": ...}, ...], not as
    # [{"gallery_photo": {...}}, ...]. In that case the array item itself is
    # the value for the child element.
    if (
        direct_value
        and not has_value
        and element.get("decorative") is False
        and element_type in GENERATED_VALUE_ELEMENT_TYPES
    ):
        return _apply_template_content_value(element, content)

    nested_content = value if isinstance(value, dict) else content_values
    nested_direct_value = direct_value and not has_value
    nested_name_occurrences = (
        {}
        if has_value and isinstance(value, dict)
        else name_occurrences
    )

    if element_type == "container":
        updated = copy.deepcopy(element)
        updated["child"] = _apply_template_content_to_element(
            element.get("child"),
            nested_content,
            direct_value=nested_direct_value,
            name_occurrences=nested_name_occurrences,
        )
        return updated

    if element_type in {"flex", "grid", "group"}:
        updated = copy.deepcopy(element)
        children = element.get("children")
        if not isinstance(children, list):
            children = []
        updated["children"] = _apply_template_content_to_children(
            children,
            value,
            nested_content,
            direct_value=nested_direct_value,
            name_occurrences=nested_name_occurrences,
        )
        return updated

    return copy.deepcopy(element)


def _apply_template_content_to_children(
    children: list[Any],
    value: Any,
    content: Any,
    *,
    direct_value: bool = False,
    name_occurrences: dict[str, int] | None = None,
) -> list[Any]:
    if isinstance(value, list) and children:
        return [
            _apply_template_content_to_element(
                children[min(index, len(children) - 1)],
                item,
                direct_value=True,
            )
            for index, item in enumerate(value)
        ]

    return _apply_template_content_to_element_list(
        children,
        content,
        direct_value=direct_value,
        name_occurrences=name_occurrences,
    )


def _apply_template_content_to_element_list(
    elements: list[Any],
    content: Any,
    *,
    direct_value: bool = False,
    name_occurrences: dict[str, int] | None = None,
) -> list[Any]:
    scoped_name_occurrences = name_occurrences if name_occurrences is not None else {}
    hydrated_elements: list[Any] = []
    for element in elements:
        hydrated_elements.append(
            _apply_template_content_to_element(
                element,
                content,
                direct_value=direct_value,
                name_occurrences=scoped_name_occurrences,
            )
        )
    return hydrated_elements


def _apply_template_content_value(element: dict[str, Any], value: Any) -> dict[str, Any]:
    element_type = element.get("type")
    if element_type == "text":
        return _apply_template_text_content(element, value)
    if element_type == "math":
        return _apply_template_math_content(element, value)
    if element_type == "image":
        return _apply_template_image_content(element, value)
    if element_type == "text-list":
        return _apply_template_text_list_content(element, value)
    if element_type == "table":
        return _apply_template_table_content(element, value)
    if element_type == "chart":
        return _apply_template_chart_content(element, value)
    return copy.deepcopy(element)


def _apply_template_math_content(
    element: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    latex = read_template_text(value)
    if latex is None or latex.strip() == "":
        return copy.deepcopy(element)

    normalized = latex.strip()
    if normalized.startswith("$$") and normalized.endswith("$$") and len(normalized) > 4:
        normalized = normalized[2:-2].strip()
    elif normalized.startswith(r"\[") and normalized.endswith(r"\]") and len(normalized) > 4:
        normalized = normalized[2:-2].strip()

    updated = copy.deepcopy(element)
    updated["latex"] = normalized[:4000]
    return updated


def _apply_template_text_content(
    element: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    text = read_template_text(value)
    if text is None or text == "":
        return copy.deepcopy(element)

    updated = copy.deepcopy(element)
    first_run = _first_template_text_run(element.get("runs"))
    updated["runs"] = _template_text_runs_from_markdown(
        text,
        first_run,
        fallback_font=element.get("font"),
    )
    updated.pop("text", None)
    return updated


def _apply_template_image_content(
    element: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return copy.deepcopy(element)

    url = None
    for key in ("image_url", "icon_url", "__image_url__", "__icon_url__", "url"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            url = candidate
            break

    if not url:
        return copy.deepcopy(element)

    updated = copy.deepcopy(element)
    updated["data"] = url
    _normalize_generated_image_fit(updated, url)
    prompt = template_asset_prompt(value, is_icon=element.get("is_icon") is True)
    if prompt:
        updated["prompt"] = prompt
    return updated


def _apply_template_text_list_content(
    element: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, list):
        return copy.deepcopy(element)

    existing_items = element.get("items")
    if not isinstance(existing_items, list):
        existing_items = []

    items = []
    for index, item in enumerate(value):
        text = read_template_text(item)
        if text is not None and text != "":
            existing_runs = (
                existing_items[index]
                if index < len(existing_items) and isinstance(existing_items[index], list)
                else None
            )
            first_run = (
                existing_runs[0]
                if isinstance(existing_runs, list)
                and existing_runs
                and isinstance(existing_runs[0], dict)
                else {}
            )
            items.append(
                _template_text_runs_from_markdown(
                    text,
                    first_run,
                    fallback_font=element.get("font"),
                )
            )

    updated = copy.deepcopy(element)
    updated["items"] = items
    return updated


def _apply_template_table_content(
    element: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        return copy.deepcopy(element)

    template_columns = element.get("columns")
    if not isinstance(template_columns, list):
        template_columns = []
    template_rows = [
        row
        for row in element.get("rows", [])
        if isinstance(row, list)
    ]

    generated_columns = [
        _read_template_table_text(item)
        for item in value.get("columns", [])
    ] if isinstance(value.get("columns"), list) else []
    generated_rows = [
        [_read_template_table_text(cell) for cell in row]
        for row in value.get("rows", [])
        if isinstance(row, list)
    ] if isinstance(value.get("rows"), list) else []
    fallback_row = template_rows[-1] if template_rows else template_columns

    updated = copy.deepcopy(element)
    updated["columns"] = (
        _merge_template_table_row_to_length(
            template_columns,
            generated_columns,
            is_header=True,
        )
        if generated_columns
        else copy.deepcopy(template_columns)
    )
    updated["rows"] = (
        [
            _merge_template_table_row_to_length(
                template_rows[index] if index < len(template_rows) else fallback_row,
                row,
                is_header=False,
            )
            for index, row in enumerate(generated_rows)
        ]
        if generated_rows
        else copy.deepcopy(template_rows)
    )
    return updated


def _merge_template_table_row_to_length(
    template_cells: list[Any],
    generated_texts: list[Optional[str]],
    *,
    is_header: bool,
) -> list[Any]:
    fallback_cell = template_cells[-1] if template_cells else None
    return [
        _replace_template_table_cell_text(
            template_cells[index] if index < len(template_cells) else fallback_cell,
            text or "",
            is_header=is_header,
        )
        for index, text in enumerate(generated_texts)
    ]


def _replace_template_table_cell_text(
    cell: Any,
    text: str,
    *,
    is_header: bool,
) -> dict[str, Any]:
    font = GENERATED_TABLE_HEADER_FONT if is_header else GENERATED_TABLE_TEXT_FONT
    if not isinstance(cell, dict):
        return {
            "color": GENERATED_TABLE_CELL_FILL,
            "stroke": GENERATED_TABLE_CELL_STROKE,
            "font": font,
            "runs": _template_text_runs_from_markdown(
                text,
                {"font": font},
            ),
        }

    updated = copy.deepcopy(cell)
    first_run = _first_template_text_run(cell.get("runs"))
    run_font = first_run.get("font") if isinstance(first_run.get("font"), dict) else None
    next_font = run_font or cell.get("font") or font
    updated["color"] = cell.get("color") or cell.get("fill") or GENERATED_TABLE_CELL_FILL
    updated["stroke"] = cell.get("stroke") or GENERATED_TABLE_CELL_STROKE
    updated["font"] = cell.get("font") or next_font
    updated["runs"] = _template_text_runs_from_markdown(
        text,
        first_run,
        fallback_font=next_font,
    )
    updated.pop("text", None)
    updated.pop("fill", None)
    return updated


def _first_template_text_run(runs: Any) -> dict[str, Any]:
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        return runs[0]
    return {}


def _template_text_runs_from_markdown(
    text: str,
    first_run: Any,
    *,
    fallback_font: Any = None,
) -> list[dict[str, Any]]:
    if parse_latex_tags(text) is not None or (
        isinstance(first_run, dict) and first_run.get("type") == "latex"
    ):
        return replace_text_runs(
            [first_run] if isinstance(first_run, dict) else None,
            text,
            fallback_font,
        )

    base_run = copy.deepcopy(first_run) if isinstance(first_run, dict) else {}
    parsed = _parse_template_markdown_text(text)
    has_markdown_style = any(style for _parsed_text, style in parsed)
    base_run = _template_base_run_for_markdown(
        base_run,
        fallback_font,
        strip_inline_emphasis=has_markdown_style,
    )

    text_runs: list[dict[str, Any]] = []
    for parsed_text, style in parsed:
        run = copy.deepcopy(base_run)
        run["text"] = parsed_text
        if style:
            font = run.get("font")
            run["font"] = {
                **(copy.deepcopy(font) if isinstance(font, dict) else {}),
                **style,
            }
        _append_template_text_run(text_runs, run)

    if text_runs:
        return text_runs
    return [{**base_run, "text": " "}]


def _template_base_run_for_markdown(
    base_run: dict[str, Any],
    fallback_font: Any,
    *,
    strip_inline_emphasis: bool,
) -> dict[str, Any]:
    font = base_run.get("font")
    if isinstance(fallback_font, dict):
        merged_font = {
            **copy.deepcopy(fallback_font),
            **(copy.deepcopy(font) if isinstance(font, dict) else {}),
        }
        base_run["font"] = merged_font
    elif isinstance(font, dict):
        base_run["font"] = copy.deepcopy(font)

    if strip_inline_emphasis and isinstance(base_run.get("font"), dict):
        base_run["font"].pop("bold", None)
        base_run["font"].pop("italic", None)

    return base_run


def _parse_template_markdown_text(
    text: str,
) -> list[tuple[str, dict[str, bool]]]:
    parsed: list[tuple[str, dict[str, bool]]] = []
    index = 0

    while index < len(text):
        strong_delimiter = _template_read_markdown_delimiter(
            text,
            index,
            TEMPLATE_STRONG_MARKDOWN_DELIMITERS,
        )
        if strong_delimiter:
            close = text.find(strong_delimiter, index + len(strong_delimiter))
            if close > index + len(strong_delimiter):
                parsed.append(
                    (
                        text[index + len(strong_delimiter) : close],
                        {"bold": True},
                    )
                )
                index = close + len(strong_delimiter)
                continue

        emphasis_delimiter = _template_read_markdown_delimiter(
            text,
            index,
            TEMPLATE_EMPHASIS_MARKDOWN_DELIMITERS,
        )
        if emphasis_delimiter:
            close = text.find(emphasis_delimiter, index + len(emphasis_delimiter))
            if close > index + len(emphasis_delimiter):
                parsed.append(
                    (
                        text[index + len(emphasis_delimiter) : close],
                        {"italic": True},
                    )
                )
                index = close + len(emphasis_delimiter)
                continue

        next_index = _template_next_markdown_delimiter_index(text, index + 1)
        parsed.append(
            (
                text[index : len(text) if next_index == -1 else next_index],
                {},
            )
        )
        index = len(text) if next_index == -1 else next_index

    return parsed


def _template_read_markdown_delimiter(
    text: str,
    index: int,
    delimiters: tuple[str, ...],
) -> Optional[str]:
    for delimiter in delimiters:
        if text.startswith(delimiter, index):
            return delimiter
    return None


def _template_next_markdown_delimiter_index(text: str, start: int) -> int:
    indexes = [
        index
        for index in (
            text.find(delimiter, start)
            for delimiter in TEMPLATE_MARKDOWN_DELIMITERS
        )
        if index != -1
    ]
    return min(indexes) if indexes else -1


def _append_template_text_run(
    text_runs: list[dict[str, Any]],
    run: dict[str, Any],
) -> None:
    text = run.get("text")
    if not isinstance(text, str) or text == "":
        return

    previous = text_runs[-1] if text_runs else None
    if isinstance(previous, dict):
        previous_style = {key: value for key, value in previous.items() if key != "text"}
        next_style = {key: value for key, value in run.items() if key != "text"}
        if previous_style == next_style and isinstance(previous.get("text"), str):
            previous["text"] += text
            return

    text_runs.append(run)


def _read_template_table_text(value: Any) -> Optional[str]:
    primitive_text = _read_template_primitive_table_text(value)
    if primitive_text is not None:
        return primitive_text[:80]

    if not isinstance(value, dict):
        return None

    runs = value.get("runs")
    if isinstance(runs, list):
        run_text = "".join(
            run.get("text", "")
            for run in runs
            if isinstance(run, dict) and isinstance(run.get("text"), str)
        )
        if run_text:
            return run_text[:80]

    for key in ("text", "value"):
        text = _read_template_primitive_table_text(value.get(key))
        if text is not None:
            return text[:80]

    return None


def _read_template_primitive_table_text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _read_template_data_labels(value: Any) -> Optional[str]:
    if value is True:
        return "top"
    if value is False or value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"base", "mid", "top", "outside"}:
            return normalized
    return None


def _apply_template_chart_content(
    element: dict[str, Any],
    value: Any,
) -> dict[str, Any]:
    updated = copy.deepcopy(element)
    updated.pop("data_labels_color", None)
    updated.pop("grid", None)
    if not isinstance(value, dict):
        return updated

    chart_type = value.get("chartType", value.get("chart_type"))
    if chart_type in {
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
    }:
        updated["chart_type"] = chart_type
    if isinstance(value.get("title"), str):
        updated["title"] = value["title"]
    if isinstance(value.get("categories"), list) and value["categories"]:
        updated["categories"] = value["categories"]
    if isinstance(value.get("series"), list) and value["series"]:
        updated["series"] = value["series"]
    colors = value.get("colors")
    if isinstance(colors, list) and colors:
        updated["colors"] = colors
    for source_key, target_key in (
        ("axisColor", "axis_color"),
        ("axis_color", "axis_color"),
        ("gridColor", "grid_color"),
        ("grid_color", "grid_color"),
        ("legendColor", "legend_color"),
        ("legend_color", "legend_color"),
        ("xAxisTitle", "x_axis_title"),
        ("x_axis_title", "x_axis_title"),
        ("yAxisTitle", "y_axis_title"),
        ("y_axis_title", "y_axis_title"),
        ("source", "source"),
    ):
        if isinstance(value.get(source_key), str):
            updated[target_key] = value[source_key]
    for source_key, target_key in (
        ("xAxis", "x_axis"),
        ("x_axis", "x_axis"),
        ("yAxis", "y_axis"),
        ("y_axis", "y_axis"),
        ("xAxisGrid", "x_axis_grid"),
        ("x_axis_grid", "x_axis_grid"),
        ("yAxisGrid", "y_axis_grid"),
        ("y_axis_grid", "y_axis_grid"),
    ):
        if isinstance(value.get(source_key), bool):
            updated[target_key] = value[source_key]
    for source_key in ("dataLabels", "data_labels"):
        if source_key in value:
            updated["data_labels"] = _read_template_data_labels(value.get(source_key))
    return updated


