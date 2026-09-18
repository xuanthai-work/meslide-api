"""Tool-argument repair and JSON-ish parse helpers for chat tools.

Extracted from ``ChatTools`` as a mixin. ``tools.py`` keeps tool
definitions, dispatch, and recovery messaging and re-exports the
insert-field constants used by tests and callers.
"""

from __future__ import annotations

import json
import re
from typing import Any

import dirtyjson  # type: ignore[import-untyped]

CHART_INSERT_TOOL_FIELDS = {
    "addElement": "element",
    "addComponent": "component",
    "createComponent": "component",
    "updateComponent": "component",
}
TABLE_INSERT_TOOL_FIELDS = CHART_INSERT_TOOL_FIELDS
IMAGE_INSERT_TOOL_FIELDS = CHART_INSERT_TOOL_FIELDS
JSON_OBJECT_STRING_FIELDS = {
    "addNewSlideLayout": ("content",),
    "saveSlide": ("content",),
    "updateSlide": ("content",),
    "addElement": ("element",),
    "updateElement": ("element",),
    "addComponent": ("component",),
    "createComponent": ("component",),
    "updateComponent": ("component",),
}


class ChatToolsRepairMixin:
    def _repair_tool_args(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        error: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        repaired = dict(args)
        notes: list[str] = []
        self._repair_json_object_string_fields(tool_name, repaired, notes)
        repaired = self._repair_chart_insert_args(
            tool_name,
            repaired,
            notes,
            error=error,
        )
        repaired = self._repair_table_insert_args(
            tool_name,
            repaired,
            notes,
            error=error,
        )
        repaired = self._repair_image_insert_args(
            tool_name,
            repaired,
            notes,
            error=error,
        )
        return repaired, notes

    @staticmethod
    def _repair_json_object_string_fields(
        tool_name: str,
        args: dict[str, Any],
        notes: list[str],
    ) -> None:
        for field_name in JSON_OBJECT_STRING_FIELDS.get(tool_name, ()):
            if field_name not in args:
                continue
            value = args.get(field_name)
            if value is None:
                continue
            if isinstance(value, dict):
                args[field_name] = json.dumps(value, ensure_ascii=False)
                notes.append(
                    f"Converted {field_name} from object to JSON string."
                )
                continue
            if not isinstance(value, str):
                continue
            parsed = ChatToolsRepairMixin._loads_jsonish_object(value)
            if parsed is None:
                continue
            canonical = json.dumps(parsed, ensure_ascii=False)
            if canonical != value:
                args[field_name] = canonical
                notes.append(f"Repaired JSON string field {field_name}.")

    def _repair_chart_insert_args(
        self,
        tool_name: str,
        args: dict[str, Any],
        notes: list[str],
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload_field = CHART_INSERT_TOOL_FIELDS.get(tool_name)
        if not payload_field or payload_field not in args:
            return args

        chart_rows = self._extract_chart_rows_from_user_message(
            self._turn_user_message,
        )
        if not chart_rows:
            return args

        payload = self._parse_json_object_field(args.get(payload_field))
        if payload is None:
            return args

        title = self._infer_chart_title_from_user_message(self._turn_user_message)
        if not self._inject_missing_chart_data(payload, chart_rows, title):
            return args

        repaired = dict(args)
        repaired[payload_field] = json.dumps(payload, ensure_ascii=False)
        notes.append(
            "Filled missing chart categories and series.values from the latest user message."
        )
        return repaired

    def _repair_table_insert_args(
        self,
        tool_name: str,
        args: dict[str, Any],
        notes: list[str],
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload_field = TABLE_INSERT_TOOL_FIELDS.get(tool_name)
        if not payload_field or payload_field not in args:
            return args

        table_data = self._extract_table_from_user_message(self._turn_user_message)
        if table_data is None:
            return args

        payload = self._parse_json_object_field(args.get(payload_field))
        if payload is None:
            return args

        if not self._inject_missing_table_data(payload, table_data):
            return args

        repaired = dict(args)
        repaired[payload_field] = json.dumps(payload, ensure_ascii=False)
        notes.append(
            "Filled missing table headers/columns and rows from the latest user message."
        )
        return repaired

    def _repair_image_insert_args(
        self,
        tool_name: str,
        args: dict[str, Any],
        notes: list[str],
        *,
        error: str | None = None,
    ) -> dict[str, Any]:
        payload_field = IMAGE_INSERT_TOOL_FIELDS.get(tool_name)
        if not payload_field or payload_field not in args:
            return args

        payload = self._parse_json_object_field(args.get(payload_field))
        if payload is None:
            return args

        if not self._inject_missing_image_data(payload):
            return args

        repaired = dict(args)
        repaired[payload_field] = json.dumps(payload, ensure_ascii=False)
        notes.append(
            "Filled missing image data from the generated asset URL in this turn."
        )
        return repaired

    @staticmethod
    def _parse_json_object_field(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return json.loads(json.dumps(value, ensure_ascii=False))
        if not isinstance(value, str) or not value.strip():
            return None
        parsed = ChatToolsRepairMixin._loads_jsonish_object(value)
        if not isinstance(parsed, dict):
            return None
        return json.loads(json.dumps(parsed, ensure_ascii=False))

    def _remember_generated_assets(self, result: dict[str, Any]) -> None:
        assets = result.get("assets") if isinstance(result, dict) else None
        if not isinstance(assets, list):
            return

        for asset in assets:
            if not isinstance(asset, dict):
                continue
            url = asset.get("url")
            if not isinstance(url, str) or not url.strip():
                continue
            self._generated_assets.append(
                {
                    "kind": str(asset.get("kind") or "image"),
                    "prompt": str(asset.get("prompt") or ""),
                    "url": url.strip(),
                }
            )

    @staticmethod
    def _loads_jsonish_object(value: str) -> dict[str, Any] | None:
        try:
            parsed = ChatToolsRepairMixin._loads_jsonish(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _loads_jsonish(value: str) -> Any:
        last_exc: Exception | None = None
        for candidate in ChatToolsRepairMixin._jsonish_candidates(value):
            try:
                return dirtyjson.loads(candidate)
            except Exception as exc:
                last_exc = exc
            try:
                return json.loads(candidate)
            except Exception as exc:
                last_exc = exc
        if last_exc:
            raise last_exc
        raise ValueError("JSON value is empty.")

    @staticmethod
    def _jsonish_candidates(value: str) -> list[str]:
        stripped = (value or "").strip()
        if not stripped:
            return []

        candidates = [stripped]
        fence_match = re.search(
            r"```(?:json|javascript|js)?\s*(.*?)```",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if fence_match:
            candidates.append(fence_match.group(1).strip())

        for opener, closer in (("{", "}"), ("[", "]")):
            start = stripped.find(opener)
            end = stripped.rfind(closer)
            if start != -1 and end > start:
                candidates.append(stripped[start : end + 1].strip())

        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            unique.append(candidate)
        return unique

    @classmethod
    def _inject_missing_chart_data(
        cls,
        node: Any,
        rows: list[dict[str, Any]],
        title: str,
    ) -> bool:
        from services.chat.slide_ui_charts import _chart_element_has_explicit_data

        changed = False
        if isinstance(node, dict):
            if node.get("type") == "chart" and not _chart_element_has_explicit_data(node):
                categories = [row["label"] for row in rows]
                values = [row["value"] for row in rows]
                node.setdefault("chart_type", "bar")
                node.setdefault("title", title)
                node["categories"] = categories
                node["series"] = [{"name": title or "Series 1", "values": values}]
                node["data"] = [
                    {"label": row["label"], "value": row["value"]}
                    for row in rows
                ]
                changed = True
            for value in node.values():
                changed = cls._inject_missing_chart_data(value, rows, title) or changed
        elif isinstance(node, list):
            for value in node:
                changed = cls._inject_missing_chart_data(value, rows, title) or changed
        return changed

    @classmethod
    def _inject_missing_table_data(
        cls,
        node: Any,
        table_data: dict[str, Any],
    ) -> bool:
        from services.chat.slide_ui_charts import _table_element_has_explicit_data

        changed = False
        if isinstance(node, dict):
            if node.get("type") == "table" and not _table_element_has_explicit_data(node):
                columns = list(table_data["columns"])
                rows = [list(row) for row in table_data["rows"]]
                node["columns"] = columns
                node["rows"] = rows
                node.setdefault("min_columns", 1)
                node.setdefault("max_columns", max(len(columns), 1))
                node.setdefault("min_rows", 1)
                node.setdefault("max_rows", max(len(rows), 1))
                changed = True
            for value in node.values():
                changed = cls._inject_missing_table_data(value, table_data) or changed
        elif isinstance(node, list):
            for value in node:
                changed = cls._inject_missing_table_data(value, table_data) or changed
        return changed

    def _inject_missing_image_data(self, node: Any) -> bool:
        from services.chat.slide_ui_charts import _image_element_has_explicit_data

        changed = False
        if isinstance(node, dict):
            if node.get("type") == "image" and not _image_element_has_explicit_data(node):
                asset = self._latest_generated_asset_for_image(
                    is_icon=node.get("is_icon") is True,
                )
                if asset is not None:
                    node["data"] = asset["url"]
                    node.setdefault("is_icon", asset.get("kind") == "icon")
                    prompt = asset.get("prompt")
                    if isinstance(prompt, str) and prompt.strip():
                        node.setdefault("prompt", prompt.strip())
                    changed = True
            for value in node.values():
                changed = self._inject_missing_image_data(value) or changed
        elif isinstance(node, list):
            for value in node:
                changed = self._inject_missing_image_data(value) or changed
        return changed

    def _latest_generated_asset_for_image(
        self,
        *,
        is_icon: bool,
    ) -> dict[str, Any] | None:
        preferred_kind = "icon" if is_icon else "image"
        for asset in reversed(self._generated_assets):
            if asset.get("kind") == preferred_kind and asset.get("url"):
                return asset
        for asset in reversed(self._generated_assets):
            if asset.get("url"):
                return asset
        return None

    @classmethod
    def _extract_chart_rows_from_user_message(
        cls,
        user_message: str,
    ) -> list[dict[str, Any]]:
        text = cls._strip_ui_context_prefix(user_message)
        if not text:
            return []

        rows: list[dict[str, Any]] = []
        seen_labels: set[str] = set()
        segments = re.split(r"[\n;,|]+|\s+\band\b\s+", text, flags=re.IGNORECASE)
        for segment in segments:
            segment = segment.strip(" \t\r\n.:-–—")
            if not segment:
                continue
            for match in re.finditer(
                r"(?P<label>[A-Za-z][A-Za-z0-9&'./() -]{0,80}?)"
                r"\s*(?:[:=\-–—]|\bhas\b|\bhave\b|\bwith\b)?\s+"
                r"(?P<value>[-+]?(?:\d[\d,]*(?:\.\d+)?|\.\d+))\b",
                segment,
                flags=re.IGNORECASE,
            ):
                label = cls._clean_chart_label(match.group("label"))
                if not label:
                    continue
                number = cls._parse_chart_number(match.group("value"))
                if number is None:
                    continue
                normalized_label = label.lower()
                if normalized_label in seen_labels:
                    continue
                seen_labels.add(normalized_label)
                rows.append({"label": label, "value": number})
                if len(rows) >= 12:
                    return rows
        return rows if len(rows) >= 2 else []

    @classmethod
    def _extract_table_from_user_message(
        cls,
        user_message: str,
    ) -> dict[str, Any] | None:
        text = cls._strip_ui_context_prefix(user_message)
        if not text:
            return None

        marker_match = re.search(r"\bdata\s*:\s*(?P<data>.+)$", text, re.IGNORECASE | re.DOTALL)
        if marker_match:
            header_text = text[: marker_match.start()]
            headers = cls._extract_table_headers(header_text)
            rows = cls._parse_table_rows(marker_match.group("data"))
            if headers and rows:
                normalized_rows = cls._align_table_rows(rows, len(headers))
                if normalized_rows:
                    return {"columns": headers, "rows": normalized_rows}

        csv_rows = cls._parse_table_rows(text, row_split_pattern=r"[\n;]+")
        if len(csv_rows) >= 2:
            headers = csv_rows[0]
            rows = cls._align_table_rows(csv_rows[1:], len(headers))
            if headers and rows:
                return {"columns": headers, "rows": rows}

        return None

    @classmethod
    def _extract_table_headers(cls, text: str) -> list[str]:
        patterns = (
            r"\bfirst\s+row\s+with\s+(?P<headers>[^.\n;]+)",
            r"\bheaders?\s*(?:are|with|:)?\s+(?P<headers>[^.\n;]+)",
            r"\bcolumns?\s*(?:are|with|:)?\s+(?P<headers>[^.\n;]+)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            headers = cls._split_table_cells(match.group("headers"))
            if len(headers) >= 1:
                return headers
        return []

    @classmethod
    def _parse_table_rows(
        cls,
        text: str,
        *,
        row_split_pattern: str = r"[;\n]+",
    ) -> list[list[str]]:
        rows: list[list[str]] = []
        for raw_row in re.split(row_split_pattern, text):
            cells = cls._split_table_cells(raw_row)
            if cells:
                rows.append(cells)
        return rows

    @staticmethod
    def _split_table_cells(text: str) -> list[str]:
        cells = [
            cell.strip(" \t\r\n.:-–—")
            for cell in re.split(r"\s*\|\s*|\s*,\s*", text or "")
        ]
        return [cell for cell in cells if cell]

    @staticmethod
    def _align_table_rows(rows: list[list[str]], column_count: int) -> list[list[str]]:
        aligned: list[list[str]] = []
        for row in rows:
            if len(row) == column_count:
                aligned.append(row)
        return aligned

    @staticmethod
    def _clean_chart_label(value: str) -> str:
        label = re.sub(r"\s+", " ", value or "").strip(" \t\r\n.:-–—")
        label = re.sub(
            r"^(?:and|the|for|category|label|value|metric|series)\s+",
            "",
            label,
            flags=re.IGNORECASE,
        ).strip(" \t\r\n.:-–—")
        if not label:
            return ""
        if label.lower() in {
            "slide",
            "index",
            "chart",
            "bar chart",
            "line chart",
            "user message",
        }:
            return ""
        if label == label.lower():
            return label[:1].upper() + label[1:]
        return label

    @staticmethod
    def _parse_chart_number(value: str) -> float | int | None:
        try:
            number = float(str(value).replace(",", ""))
        except ValueError:
            return None
        if not number == number or number in {float("inf"), float("-inf")}:
            return None
        return int(number) if number.is_integer() else number

    @classmethod
    def _infer_chart_title_from_user_message(cls, user_message: str) -> str:
        text = cls._strip_ui_context_prefix(user_message)
        quoted_title = re.search(
            r"\b(?:titled|called|named)\s+[\"']([^\"']{1,80})[\"']",
            text,
            flags=re.IGNORECASE,
        )
        if quoted_title:
            return quoted_title.group(1).strip()

        count_title = re.search(
            r"\b(?:number\s+of|no\.?\s*of|no\s+of)\s+"
            r"(?P<title>[A-Za-z][A-Za-z ]{1,40}?)"
            r"(?:\s+(?:the|for|by|of|that|which)\b|[.,;\n]|$)",
            text,
            flags=re.IGNORECASE,
        )
        if count_title:
            title = count_title.group("title").strip()
            if title:
                return title[:1].upper() + title[1:]

        lowered = text.lower()
        for keyword, title in (
            ("goal", "Goals"),
            ("revenue", "Revenue"),
            ("sales", "Sales"),
            ("profit", "Profit"),
            ("age", "Age"),
        ):
            if keyword in lowered:
                return title
        return "Chart"

    @staticmethod
    def _strip_ui_context_prefix(user_message: str) -> str:
        marker = "\nUser message:"
        if not user_message.startswith("UI context:"):
            return user_message.strip()
        marker_index = user_message.find(marker)
        if marker_index == -1:
            return user_message.strip()
        return user_message[marker_index + len(marker) :].lstrip()

