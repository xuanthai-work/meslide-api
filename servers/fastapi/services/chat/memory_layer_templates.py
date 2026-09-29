"""Template-content application helpers for chat memory.

Extracted from ``PresentationChatMemoryLayer`` as a mixin so the memory
layer file no longer owns the Template V2 content-apply family.
"""

from __future__ import annotations

import copy
from typing import Any

from templates.v2.content import (
    hydrate_repeated_top_level_groups,
    lookup_template_content,
    read_template_text,
    repeated_content_keys_for_name,
    template_asset_prompt,
)
from utils.asset_directory_utils import normalize_slide_asset_url
from utils.latex_text import replace_text_runs

TEMPLATE_GENERATED_ELEMENT_TYPES = {
    "text",
    "math",
    "image",
    "text-list",
    "table",
    "chart",
    "infographic",
}


class PresentationChatTemplateMixin:
    @classmethod
    def _apply_template_content_to_ui(
        cls,
        ui: dict[str, Any],
        content: dict[str, Any],
        *,
        theme: dict[str, Any] | None = None,
    ) -> None:
        components = ui.get("components")
        if not isinstance(components, list):
            return

        component_counts: dict[str, int] = {}
        for component in components:
            if isinstance(component, dict):
                component_id = str(component.get("id") or "")
                component_counts[component_id] = (
                    component_counts.get(component_id, 0) + 1
                )

        component_seen: dict[str, int] = {}
        used_keys: set[str] = set()
        for component in components:
            if not isinstance(component, dict):
                continue
            component_id = str(component.get("id") or "")
            occurrence_index = component_seen.get(component_id, 0)
            component_seen[component_id] = occurrence_index + 1
            candidate_keys = [component_id]
            if component_counts.get(component_id, 0) > 1:
                candidate_keys.insert(0, f"{component_id}_{occurrence_index}")

            component_content = None
            for key in candidate_keys:
                if key in used_keys:
                    continue
                value = content.get(key)
                if isinstance(value, dict):
                    component_content = value
                    used_keys.add(key)
                    break
            if component_content is None:
                continue

            cls._apply_template_component_content(
                component,
                component_content,
                theme=theme,
            )

    @classmethod
    def _apply_template_component_content(
        cls,
        component: dict[str, Any],
        content: dict[str, Any],
        *,
        theme: dict[str, Any] | None = None,
    ) -> None:
        elements = component.get("elements")
        if not isinstance(elements, list):
            return

        def apply_repeated_item(
            element: dict[str, Any],
            item: Any,
        ) -> dict[str, Any]:
            cls._apply_template_element_content(
                element,
                item,
                theme=theme,
                direct_value=True,
            )
            return element

        repeated_groups = hydrate_repeated_top_level_groups(
            elements,
            content,
            apply_item=apply_repeated_item,
        )
        if repeated_groups is not None:
            component["elements"] = repeated_groups
            return

        cls._apply_template_elements_content(
            elements,
            content,
            theme=theme,
        )

    @classmethod
    def _apply_template_element_content(
        cls,
        element: dict[str, Any],
        content: Any,
        *,
        theme: dict[str, Any] | None = None,
        direct_value: bool = False,
        preferred_content_keys: list[str] | None = None,
        name_occurrences: dict[str, int] | None = None,
    ) -> None:
        content_values = content if isinstance(content, dict) else {}
        element_type = element.get("type")
        name = element.get("name")
        has_value = False
        value = None
        if isinstance(name, str):
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
            has_value
            and element.get("decorative") is False
            and element_type in TEMPLATE_GENERATED_ELEMENT_TYPES
        ):
            cls._set_template_element_value(
                element,
                value,
                theme=theme,
            )
            return

        if (
            direct_value
            and not has_value
            and element.get("decorative") is False
            and element_type in TEMPLATE_GENERATED_ELEMENT_TYPES
        ):
            cls._set_template_element_value(
                element,
                content,
                theme=theme,
            )
            return

        nested_content = value if isinstance(value, dict) else content_values
        nested_direct_value = direct_value and not has_value
        nested_name_occurrences = (
            {}
            if has_value and isinstance(value, dict)
            else name_occurrences
        )

        child = element.get("child")
        if isinstance(child, dict):
            cls._apply_template_element_content(
                child,
                nested_content,
                theme=theme,
                direct_value=nested_direct_value,
                name_occurrences=nested_name_occurrences,
            )

        children = element.get("children")
        if isinstance(children, list):
            if isinstance(value, list) and children:
                next_children: list[Any] = []
                for index, item in enumerate(value):
                    source_child = copy.deepcopy(children[min(index, len(children) - 1)])
                    if isinstance(source_child, dict):
                        cls._apply_template_element_content(
                            source_child,
                            item,
                            theme=theme,
                            direct_value=True,
                        )
                    next_children.append(source_child)
                element["children"] = next_children
                return

            cls._apply_template_elements_content(
                children,
                nested_content,
                theme=theme,
                direct_value=nested_direct_value,
                name_occurrences=nested_name_occurrences,
            )

    @classmethod
    def _apply_template_elements_content(
        cls,
        elements: list[Any],
        content: Any,
        *,
        theme: dict[str, Any] | None = None,
        direct_value: bool = False,
        name_occurrences: dict[str, int] | None = None,
    ) -> None:
        scoped_name_occurrences = (
            name_occurrences if name_occurrences is not None else {}
        )
        for element in elements:
            if not isinstance(element, dict):
                continue
            cls._apply_template_element_content(
                element,
                content,
                theme=theme,
                direct_value=direct_value,
                name_occurrences=scoped_name_occurrences,
            )

    @classmethod
    def _set_template_element_value(
        cls,
        element: dict[str, Any],
        value: Any,
        *,
        theme: dict[str, Any] | None = None,
    ) -> None:
        element_type = element.get("type")
        if element_type == "text":
            text = read_template_text(value)
            if text is None or text == "":
                return
            cls._set_template_runs_text(element, text)
            if any(run.get("type") == "latex" for run in element["runs"]):
                element.pop("text", None)
            else:
                element["text"] = text
            return

        if element_type == "math":
            latex = read_template_text(value)
            if latex is None or not latex.strip():
                return
            normalized = latex.strip()
            if normalized.startswith("$$") and normalized.endswith("$$"):
                normalized = normalized[2:-2].strip()
            elif normalized.startswith(r"\[") and normalized.endswith(r"\]"):
                normalized = normalized[2:-2].strip()
            element["latex"] = normalized[:4000]
            return

        if element_type == "text-list" and isinstance(value, list):
            source_items = (
                element.get("items") if isinstance(element.get("items"), list) else []
            )
            element["items"] = [
                cls._replacement_runs_from_existing(
                    source_items[index] if index < len(source_items) else None,
                    read_template_text(item) or "",
                    element.get("font"),
                )
                for index, item in enumerate(value)
            ]
            return

        if element_type == "image":
            asset_url = cls._template_asset_url(value)
            if asset_url:
                from services.chat.slide_ui_charts import (
                    _normalize_generated_image_fit,
                )

                element["data"] = asset_url
                fit_value = value.get("fit") if isinstance(value, dict) else None
                if isinstance(fit_value, str) and fit_value in ("contain", "cover", "fill"):
                    element["fit"] = fit_value
                else:
                    _normalize_generated_image_fit(element, asset_url)
                prompt = template_asset_prompt(
                    value,
                    is_icon=element.get("is_icon") is True,
                )
                if prompt:
                    element["prompt"] = prompt
            return

        if element_type == "chart" and isinstance(value, dict):
            from services.chat.slide_ui_charts import _apply_chart_content_update

            _apply_chart_content_update(element, value, theme)
            return

        if element_type == "infographic" and isinstance(value, dict):
            cls._set_template_infographic_content(element, value)
            return

        if element_type == "table":
            if isinstance(value, dict):
                cls._set_template_table_content(element, value)
            elif isinstance(value, list):
                cls._set_template_table_rows(element, value)

    @staticmethod
    def _set_template_infographic_content(
        element: dict[str, Any],
        value: dict[str, Any],
    ) -> None:
        data = value.get("data")
        if isinstance(data, dict) and data.get("type") in {"progress_bar", "gauge"}:
            next_data: dict[str, Any] = {"type": data["type"]}
            for key in ("min_value", "max_value", "value"):
                raw = data.get(key)
                if isinstance(raw, (int, float)):
                    next_data[key] = float(raw)
            if {"min_value", "max_value", "value"}.issubset(next_data):
                element["data"] = next_data

        colors = value.get("colors")
        if isinstance(colors, list):
            element["colors"] = [
                color
                for color in colors
                if isinstance(color, str) and color.strip()
            ]

    @classmethod
    def _set_template_runs_text(cls, element: dict[str, Any], text: str) -> None:
        element["runs"] = cls._replacement_runs_from_existing(
            element.get("runs"),
            text,
            element.get("font"),
        )

    @staticmethod
    def _replacement_runs_from_existing(
        existing_runs: Any,
        text: str,
        fallback_font: Any,
    ) -> list[dict[str, Any]]:
        return replace_text_runs(existing_runs, text, fallback_font)

    @staticmethod
    def _template_asset_url(value: Any) -> str | None:
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
                if normalized_url.strip().startswith(
                    ("http://", "https://", "/app_data/", "/static/", "data:", "blob:")
                ):
                    return normalized_url
                if fallback_url is None:
                    fallback_url = normalized_url
        return fallback_url

    @classmethod
    def _set_template_table_content(
        cls,
        element: dict[str, Any],
        value: dict[str, Any],
    ) -> None:
        columns = value.get("columns")
        if isinstance(columns, list):
            existing_columns = (
                element.get("columns")
                if isinstance(element.get("columns"), list)
                else []
            )
            element["columns"] = cls._template_table_cells_from_values(
                existing_columns,
                columns,
                element.get("font"),
            )

        rows = value.get("rows")
        if isinstance(rows, list):
            cls._set_template_table_rows(element, rows)

    @classmethod
    def _template_table_cells_from_values(
        cls,
        existing_cells: list[Any],
        values: list[Any],
        fallback_font: Any,
    ) -> list[dict[str, Any]]:
        fallback_cell = existing_cells[-1] if existing_cells else None
        cells: list[dict[str, Any]] = []
        for index, value in enumerate(values):
            existing_cell = (
                existing_cells[index]
                if index < len(existing_cells)
                else fallback_cell
            )
            existing_runs = (
                existing_cell.get("runs")
                if isinstance(existing_cell, dict)
                else None
            )
            cell = copy.deepcopy(existing_cell) if isinstance(existing_cell, dict) else {}
            cell["runs"] = cls._replacement_runs_from_existing(
                existing_runs,
                cls._table_cell_text_value(value),
                cell.get("font") or fallback_font,
            )
            cells.append(cell)
        return cells

    @classmethod
    def _set_template_table_rows(
        cls,
        element: dict[str, Any],
        rows: list[Any],
    ) -> None:
        existing_rows = (
            element.get("rows") if isinstance(element.get("rows"), list) else []
        )
        next_rows: list[list[dict[str, Any]]] = []
        for row_index, row in enumerate(rows):
            if not isinstance(row, list):
                continue
            existing_row = (
                existing_rows[row_index]
                if row_index < len(existing_rows)
                and isinstance(existing_rows[row_index], list)
                else []
            )
            next_rows.append(
                cls._template_table_cells_from_values(
                    existing_row,
                    row,
                    element.get("font"),
                )
            )
        element["rows"] = next_rows

