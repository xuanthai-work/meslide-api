"""Raise the character ceilings left on prose fields across every template.

Vietnamese expresses the same message in ~20-40% more characters than English,
so a ``max_length`` tuned for English truncates Vietnamese copy. This script
scans ``templates/*/template.json`` and:

* raises any prose ``max_length <= 15`` to ``max(25, round(max_length * 2))``
  (e.g. ``headline_second_line: 7`` -> 25, ``large_left_heading: 15`` -> 30);
* relaxes a locked ``min_length > 4`` to 3 so ``min == max`` fields stop
  rejecting valid Vietnamese.

Numeric fields (date/page/value/metric/number/index/percent/quote_mark) are left
untouched. Run from the ``gslide-api`` directory::

    python servers/fastapi/scripts/normalize_template_character_limits.py
    python servers/fastapi/scripts/normalize_template_character_limits.py --check
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

MAX_LENGTH_THRESHOLD = 15
RAISED_MIN_MAX_LENGTH = 25
LENGTH_MULTIPLIER = 2.0
MIN_LENGTH_THRESHOLD = 4
RELAXED_MIN_LENGTH = 3

TEXT_NAME_HINTS = (
    "heading",
    "headline",
    "title",
    "subtitle",
    "caption",
    "statement",
    "label",
    "topic",
    "card",
    "summary",
    "copy",
    "body",
    "description",
    "paragraph",
    "content",
    "text",
    "name",
)
NUMERIC_NAME_HINTS = (
    "date",
    "page",
    "pagination",
    "folio",
    "value",
    "metric",
    "number",
    "index",
    "percent",
    "pct",
    "count",
    "code",
    "marker",
    "initial",
    "quote_mark",
)


def default_templates_dir() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "templates"


def is_prose_field(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    if any(hint in lowered for hint in NUMERIC_NAME_HINTS):
        return False
    return any(hint in lowered for hint in TEXT_NAME_HINTS)


def iter_field_dicts(node: Any) -> Iterator[dict]:
    if isinstance(node, dict):
        if "max_length" in node or "min_length" in node:
            yield node
        for value in node.values():
            yield from iter_field_dicts(value)
    elif isinstance(node, list):
        for value in node:
            yield from iter_field_dicts(value)


def normalize_field(field: dict) -> list[str]:
    if not is_prose_field(field.get("name")):
        return []

    changes: list[str] = []
    max_length = field.get("max_length")
    if (
        isinstance(max_length, int)
        and not isinstance(max_length, bool)
        and max_length > 0
        and max_length <= MAX_LENGTH_THRESHOLD
    ):
        new_max = max(RAISED_MIN_MAX_LENGTH, round(max_length * LENGTH_MULTIPLIER))
        changes.append(f"max_length {max_length} -> {new_max}")
        field["max_length"] = new_max

    min_length = field.get("min_length")
    if (
        isinstance(min_length, int)
        and not isinstance(min_length, bool)
        and min_length > MIN_LENGTH_THRESHOLD
    ):
        new_min = min(RELAXED_MIN_LENGTH, min_length)
        changes.append(f"min_length {min_length} -> {new_min}")
        field["min_length"] = new_min

    return changes


def process_template(path: Path, check: bool) -> dict:
    raw = path.read_text(encoding="utf-8")
    had_trailing_newline = raw.endswith("\n")
    data = json.loads(raw)

    summary = {"max_length": 0, "min_length": 0, "remaining_low_max": 0}
    remaining_low_max = 0

    for field in iter_field_dicts(data):
        changes = normalize_field(field) if not check else []
        for change in changes:
            if change.startswith("max_length"):
                summary["max_length"] += 1
            else:
                summary["min_length"] += 1
        if check and is_prose_field(field.get("name")):
            max_length = field.get("max_length")
            if (
                isinstance(max_length, int)
                and not isinstance(max_length, bool)
                and 0 < max_length <= MAX_LENGTH_THRESHOLD
            ):
                remaining_low_max += 1

    summary["remaining_low_max"] = remaining_low_max

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
        help="Only report prose fields still capped at <= 15 (no writes).",
    )
    args = parser.parse_args()

    paths = sorted(args.templates_dir.glob("*/template.json"))
    if not paths:
        print(f"No templates found under {args.templates_dir}")
        return 1

    totals = {"max_length": 0, "min_length": 0, "remaining_low_max": 0}
    for path in paths:
        summary = process_template(path, args.check)
        for key in totals:
            totals[key] += summary[key]
        name = path.parent.name
        if args.check:
            print(f"{name:10} prose_max_length<=15={summary['remaining_low_max']:3}")
        else:
            print(
                f"{name:10} raised_max_length={summary['max_length']:3} "
                f"relaxed_min_length={summary['min_length']:3}"
            )

    print("---- totals ----")
    for key, value in totals.items():
        print(f"{key}: {value}")

    if args.check and totals["remaining_low_max"]:
        print(f"FAILED: {totals['remaining_low_max']} prose field(s) still capped.")
        return 1
    if args.check:
        print("OK: no prose field is capped at <= 15.")
    else:
        print(f"Processed {len(paths)} templates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
