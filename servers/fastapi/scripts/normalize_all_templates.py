"""Normalize every bundled presentation template.

Scans ``templates/*/template.json`` and repairs the four layout-breaking
classes of issues that template authors can introduce:

1. ``line_height < 1.05`` collapses lines on top of each other (especially
   with Vietnamese diacritics). Headings get ``1.15``, body copy gets ``1.3``.
2. Deeply negative ``letter_spacing`` (< -1.5) makes glyphs collide
   horizontally. Headings get ``-0.5``, body copy gets ``0``.
3. Oversized fonts (``size > 90``, e.g. Momentum's 151.11 cover title) are
   pulled back to 64/72. An oversized element that also hangs above the slide
   (``position.y < 0``) is re-seated at ``y = 0`` and its next sibling is
   shifted down by the same amount so the subtitle keeps its spacing.
4. Short ``max_length`` caps on prose headings/labels truncate Vietnamese
   phrases. Names containing heading/title/statement/copy/label are relaxed,
   while numeric/page/date/marker fields are left alone.

It also strips the legacy ``__gslide_manual_position`` flag from flex children.

Run from the ``gslide-api`` directory::

    python servers/fastapi/scripts/normalize_all_templates.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

MIN_LINE_HEIGHT = 1.05
HEADING_LINE_HEIGHT = 1.15
BODY_LINE_HEIGHT = 1.3
HEADING_MIN_SIZE = 24

MIN_LETTER_SPACING = -1.5
HEADING_LETTER_SPACING = -0.5
BODY_LETTER_SPACING = 0.0

MAX_FONT_SIZE = 90
HUGE_FONT_SIZE = 100
HEADING_TARGET_SIZE = 72
LARGE_HEADING_TARGET_SIZE = 64
LARGE_HEADING_THRESHOLD = 120

MAX_HEADLINE_LENGTH = 12
RELAXED_HEADLINE_MIN = 25
MIN_LOWERED_LENGTH = 3

HEADLINE_NAME_HINTS = ("heading", "title", "statement", "copy", "label")
NON_PROSE_NAME_HINTS = (
    "number",
    "num",
    "index",
    "page",
    "date",
    "time",
    "year",
    "month",
    "day",
    "code",
    "count",
    "percent",
    "pct",
    "marker",
)

MANUAL_POSITION_KEY = "__gslide_manual_position"


def default_templates_dir() -> Path:
    # .../gslide-api/servers/fastapi/scripts/normalize_all_templates.py
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "templates"


def is_font_dict(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if "family" in value:
        return True
    return "line_height" in value or "letter_spacing" in value


def read_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def normalize_font(font: dict) -> list[str]:
    """Apply the size/line-height/letter-spacing rules to a single font dict."""

    changes: list[str] = []
    size = read_number(font.get("size"))

    if size is not None and size > MAX_FONT_SIZE:
        new_size = (
            LARGE_HEADING_TARGET_SIZE
            if size >= LARGE_HEADING_THRESHOLD
            else HEADING_TARGET_SIZE
        )
        changes.append(f"size {size} -> {new_size}")
        font["size"] = new_size
        size = float(new_size)

    is_heading = size is not None and size >= HEADING_MIN_SIZE

    line_height = read_number(font.get("line_height"))
    if line_height is not None and line_height < MIN_LINE_HEIGHT:
        target = HEADING_LINE_HEIGHT if is_heading else BODY_LINE_HEIGHT
        changes.append(f"line_height {line_height} -> {target}")
        font["line_height"] = target

    letter_spacing = read_number(font.get("letter_spacing"))
    if letter_spacing is not None and letter_spacing < MIN_LETTER_SPACING:
        target = HEADING_LETTER_SPACING if is_heading else BODY_LETTER_SPACING
        changes.append(f"letter_spacing {letter_spacing} -> {target}")
        font["letter_spacing"] = target

    return changes


def walk_fonts(node: Any) -> Iterable[dict]:
    if isinstance(node, dict):
        if is_font_dict(node):
            yield node
        for value in node.values():
            yield from walk_fonts(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_fonts(value)


def primary_font_size(element: dict) -> float | None:
    size = read_number((element.get("font") or {}).get("size"))
    if size is not None:
        return size
    for run in element.get("runs") or []:
        if isinstance(run, dict):
            run_size = read_number((run.get("font") or {}).get("size"))
            if run_size is not None:
                return run_size
    return None


def fix_oversized_position(element: dict, siblings: list, index: int) -> list[str]:
    """Re-seat an oversized element pulled above the slide and keep the sibling gap."""

    size = primary_font_size(element)
    if size is None or size <= HUGE_FONT_SIZE:
        return []

    position = element.get("position")
    if not isinstance(position, dict):
        return []
    y = read_number(position.get("y"))
    if y is None or y >= 0:
        return []

    delta = -y
    position["y"] = 0
    changes = [f"{element.get('name')}: position.y {y} -> 0"]

    if index + 1 < len(siblings):
        sibling = siblings[index + 1]
        if isinstance(sibling, dict):
            sibling_position = sibling.get("position")
            if isinstance(sibling_position, dict):
                sibling_y = read_number(sibling_position.get("y"))
                if sibling_y is not None:
                    sibling_position["y"] = sibling_y + delta
                    changes.append(
                        f"{sibling.get('name')}: position.y {sibling_y} -> "
                        f"{sibling_y + delta}"
                    )
    return changes


def walk_elements(node: Any) -> Iterable[tuple[dict, list, int]]:
    if isinstance(node, dict):
        for value in node.values():
            yield from walk_elements(value)
    elif isinstance(node, list):
        for index, item in enumerate(node):
            if isinstance(item, dict) and item.get("type") in ("text", "text-list"):
                yield item, node, index
            yield from walk_elements(item)


def relax_max_length(element: dict) -> list[str]:
    name = element.get("name")
    if not isinstance(name, str):
        return []
    lowered = name.lower()
    if not any(hint in lowered for hint in HEADLINE_NAME_HINTS):
        return []
    if any(hint in lowered for hint in NON_PROSE_NAME_HINTS):
        return []

    max_length = element.get("max_length")
    if not isinstance(max_length, int) or isinstance(max_length, bool):
        return []
    if max_length > MAX_HEADLINE_LENGTH:
        return []

    new_max = max(RELAXED_HEADLINE_MIN, int(round(max_length * 2.5)))
    changes = [f"{name}: max_length {max_length} -> {new_max}"]
    element["max_length"] = new_max

    min_length = element.get("min_length")
    if isinstance(min_length, int) and not isinstance(min_length, bool):
        new_min = min(MIN_LOWERED_LENGTH, min_length)
        if new_min != min_length:
            changes.append(f"{name}: min_length {min_length} -> {new_min}")
            element["min_length"] = new_min

    return changes


def strip_manual_position(node: Any, in_flex: bool = False) -> int:
    """Remove ``__gslide_manual_position`` from any descendant of a flex."""

    removed = 0
    if isinstance(node, dict):
        if in_flex and MANUAL_POSITION_KEY in node:
            del node[MANUAL_POSITION_KEY]
            removed += 1
        is_flex = node.get("type") == "flex"
        for value in node.values():
            removed += strip_manual_position(value, in_flex or is_flex)
    elif isinstance(node, list):
        for value in node:
            removed += strip_manual_position(value, in_flex)
    return removed


def normalize_template(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    had_trailing_newline = raw.endswith("\n")
    data = json.loads(raw)

    summary = {"font": 0, "position": 0, "max_length": 0, "manual": 0}

    for element, siblings, index in walk_elements(data):
        summary["position"] += len(fix_oversized_position(element, siblings, index))

    for font in walk_fonts(data):
        summary["font"] += len(normalize_font(font))

    for element, _siblings, _index in walk_elements(data):
        summary["max_length"] += len(relax_max_length(element))

    summary["manual"] = strip_manual_position(data)

    dumped = json.dumps(data, indent=2, ensure_ascii=False)
    if had_trailing_newline:
        dumped += "\n"
    path.write_text(dumped, encoding="utf-8")

    return summary


def resolve_template_paths(templates_dir: Path) -> list[Path]:
    return sorted(templates_dir.glob("*/template.json"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=default_templates_dir(),
        help="Directory containing <template>/template.json folders.",
    )
    args = parser.parse_args()

    paths = resolve_template_paths(args.templates_dir)
    if not paths:
        print(f"No templates found under {args.templates_dir}")
        return 1

    for path in paths:
        summary = normalize_template(path)
        name = path.parent.name
        print(
            f"{name:10} fonts={summary['font']:4} "
            f"positions={summary['position']:3} "
            f"max_length={summary['max_length']:4} "
            f"manual_pos={summary['manual']:3}"
        )

    print(f"Normalized {len(paths)} templates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
