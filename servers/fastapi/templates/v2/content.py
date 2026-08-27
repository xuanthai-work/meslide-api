"""Helpers for applying generated content to Template V2 element structures."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from typing import Any

from .schema import get_repeated_top_level_group_schema_name


def content_name_candidates(name: str) -> list[str]:
    without_numeric_token = re.sub(r"_\d+(?=_|$)", "", name)
    without_prefix = (
        without_numeric_token.split("_", 1)[1]
        if "_" in without_numeric_token
        else without_numeric_token
    )

    candidates: list[str] = []
    for candidate in (name, without_numeric_token, without_prefix):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def lookup_template_content(
    content: dict[str, Any],
    name: str,
    *,
    preferred_keys: list[str] | None = None,
) -> tuple[bool, Any]:
    candidates: list[str] = []
    for candidate in [
        *(preferred_keys or []),
        *content_name_candidates(name),
    ]:
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate in content:
            return True, content[candidate]
    return False, None


def repeated_content_keys_for_name(
    name: str,
    content: dict[str, Any],
    name_occurrences: dict[str, int],
) -> list[str] | None:
    occurrence_index = name_occurrences.get(name, 0)
    name_occurrences[name] = occurrence_index + 1
    if occurrence_index == 0:
        return None

    suffixed_key = f"{name}_{occurrence_index + 1}"
    return [suffixed_key] if suffixed_key in content else None


def template_asset_prompt(value: Any, *, is_icon: bool) -> str | None:
    if not isinstance(value, dict):
        return None

    prompt_keys = (
        ("icon_query", "__icon_query__", "query", "prompt")
        if is_icon
        else ("image_prompt", "__image_prompt__", "prompt", "query")
    )
    for key in prompt_keys:
        prompt = value.get(key)
        if isinstance(prompt, str) and prompt.strip():
            return prompt
    return None


def read_template_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        if isinstance(text, (int, float)) and not isinstance(text, bool):
            return str(text)
    return None


def hydrate_repeated_top_level_groups(
    elements: list[Any],
    content: Any,
    *,
    apply_item: Callable[[dict[str, Any], Any], dict[str, Any]],
) -> list[Any] | None:
    """Map one generated array item to each complete positioned group.

    Repeated top-level groups are one schema array even though each source group
    can have different positions and decorative connector geometry. Hydrating
    the array at the child level would discard the first item and duplicate the
    content child inside every positioned group.
    """
    if not isinstance(content, dict):
        return None

    field_name = get_repeated_top_level_group_schema_name(elements)
    if field_name is None:
        return None

    values = content.get(field_name)
    if not isinstance(values, list) or not elements:
        return None

    hydrated: list[Any] = []
    for index, value in enumerate(values):
        source = copy.deepcopy(elements[min(index, len(elements) - 1)])
        if not isinstance(source, dict):
            return None
        hydrated.append(apply_item(source, value))
    return hydrated
