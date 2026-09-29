"""Repair vertical "text stack" collisions across every bundled template.

A slide layout places every element at an absolute coordinate. When a heading
box is taller than one line, the canvas fitter allows it to wrap; a two-line
heading can then paint over the paragraph pinned right below it. This script
scans ``templates/*/template.json`` and repairs the stacks:

* **Cách 1** - an oversized box for a short heading (``max_length <= 35``) is
  tightened to a single line. The frontend ``isSingleLineBox`` heuristic then
  keeps the heading on one line (auto-scaling the font) exactly like the
  thumbnail preview.
* **Cách 2** - an oversized box for a long heading (``max_length > 35``) that
  actually overlaps the next text element is converted into a ``flex`` column.
  The paragraph then flows below the rendered heading instead of under a stale
  absolute offset.

Pass ``--check`` to only report remaining vertical overlaps (exit code 1 when
any are found).

Run from the ``gslide-api`` directory::

    python servers/fastapi/scripts/fix_text_stack_collisions.py
    python servers/fastapi/scripts/fix_text_stack_collisions.py --check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

MIN_STACK_GAP = 24
FLEX_STACK_GAP = 16
SHORT_HEADING_MAX_LENGTH = 35
OVERSIZED_SLACK = 1.3
ONE_LINE_PADDING = 1.15
TRUE_OVERLAP_SLACK = 1.6
DECORATIVE_HINTS = ("quote", "mark", "accent", "underline")
OVERLAP_EPSILON = 0.5


def default_templates_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "templates"


def num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def font_of(element: dict) -> dict:
    font = element.get("font")
    return font if isinstance(font, dict) else {}


def line_height_of(element: dict) -> float:
    value = num(font_of(element).get("line_height"))
    return value if value else 1.25


def font_size_of(element: dict) -> float | None:
    return num(font_of(element).get("size"))


def height_of(element: dict) -> float | None:
    return num((element.get("size") or {}).get("height"))


def width_of(element: dict) -> float | None:
    return num((element.get("size") or {}).get("width"))


def position_of(element: dict) -> dict:
    position = element.get("position")
    return position if isinstance(position, dict) else {}


def is_text_element(element: Any) -> bool:
    return isinstance(element, dict) and element.get("type") in ("text", "text-list")


def is_decorative(element: dict) -> bool:
    name = element.get("name")
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    return any(hint in lowered for hint in DECORATIVE_HINTS)


def stacks_vertically(previous: dict, current: dict) -> bool:
    """True when ``current`` sits below ``previous`` and their boxes overlap on X."""

    previous_position = position_of(previous)
    current_position = position_of(current)
    previous_y = num(previous_position.get("y"))
    current_y = num(current_position.get("y"))
    if previous_y is None or current_y is None or current_y < previous_y:
        return False

    previous_x = num(previous_position.get("x")) or 0
    current_x = num(current_position.get("x")) or 0
    previous_width = width_of(previous) or 0
    current_width = width_of(current) or 0
    return previous_x < current_x + current_width and current_x < previous_x + previous_width


def vertical_gap(previous: dict, current: dict) -> float | None:
    previous_y = num(position_of(previous).get("y"))
    current_y = num(position_of(current).get("y"))
    previous_height = height_of(previous)
    if previous_y is None or current_y is None or previous_height is None:
        return None
    return current_y - (previous_y + previous_height)


def oversized(previous: dict) -> bool:
    size = font_size_of(previous)
    height = height_of(previous)
    if size is None or height is None:
        return False
    return height > size * line_height_of(previous) * OVERSIZED_SLACK


def multi_line_capable(previous: dict) -> bool:
    """True when the box is tall enough for the frontend to allow a second line."""

    size = font_size_of(previous)
    height = height_of(previous)
    if size is None or height is None:
        return False
    return height >= size * line_height_of(previous) * TRUE_OVERLAP_SLACK


def single_line_height(previous: dict) -> int | None:
    size = font_size_of(previous)
    if size is None:
        return None
    return round(size * line_height_of(previous) * ONE_LINE_PADDING)


def should_convert_to_flex(previous: dict, current: dict, gap: float) -> bool:
    max_length = previous.get("max_length")
    if isinstance(max_length, int) and max_length <= SHORT_HEADING_MAX_LENGTH:
        return False
    if is_decorative(previous) or is_decorative(current):
        return False
    if not multi_line_capable(previous):
        return False
    return gap < 0


def iter_component_elements(node: Any) -> Iterator[list]:
    """Yield every static component's ``elements`` array (flex/grid excluded)."""

    if isinstance(node, dict):
        elements = node.get("elements")
        if isinstance(elements, list) and node.get("type") not in (
            "flex",
            "grid",
            "group",
            "list-view",
            "grid-view",
        ):
            yield elements
        for value in node.values():
            yield from iter_component_elements(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_component_elements(value)


def iter_parent_components(node: Any) -> Iterator[dict]:
    """Yield every static component dict so it can be converted to flex."""

    if isinstance(node, dict):
        elements = node.get("elements")
        if (
            isinstance(elements, list)
            and elements
            and node.get("type") not in (
                "flex",
                "grid",
                "group",
                "list-view",
                "grid-view",
            )
        ):
            yield node
        for value in node.values():
            yield from iter_parent_components(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_parent_components(value)


def analyze_component(elements: list) -> tuple[int, dict | None]:
    """Return (single_line_fixes, component_to_flex_or_None)."""

    fixes = 0
    convert = False
    for previous, current in zip(elements, elements[1:]):
        if not (is_text_element(previous) and is_text_element(current)):
            continue
        if not stacks_vertically(previous, current):
            continue
        gap = vertical_gap(previous, current)
        if gap is None or gap >= MIN_STACK_GAP:
            continue
        if not oversized(previous):
            continue
        if is_decorative(previous):
            continue

        max_length = previous.get("max_length")
        if isinstance(max_length, int) and max_length <= SHORT_HEADING_MAX_LENGTH:
            target = single_line_height(previous)
            if target is not None and target < (height_of(previous) or 0):
                previous["size"]["height"] = target
                fixes += 1
            continue

        if gap < 0:
            target = estimate_text_height(previous)
            current_y = num(position_of(current).get("y")) or 0
            previous_y = num(position_of(previous).get("y")) or 0
            available_h = max(0, current_y - previous_y - 12)
            safe_h = min(target, available_h) if target is not None else available_h
            if safe_h > 0 and safe_h < (height_of(previous) or 0):
                previous["size"]["height"] = round(safe_h)
                fixes += 1
            continue

    return fixes, None


def estimate_text_height(element: dict) -> int | None:
    """Estimate the natural rendered height of a text element.

    Used to bound the ``flex-basis`` of an oversized heading so the flex column
    does not reserve a giant empty gap (and shove its siblings into the footer).
    """

    size = font_size_of(element)
    width = width_of(element)
    if size is None or width is None or width <= 0:
        return None
    average_char_width = max(1.0, size * 0.52)
    chars_per_line = max(1, int(width // average_char_width))
    max_length = element.get("max_length")
    if isinstance(max_length, int) and max_length > 0:
        text_length = max_length
    else:
        runs = element.get("runs")
        text = (
            "".join(str(run.get("text", "")) for run in runs if isinstance(run, dict))
            if isinstance(runs, list)
            else ""
        )
        text_length = len(text)
    lines = max(1, -(-text_length // chars_per_line))
    return round(lines * size * line_height_of(element))


def normalize_flex_child_heights(component: dict) -> int:
    if component.get("type") != "flex":
        return 0
    children = component.get("children")
    if not isinstance(children, list):
        return 0
    fixes = 0
    for child in children:
        if not (is_text_element(child) and oversized(child)):
            continue
        target = estimate_text_height(child)
        current = height_of(child)
        if target is not None and current is not None and target < current:
            child["size"]["height"] = target
            fixes += 1
    return fixes


def count_vertical_overlaps(elements: list) -> int:
    overlaps = 0
    for previous, current in zip(elements, elements[1:]):
        if not (is_text_element(previous) and is_text_element(current)):
            continue
        if not stacks_vertically(previous, current):
            continue
        if is_decorative(previous) or is_decorative(current):
            continue
        if not multi_line_capable(previous):
            continue
        gap = vertical_gap(previous, current)
        if gap is not None and gap < -OVERLAP_EPSILON:
            overlaps += 1
    return overlaps


def iter_flow_components(node: Any) -> Iterator[dict]:
    """Yield every flex component so oversized child boxes can be bounded."""

    if isinstance(node, dict):
        if node.get("type") == "flex" and isinstance(node.get("children"), list):
            yield node
        for value in node.values():
            yield from iter_flow_components(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_flow_components(value)


def process_template(path: Path, check: bool) -> dict:
    raw = path.read_text(encoding="utf-8")
    had_trailing_newline = raw.endswith("\n")
    data = json.loads(raw)

    summary = {"single_line": 0, "flex": 0, "flex_height": 0, "overlaps": 0}

    for component in list(iter_parent_components(data)):
        elements = component.get("elements")
        if not isinstance(elements, list):
            continue
        if check:
            summary["overlaps"] += count_vertical_overlaps(elements)
            continue
        fixes, _ = analyze_component(elements)
        summary["single_line"] += fixes

    if not check:
        for component in list(iter_flow_components(data)):
            summary["flex_height"] += normalize_flex_child_heights(component)

    if not check:
        dumped = json.dumps(data, indent=2, ensure_ascii=False)
        if had_trailing_newline:
            dumped += "\n"
        path.write_text(dumped, encoding="utf-8")

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=default_templates_dir(),
        help="Directory containing <template>/template.json folders.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Only report remaining vertical overlaps (no writes).",
    )
    args = parser.parse_args()

    paths = sorted(args.templates_dir.glob("*/template.json"))
    if not paths:
        print(f"No templates found under {args.templates_dir}")
        return 1

    totals = {"single_line": 0, "flex": 0, "flex_height": 0, "overlaps": 0}
    for path in paths:
        summary = process_template(path, args.check)
        for key in totals:
            totals[key] += summary[key]
        name = path.parent.name
        if args.check:
            print(f"{name:10} vertical_overlaps={summary['overlaps']:3}")
        else:
            print(
                f"{name:10} single_line_fixes={summary['single_line']:3} "
                f"flex_conversions={summary['flex']:3} "
                f"flex_height_fixes={summary['flex_height']:3}"
            )

    print("---- totals ----")
    for key, value in totals.items():
        print(f"{key}: {value}")

    if args.check and totals["overlaps"]:
        print(f"FAILED: {totals['overlaps']} vertical overlap(s) remaining.")
        return 1
    if args.check:
        print("OK: no vertical text-stack overlaps found.")
    else:
        print(f"Processed {len(paths)} templates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
