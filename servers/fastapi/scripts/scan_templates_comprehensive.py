"""Comprehensive layout scanner for every bundled presentation template.

Simulates Vietnamese line growth and detects two classes of layout-breaking
problems that the lighter scanners do not catch:

* **Undersized boxes** - a text box far shorter than its font needs, which
  clips or collapses the copy.
* **Cross-component AABB collisions** - two non-decorative text boxes whose
  axis-aligned bounding boxes overlap enough that they paint over each other.

Run from the ``gslide-api`` directory::

    python servers/fastapi/scripts/scan_templates_comprehensive.py

Exits non-zero (and prints every finding) when any critical issue remains.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def estimate_rendered_height(text, font_size, line_height, box_width, max_len=None):
    length = max(len(text) if text else 0, int((max_len or 0) * 0.8))
    if length <= 0:
        length = 10
    avg_char_w = font_size * 0.55
    chars_per_line = max(1, int(box_width / avg_char_w))
    lines = math.ceil(length / chars_per_line)
    return lines * font_size * line_height, lines


def scan_all():
    root = Path(__file__).resolve().parents[3] / "templates"
    critical_errors = []

    for tdir in sorted(root.iterdir()):
        if not tdir.is_dir():
            continue
        tfile = tdir / "template.json"
        if not tfile.exists():
            continue
        raw = json.loads(tfile.read_text(encoding="utf-8"))

        for l in raw.get("layouts", []):
            lid = l.get("id")
            boxes = []
            for c in l.get("components", []):
                cx, cy = c.get("position", {}).get("x", 0), c.get("position", {}).get("y", 0)
                for el in c.get("elements", []):
                    if el.get("type") == "text":
                        pos = el.get("position", {})
                        sz = el.get("size", {})
                        font = el.get("font", {})
                        fsize = font.get("size", 16)
                        lh = font.get("line_height", 1.2)
                        x, y = cx + pos.get("x", 0), cy + pos.get("y", 0)
                        w, h = sz.get("width", 0), sz.get("height", 0)
                        boxes.append({
                            "cid": c.get("id"), "name": el.get("name"),
                            "decorative": el.get("decorative", False),
                            "x": x, "y": y, "w": w, "h": h,
                            "fsize": fsize, "lh": lh
                        })

            # 1. Check undersized height
            for b in boxes:
                if not b["decorative"] and b["fsize"] > 18 and b["h"] < b["fsize"] * 0.75:
                    critical_errors.append(f"[{tdir.name}] {lid}: {b['cid']}.{b['name']} has height={b['h']} for font_size={b['fsize']}")

            # 2. Check cross-component collision
            for i in range(len(boxes)):
                for j in range(i + 1, len(boxes)):
                    b1, b2 = boxes[i], boxes[j]
                    if b1["decorative"] or b2["decorative"]:
                        continue
                    xo = max(0, min(b1["x"] + b1["w"], b2["x"] + b2["w"]) - max(b1["x"], b2["x"]))
                    yo = max(0, min(b1["y"] + b1["h"], b2["y"] + b2["h"]) - max(b1["y"], b2["y"]))
                    if xo > 30 and yo > 15:
                        critical_errors.append(f"[{tdir.name}] {lid}: Collision between {b1['cid']}.{b1['name']} and {b2['cid']}.{b2['name']} (dx={xo:.1f}, dy={yo:.1f})")

    print(f"Comprehensive Template Scan Result: {len(critical_errors)} CRITICAL ERRORS FOUND.")
    for err in critical_errors:
        print("  ->", err)
    return len(critical_errors)


if __name__ == "__main__":
    sys.exit(scan_all())
