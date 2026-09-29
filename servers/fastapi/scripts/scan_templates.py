"""Report layout-breaking issues across every bundled presentation template.

Companion to ``normalize_all_templates.py``. Exits non-zero when any of the
following remain in ``templates/*/template.json``:

* ``low_line_height``: a font with ``line_height < 1.0``.
* ``negative_letter_spacing``: a font with ``letter_spacing < -2.0``.
* ``huge_font_size``: a font with ``size > 100``.
* ``manual_pos_in_flex``: a flex child pinned with ``__gslide_manual_position``.

Run from the ``gslide-api`` directory::

    python servers/fastapi/scripts/scan_templates.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

LOW_LINE_HEIGHT = 1.0
MIN_LETTER_SPACING = -2.0
HUGE_FONT_SIZE = 100
MANUAL_POSITION_KEY = "__gslide_manual_position"


def default_templates_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "templates"


def read_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def is_font_dict(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if "family" in value:
        return True
    return "line_height" in value or "letter_spacing" in value


def scan(node: Any, counters: dict[str, int], in_flex: bool = False) -> None:
    if isinstance(node, dict):
        if is_font_dict(node):
            line_height = read_number(node.get("line_height"))
            if line_height is not None and line_height < LOW_LINE_HEIGHT:
                counters["low_line_height"] += 1
            letter_spacing = read_number(node.get("letter_spacing"))
            if letter_spacing is not None and letter_spacing < MIN_LETTER_SPACING:
                counters["negative_letter_spacing"] += 1
            size = read_number(node.get("size"))
            if size is not None and size > HUGE_FONT_SIZE:
                counters["huge_font_size"] += 1

        is_flex = node.get("type") == "flex"
        if in_flex and MANUAL_POSITION_KEY in node:
            counters["manual_pos_in_flex"] += 1

        for value in node.values():
            scan(value, counters, in_flex or is_flex)
    elif isinstance(node, list):
        for value in node:
            scan(value, counters, in_flex)


def scan_template(path: Path) -> dict[str, int]:
    data = json.loads(path.read_text(encoding="utf-8"))
    counters = {
        "low_line_height": 0,
        "negative_letter_spacing": 0,
        "huge_font_size": 0,
        "manual_pos_in_flex": 0,
    }
    scan(data, counters)
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--templates-dir",
        type=Path,
        default=default_templates_dir(),
        help="Directory containing <template>/template.json folders.",
    )
    args = parser.parse_args()

    paths = sorted(args.templates_dir.glob("*/template.json"))
    if not paths:
        print(f"No templates found under {args.templates_dir}")
        return 1

    totals = {
        "low_line_height": 0,
        "negative_letter_spacing": 0,
        "huge_font_size": 0,
        "manual_pos_in_flex": 0,
    }

    for path in paths:
        counters = scan_template(path)
        for key, value in counters.items():
            totals[key] += value
        print(
            f"{path.parent.name:10} "
            f"low_line_height={counters['low_line_height']:4} "
            f"negative_letter_spacing={counters['negative_letter_spacing']:4} "
            f"huge_font_size={counters['huge_font_size']:4} "
            f"manual_pos_in_flex={counters['manual_pos_in_flex']:3}"
        )

    print("---- totals ----")
    for key, value in totals.items():
        print(f"{key}: {value}")

    issues = sum(totals.values())
    if issues:
        print(f"FAILED: {issues} issue(s) remaining.")
        return 1
    print("OK: no layout-breaking issues found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
