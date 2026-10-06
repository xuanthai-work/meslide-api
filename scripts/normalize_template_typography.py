#!/usr/bin/env python3
"""Normalize template V2 body-copy typography.

Why this exists
---------------
Template V2 fits text with ``fitTextToBox`` (gslide-web/components/slide-editor/
text/template-v2-text-fit.mjs). For a body element the shrink floor is
``max(designedSize * 0.72, 12)`` -- so every designed size up to 16.67 renders at
exactly 12px at worst -- and the fit box is the element's own ``size.height``.
Flex layout never overrides it: an explicit height gives the child ``grow: 0``.

When a description box is too short even for the floor size, ``fitTextToBox``
gives up (``finishOverflowFit`` returns lines that do not fit) and the text node
clips itself (``verticalGlyphClipBounds``), cutting the last line. 156 description
boxes across the fourteen V2 templates clip that way.

Raising the designed size alone makes this *worse*: the floor stays 12px, but the
run now sits in a box that was sized for 10.67px copy. So every font bump has to
ship with a taller box -- that is the whole point of this script.

One trap when measuring by hand: the fitter reads ``font.lineHeight`` and
``font.letterSpacing``, but the template JSON stores ``line_height`` and
``letter_spacing``. Feeding the raw records straight to ``fitTextToBox`` silently
drops both, which over-counts the clipping boxes and invents regressions the
render path (``fontFromRecord``) never sees. The model below therefore reads the
stored snake_case keys directly.

Policy
------
For each description-role element (name contains ``description``) whose designed
font is under ``FONT_TRIGGER``:

1. Measure how tall the copy is at the 12px floor, using the same unit-based
   model the repo's own fitter uses by default (``measureTextByUnits``).
2. Grow the box to that height plus a margin, clamped to the free space its
   parent container can give it. ``group``/``flex`` do not clip on canvas, so a
   box taller than its parent paints over the sibling below -- the clamp matters.
3. Set the designed size to ``MIN_DESCRIPTION_FONT`` and mirror it onto the runs.
4. Bump the font only when the box can actually hold the taller copy. When it
   cannot -- the parent has no room and the box is already too short -- the
   element keeps its current size rather than getting worse, which is the bug we
   are fixing. A box that is *already* tall enough needs no height change and
   takes the larger font on its own.

The pass is idempotent: an element is only touched while its font is below the
trigger, so a second run is a no-op. ``nova`` additionally gets a hand-tuned
override table that also rebalances the flex/group containers holding the taller
boxes.

Containers
----------
Clamping to what the parent already offered left ~140 elements stranded below the
trigger -- the box was short because the container around it was. So when the
clamp says no, a second pass grows the container instead: the flex/group chain
above the text box stretches to hold the copy, siblings parked below slide down,
a card backdrop painted to the group's bottom stretches with it, and the layout
is dropped outright if the result would run off the 1280x720 slide. Heights come
from ``check_template_text_clips.mjs`` -- the regression guard driving the real
``fitTextToBox`` -- rather than the unit model below, which only approximates it.

Usage
-----
    python gslide-api/scripts/normalize_template_typography.py --dry-run
    python gslide-api/scripts/normalize_template_typography.py
    python gslide-api/scripts/normalize_template_typography.py --templates nova,horizon
    python gslide-api/scripts/normalize_template_typography.py --no-grow
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_ROOT = os.path.join(REPO_ROOT, "templates")

#: Element names below this designed size are treated as under-sized body copy.
FONT_TRIGGER = 13.5
#: Smallest designed size body copy is allowed to keep.
MIN_DESCRIPTION_FONT = 14.0
#: Never grow a box by more than this multiple of its current height, even when
#: the parent has room -- a runaway box distorts the layout it was designed in.
MAX_HEIGHT_GROWTH = 2.5
#: Headroom over the measured copy height. Real glyph metrics are wider than the
#: unit model, so a box sized to the model exactly still risks clipping a line.
HEIGHT_MARGIN = 1.08
#: Fraction of the paint-safe width the model may use when deciding whether copy
#: fits. The editor renders through a canvas measure that runs a little wider than
#: the unit model, so a line that lands a hair under the limit here can wrap to
#: one more line there. Measuring against a slightly narrower column makes those
#: knife-edge cases read as "does not fit", which skips the element instead of
#: bumping its font into a box that can no longer hold it.
WIDTH_SAFETY = 0.97
#: Slack on the fit test, in px. Layout numbers come out of summed floats, so an
#: exactly-fitting box can compare as a few ulps short of its own content.
HEIGHT_EPSILON = 0.5

#: Role names that carry body copy. Kept narrow on purpose: headings, values,
#: labels and titles must keep their designed size.
DESCRIPTION_MARKER = "description"

# --- Lead tier ----------------------------------------------------------
#
# The lead paragraph is the largest body copy on a slide and sits next to a
# 60-70px title, so the 14px the description tier settles for reads as a caption
# stranded in a very wide box. The lead tier is set from the box's own width
# instead: a wide box carries more copy per line and can hold the larger size.
#
# Raising a designed size raises the size the fitter may shrink to as well
# (``floorFontPx`` is a ratio of the designed size, not a fixed 12px), and
# ``line_height`` stretches every line. Together those ask for roughly 40% more
# height at 20px than the same copy took at 14.2px, so the pass cannot simply
# swap the font in: it has to re-measure and grow the box, which is why
# ``main`` stages the typography before asking the fitter for heights.

#: Element names that name the lead tier, matched whole. ``summary_label`` is a
#: metric card's caption and ``summary_change`` a delta chip: names that merely
#: contain "summary" are labels by design, not lead copy. The two
#: ``*_description`` names are lead copy too -- they are what a template calls its
#: lead paragraph when it also carries ordinary descriptions -- and the lead pass
#: claims them before the description tier can settle them at 14px.
LEAD_NAMES = (
    "summary",
    "introductory_summary",
    "introductory_description",
    "supporting_description",
    "lead_paragraph",
)
#: Mirrors the guard's ``MEASURED_NAME``. The two must agree or the guard's row
#: indices and this script's measured keys drift apart.
MEASURED_NAME_RE = re.compile(
    r"description|^(summary|introductory_summary|lead_paragraph)$", re.IGNORECASE
)
LEAD_NAME_RE = re.compile(
    "^(%s)$" % "|".join(LEAD_NAMES), re.IGNORECASE
)

#: A lead box narrower than this is not lead copy -- it is a small note.
LEAD_MIN_WIDTH = 350.0
#: Below this height the box is a one- or two-line strip, not a paragraph.
LEAD_MIN_HEIGHT = 80.0
#: Ceiling of the tier the lead pass raises. Anything already larger was set to a
#: lead size deliberately and is left alone; below it, the box is still carrying
#: body type. There is no floor: the guard now measures lead boxes no earlier
#: pass could see, and one of those had drifted below the tier entirely.
LEAD_FONT_MAX = 14.6
#: Width from which a lead box can carry the larger size.
LEAD_WIDE_WIDTH = 550.0
LEAD_WIDE_FONT = 20.0
LEAD_NARROW_FONT = 18.0
#: Lead copy is set looser than the description tier's 1.15 default.
LEAD_LINE_HEIGHT = 1.35

# --- Fit model ----------------------------------------------------------
# Ported from gslide-web/components/slide-editor/text/template-v2-text-fit.mjs
# so this script can size boxes without shelling out to Node. Only the paths the
# fitter uses first (word wrap, then character split) are modelled.

LATIN_EM = 0.5
VIETNAMESE_EM = 0.72
VIETNAMESE_BOLD_MULTIPLIER = 1.16
CJK_EM = 1.0
PAINT_SAFETY = 1.1
#: Mirrors gslide-web/components/slide-editor/text/template-v2-text-fit.mjs. The
#: editor owns this number; the port only follows it, or boxes get planned
#: against a column the renderer never uses.
UNICODE_PAINT_SAFETY = 1.03
SHRINK_RATIO = 0.72
BODY_MIN_PX = 12

VIETNAMESE_RE = re.compile(
    "["
    "àáạảãâầấậẩẫ"
    "ăằắặẳẵèéẹẻẽ"
    "êềếệểễìíịỉĩ"
    "òóọỏõôồốộổỗ"
    "ơờớợởỡùúụủũ"
    "ưừứựửữỳýỵỷỹ"
    "đ"
    "ÀÁẠẢÃÂẦẤẬẨẪ"
    "ĂẰẮẶẲẴÈÉẸẺẼ"
    "ÊỀẾỆỂỄÌÍỊỈĨ"
    "ÒÓỌỎÕÔỒỐỘỔỖ"
    "ƠỜỚỢỞỠÙÚỤỦŨ"
    "ƯỪỨỰỬỮỲÝỴỶỸ"
    "Đ"
    "]"
)
CJK_RE = re.compile("[　-鿿가-힯豈-﫿]")


def character_unit(char: str, bold: bool = False) -> float:
    if not char:
        return 0.0
    if unicodedata.combining(char):
        return 0.0
    if CJK_RE.match(char):
        return CJK_EM
    vietnamese = bool(VIETNAMESE_RE.match(char))
    unit = VIETNAMESE_EM if vietnamese else LATIN_EM
    return unit * VIETNAMESE_BOLD_MULTIPLIER if (bold and vietnamese) else unit


def measure_text(text: str, font: dict) -> float:
    size = font.get("size") if isinstance(font.get("size"), (int, float)) else 18.0
    if size <= 0:
        size = 18.0
    spacing = font.get("letter_spacing")
    spacing = float(spacing) if isinstance(spacing, (int, float)) else 0.0
    chars = list(text or "")
    units = sum(character_unit(ch, bool(font.get("bold"))) for ch in chars)
    return units * size + spacing * max(0, len(chars) - 1)


def paint_safe_width(width: float, text: str) -> float:
    safety = UNICODE_PAINT_SAFETY if any(ord(ch) > 127 for ch in text) else PAINT_SAFETY
    return width / safety


def split_oversized(text: str, font: dict, max_width: float):
    """Break a word that cannot fit on a line of its own."""
    if not text or max_width <= 0 or measure_text(text, font) <= max_width:
        return [(text, measure_text(text, font))]
    chars = list(text)
    pieces = []
    start = 0
    while start < len(chars):
        low, high = start + 1, len(chars)
        best_end = low
        while low <= high:
            mid = (low + high) // 2
            width = measure_text("".join(chars[start:mid]), font)
            if width <= max_width or mid == start + 1:
                best_end = mid
                low = mid + 1
            else:
                high = mid - 1
        pieces.append(
            ("".join(chars[start:best_end]), measure_text("".join(chars[start:best_end]), font))
        )
        start = best_end
    return pieces


TOKEN_RE = re.compile(r"\n|[^\S\n]+|[^\s]+")


def layout_height(runs, width: float) -> float:
    """Total painted height of ``runs`` wrapped into ``width``."""
    lines = []
    current = []
    line_width = 0.0

    def flush():
        nonlocal current, line_width
        if current:
            lines.append(current)
        current = []
        line_width = 0.0

    for run in runs:
        font = run.get("font") or {}
        for part in TOKEN_RE.findall(str(run.get("text") or "")):
            if part == "\n":
                flush()
                continue
            if part.strip() == "":
                remaining = width - line_width
                if remaining <= 0:
                    continue
                piece = split_oversized(part, font, remaining)[0]
                if not piece or piece[1] > remaining + 0.01:
                    continue
                current.append((part, font, piece[1]))
                line_width += piece[1]
                continue
            for text, piece_width in split_oversized(part, font, width):
                if line_width > 0 and line_width + piece_width > width:
                    flush()
                current.append((text, font, piece_width))
                line_width += piece_width
    flush()

    total = 0.0
    for line in lines:
        total += max(
            1.0,
            *[
                (font.get("size") or 18.0)
                * (
                    font.get("line_height")
                    if isinstance(font.get("line_height"), (int, float))
                    else 1.15
                )
                for _, font, _ in line
            ],
        )
    return total


def floor_px(designed_size: float) -> float:
    """Smallest px ``fitTextToBox`` will shrink a body run to."""
    size = designed_size if designed_size > 0 else 18.0
    return max(size * SHRINK_RATIO, BODY_MIN_PX)


def scaled_runs(runs, scale: float):
    out = []
    for run in runs:
        font = dict(run.get("font") or {})
        size = font.get("size")
        if isinstance(size, (int, float)) and size > 0:
            font["size"] = size * scale
        out.append({**run, "font": font})
    return out


def required_height(runs, width: float) -> float:
    """Height the copy occupies at the shrink floor -- i.e. what must fit."""
    designed = max(
        (r.get("font", {}).get("size") or 18.0 for r in runs if isinstance(r, dict)),
        default=18.0,
    )
    text = "".join(str(r.get("text") or "") for r in runs)
    scale = floor_px(designed) / designed
    column = paint_safe_width(width, text) * WIDTH_SAFETY
    return layout_height(scaled_runs(runs, scale), column)


# --- Geometry helpers ---------------------------------------------------

def _read_size(node):
    size = node.get("size")
    return size if isinstance(size, dict) else None


def _height_of(node):
    size = _read_size(node)
    value = size.get("height") if size else None
    return float(value) if isinstance(value, (int, float)) else None


def _position_y(node):
    position = node.get("position")
    value = position.get("y") if isinstance(position, dict) else None
    return float(value) if isinstance(value, (int, float)) else None


def _flex_children(parent):
    return [c for c in (parent.get("children") or []) if isinstance(c, dict)]


def free_height(parent, element):
    """Vertical room ``element`` may occupy without leaving ``parent``.

    ``None`` when the parent does not constrain its children (a component has no
    declared size, and neither does the layout root).
    """
    if not isinstance(parent, dict):
        return None
    parent_height = _height_of(parent)
    if parent_height is None or parent_height <= 0:
        return None

    if parent.get("type") == "flex" and _flex_children(parent):
        if parent.get("direction") == "row":
            # Children sit side by side, so a taller one does not push the others
            # down: it may use the row's whole height.
            return parent_height - (_position_y(element) or 0.0)
        gap = parent.get("gap")
        gap = float(gap) if isinstance(gap, (int, float)) else 0.0
        siblings = _flex_children(parent)
        occupied = sum(h for h in (_height_of(c) for c in siblings) if h is not None)
        return (
            parent_height
            - (occupied + gap * max(0, len(siblings) - 1))
            + (_height_of(element) or 0.0)
        )

    return parent_height - (_position_y(element) or 0.0)


# --- Overrides ----------------------------------------------------------

#: Hand-tuned overrides for Nova. Layouts 7/8/24 were measured against the real
#: fitter; the container numbers are the largest value that still clears the
#: parent group/flex, so the copy never paints over the element underneath.
#: ``height_by_index`` lists one value per occurrence, in document order.
NOVA_OVERRIDES = {
    "split_header_with_alternating_timeline_steps_8416": {
        "text": {
            # 26 keeps the heading a single-line box; width, not height, shrinks it.
            "step_heading": {"font": 18.0, "height": 26.0},
            # 5 timeline steps. Steps 2/4/5 sit in shorter parent groups, so
            # their step_copy cannot grow past 152.57.
            "step_description": {
                "font": 14.0,
                "height_by_index": [115.0, 108.0, 115.0, 108.0, 108.0],
            },
            # ``summary`` is left alone. The fitter renders its copy 110.06px tall
            # in the box it was designed with (218.78), so lifting it to 240 only
            # pushed the box 2.7px into the step nodes below it.
        },
        "flex": {
            "step_copy": {"height_by_index": [160.0, 152.57, 160.0, 152.57, 152.57]},
        },
    },
    "title_summary_with_icon_cards_8298": {
        "text": {
            "card_heading": {"font": 20.0, "height": 28.0},
            "card_description": {"font": 14.5, "height": 140.0},
        },
        # Card 2 is the one card whose content is a plain group rather than a
        # flex column, so its description box has to be lifted to make room.
        "group": {
            "card_content_item": {"height": 140.0, "y": 137.01},
        },
        # The other three cards are flex columns, and the 140px description box
        # above overflows the column that holds it: icon + heading + copy + two
        # gaps is 339.73 / 317.05, not the 277.01 / 293.01 the card shipped with.
        # Without this the copy paints over the card's footer. Indices count
        # every ``card_content`` in the layout, flex and group alike, so the two
        # flex cards that share a gap of 51.19 fall through to the last value.
        "flex": {
            "card_content": {"height_by_index": [339.73, 317.05]},
        },
    },
    "split_header_with_metric_cards_and_detail_columns_2954": {
        "text": {
            "detail_heading": {"font": 20.0, "height": 28.0},
            # Column 1 is 165.52 tall; columns 2-3 are 181.52.
            "detail_description": {
                "font": 14.0,
                "height_by_index": [125.0, 140.0, 140.0],
            },
        },
    },
}


#: The same hand-tuning for the other templates, kept in its own table so a
#: layout id shared across templates cannot silently pick up the wrong spec.
#: ``TEMPLATE_OVERRIDES`` below is what the walk reads.
LANDMARK_OVERRIDES = {
    "headered_intro_with_staggered_feature_cards_8787": {
        "text": {
            # The intro column is 195.98 tall and leaves this summary 118.21 of
            # room, but the box shipped at 80: three lines at the 20px the lead
            # pass gives it need 108, so the copy was shrinking to 15.92 in a box
            # with space enough to show it at the size it was designed for.
            "summary": {"font": 20.0, "line_height": 1.35, "height": 108.0},
        },
    },
}

TEMPLATE_OVERRIDES = {"nova": NOVA_OVERRIDES, "landmark": LANDMARK_OVERRIDES}


# --- Element mutation ---------------------------------------------------

def apply_text_font(element, new_font):
    """Set the element font and mirror the change onto its runs.

    Returns True only when a size actually moved, so re-running the pass over an
    already-normalized template reports nothing.
    """
    font = element.get("font")
    if not isinstance(font, dict):
        return False
    old_size = font.get("size")
    if not isinstance(old_size, (int, float)):
        return False
    changed = False
    if abs(old_size - new_font) > 1e-6:
        font["size"] = new_font
        changed = True
    for run in element.get("runs") or []:
        run_font = run.get("font") if isinstance(run, dict) else None
        if not isinstance(run_font, dict):
            continue
        size = run_font.get("size")
        if (
            isinstance(size, (int, float))
            and abs(size - old_size) < 1e-6
            and abs(size - new_font) > 1e-6
        ):
            run_font["size"] = new_font
            changed = True
    return changed


def apply_line_height(element, new_line_height):
    """Set the element's line height, mirroring it onto the runs that carry one.

    Runs hold their own font record and the fitter prefers it over the element's,
    so a run left at the old line height would keep the old leading no matter what
    the element says.
    """
    font = element.get("font")
    if not isinstance(font, dict):
        return False
    old = font.get("line_height")
    if not isinstance(old, (int, float)):
        old = font.get("lineHeight")
    changed = False
    if not isinstance(old, (int, float)) or abs(old - new_line_height) > 1e-6:
        font["line_height"] = new_line_height
        # A camelCase twin would win the lookup in the render path.
        font.pop("lineHeight", None)
        changed = True
    for run in element.get("runs") or []:
        run_font = run.get("font") if isinstance(run, dict) else None
        if not isinstance(run_font, dict):
            continue
        run_line_height = run_font.get("line_height")
        if not isinstance(run_line_height, (int, float)):
            run_line_height = run_font.get("lineHeight")
        if not isinstance(run_line_height, (int, float)):
            # The run inherits the element's leading, which has just moved.
            continue
        if abs(run_line_height - new_line_height) > 1e-6:
            run_font["line_height"] = new_line_height
            run_font.pop("lineHeight", None)
            changed = True
    return changed


def set_height(element, value):
    size = _read_size(element)
    if size is None:
        return False
    current = size.get("height")
    if isinstance(current, (int, float)) and abs(current - value) < 1e-6:
        return False
    size["height"] = round(float(value), 2)
    return True


def apply_override(element, spec, index):
    changed = []
    if "font" in spec and apply_text_font(element, float(spec["font"])):
        changed.append(f"font->{spec['font']}")
    height = spec.get("height")
    if height is None and "height_by_index" in spec:
        values = spec["height_by_index"]
        height = values[index] if index < len(values) else values[-1]
    if height is not None and set_height(element, height):
        changed.append(f"h->{height}")
    if "line_height" in spec and apply_line_height(element, float(spec["line_height"])):
        changed.append(f"lh->{spec['line_height']}")
    if "y" in spec:
        value = round(float(spec["y"]), 2)
        position = element.setdefault("position", {})
        if position.get("y") != value:
            position["y"] = value
            changed.append(f"y->{spec['y']}")
    return changed


def _lead_target(element):
    """Size the lead pass sets for ``element``, or ``None`` when it owns it not.

    Also answers "does this box still owe the growth pass a look": an element
    already at a lead size returns that size, so a re-run keeps measuring it
    instead of quietly dropping it from the candidate list.
    """
    if element.get("type") != "text" or not _is_lead_text(element):
        return None
    size = _read_size(element) or {}
    width, height = size.get("width"), size.get("height")
    if not isinstance(width, (int, float)) or width < LEAD_MIN_WIDTH:
        return None
    if not isinstance(height, (int, float)) or height < LEAD_MIN_HEIGHT:
        return None
    current = _designed_size(element)
    if 0 < current <= LEAD_FONT_MAX:
        return LEAD_WIDE_FONT if width >= LEAD_WIDE_WIDTH else LEAD_NARROW_FONT
    if current in (LEAD_NARROW_FONT, LEAD_WIDE_FONT):
        # Already the pass's size, and the box under it was sized for that size.
        return current
    return None


def normalize_lead(element):
    """Set the lead paragraph's size from the width of the box that holds it.

    Height is left alone here. Raising the designed size raises the floor the
    fitter may shrink to as well, so how tall the box now has to be is a question
    only the real fitter can answer -- ``grow_layout`` asks it once the typography
    is staged and takes the answer.
    """
    target = _lead_target(element)
    if target is None:
        return []

    changed = []
    if apply_text_font(element, target):
        changed.append(f"font->{target}")
    if apply_line_height(element, LEAD_LINE_HEIGHT):
        changed.append(f"lh->{LEAD_LINE_HEIGHT}")
    return changed


def normalize_generic(element, parent):
    """Raise under-sized description copy when its box can be grown to suit."""
    name = element.get("name")
    if not isinstance(name, str) or DESCRIPTION_MARKER not in name.lower():
        return []
    if element.get("type") != "text":
        return []
    if _is_lead_text(element):
        # Lead copy is the lead pass's to size; letting this one also run would
        # settle it back at the 14px description tier.
        return []

    font = element.get("font")
    size = font.get("size") if isinstance(font, dict) else None
    if not isinstance(size, (int, float)) or not 0 < size < FONT_TRIGGER:
        return []

    current = _height_of(element)
    width = _read_size(element) or {}
    width = width.get("width")
    runs = [r for r in (element.get("runs") or []) if isinstance(r, dict)]
    if current is None or not isinstance(width, (int, float)) or width <= 0 or not runs:
        return []

    need = required_height(runs, float(width)) * HEIGHT_MARGIN
    room = free_height(parent, element)

    # Tallest the box may become: its own grown need, the growth cap, and
    # whatever the parent can host. A parent that is already overflowing its own
    # content (room < current) may only keep the child at the size it has --
    # shrinking it would reshuffle a layout nobody asked to change.
    target = min(max(current, need), current * MAX_HEIGHT_GROWTH)
    if room is not None:
        target = min(target, max(current, room))

    if need > target + HEIGHT_EPSILON:
        # Nowhere to put the taller copy. Bumping the font here would newly clip
        # it, which is the bug this script exists to fix, so the element keeps
        # its current size -- a box that is already tall enough passes this test
        # unchanged and just gets the larger font.
        return []

    changed = []
    if apply_text_font(element, MIN_DESCRIPTION_FONT):
        changed.append(f"font->{MIN_DESCRIPTION_FONT}")
    if set_height(element, target):
        changed.append(f"h->{round(target, 2)}")
    return changed


# --- Container growth ---------------------------------------------------
#
# ``normalize_generic`` gives up when the parent has no room for the taller copy,
# which is how ~140 descriptions across the V2 templates ended up stranded below
# 14px. Most of them are not short of canvas; they are short of *container* -- a
# flex column sized exactly to its contents, or a card group whose box ends where
# its copy ends. This pass makes the room instead of waiting for it.
#
# Growth walks outward from the text box. A flex parent restacks, so a taller
# child simply makes it taller. A group or container positions its children
# absolutely, so a taller child can run out the bottom of the group: the group
# grows with it, anything parked below slides down, and a sibling painted to the
# group's own bottom -- the card backdrop behind the copy -- stretches to keep
# covering the box it belongs to. Siblings that merely share a row are left
# alone; that overlap is the look these layouts are built from. Growth stops at
# the component, which declares no size, and the whole layout is dropped if the
# result would leave the slide.

CANVAS_HEIGHT = 720.0
#: Column the plan is measured against. Mirrors ``heightAtFloor``'s ``widthSafety``
#: in the guard: the canvas measures glyphs a little wider than the fitter's unit
#: model, so a box planned to the last fraction of a line can still wrap on screen.
PLAN_WIDTH_SAFETY = 0.97
#: The guard drives the real fitter; this script only approximates it.
GUARD_SCRIPT = os.path.join(REPO_ROOT, "scripts", "check_template_text_clips.mjs")


def load_fitter_needs(templates, width_safety=PLAN_WIDTH_SAFETY, root=None):
    """Height each measured box must hold, measured by the real fitter.

    Keyed ``(template, "<layout>|<name>|<index>")`` to match the guard's rows.
    Returns ``None`` when the measurement cannot be taken, so callers can fall
    back to the unit model instead of writing half-checked numbers.

    ``root`` measures a staged copy instead of the templates on disk. The lead
    pass needs it: a height is only meaningful against the fonts that will ship,
    and raising a designed size also raises the size the fitter may shrink to.
    """
    command = [
        "node",
        GUARD_SCRIPT,
        "--json",
        "--templates",
        ",".join(templates),
        "--width-safety",
        str(width_safety),
    ]
    if root is not None:
        command += ["--root", root]
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        payload = json.loads(completed.stdout)
    except (OSError, ValueError) as error:
        print(f"  warning: fitter measurement unavailable ({error}); containers left alone")
        return None

    needs = {}
    for template, rows in (payload.get("perTemplate") or {}).items():
        for row in rows:
            if row.get("need") is not None:
                needs[(template, row["key"])] = float(row["need"])
    return needs


def _child_key(node):
    """Key holding ``node``'s children, or ``None`` when it has none.

    Groups, flex rows and containers nest under ``children``; components -- which
    carry an id but no type -- and the layout root use ``elements``/``components``.
    """
    if node.get("type") in ("group", "flex", "container"):
        return "children" if isinstance(node.get("children"), list) else None
    for key in ("components", "elements", "children"):
        if isinstance(node.get(key), list):
            return key
    return None


def _children_of(node):
    key = _child_key(node)
    if not key:
        return []
    return [c for c in node[key] if isinstance(c, dict)]


def _x_span(node):
    """Node's horizontal extent, in its parent's coordinates."""
    width = (_read_size(node) or {}).get("width")
    if not isinstance(width, (int, float)):
        return None
    position = node.get("position") if isinstance(node.get("position"), dict) else {}
    x = position.get("x")
    x = float(x) if isinstance(x, (int, float)) else 0.0
    return (x, x + float(width))


def _overlaps_x(one, other):
    """Whether two siblings can collide. Errs towards yes when geometry is absent."""
    first, second = _x_span(one), _x_span(other)
    if first is None or second is None:
        return True
    return min(first[1], second[1]) - max(first[0], second[0]) > 0.5


def _stacked_height(node):
    """Height a flex needs for its children: they sum in a column, max in a row.

    A child with no declared size -- a vector, an infographic -- still opens a gap
    before it, so the gap count is the whole child list, not the measurable part.
    Counting only the measured children reads a full column as having room and
    leaves the copy it was supposed to make space for still clipped.
    """
    kids = _children_of(node)
    heights = [h for h in (_height_of(k) for k in kids) if h is not None]
    if not heights:
        return 0.0
    if node.get("direction") == "row":
        return max(heights)
    gap = node.get("gap")
    gap = float(gap) if isinstance(gap, (int, float)) else 0.0
    return sum(heights) + gap * max(0, len(kids) - 1)


def content_height(node):
    """Lowest edge of anything inside ``node``, in the node's own coordinates."""
    if node.get("type") == "flex":
        return _stacked_height(node)
    return max(
        ((_position_y(k) or 0.0) + (_height_of(k) or 0.0) for k in _children_of(node)),
        default=0.0,
    )


def _max_bottom(node, offset=0.0):
    """Lowest edge of ``node`` and everything under it, in layout coordinates."""
    top = offset + (_position_y(node) or 0.0)
    bottom = top + (_height_of(node) or 0.0)
    for child in _children_of(node):
        bottom = max(bottom, _max_bottom(child, top))
    return bottom


def _layout_bottom(layout):
    return max((_max_bottom(c) for c in _children_of(layout)), default=0.0)


def _reflow_absolute(parent, child, child_before, was, now):
    """Keep absolutely-positioned siblings in step with a grown child.

    ``child`` grew from ``child_before`` to its current height inside ``parent``,
    which grew from ``was`` to ``now``. A sibling parked entirely below the child
    slides down with it; a sibling painted to the parent's old bottom is the card
    backdrop behind the copy, and stretches to keep covering it.
    """
    child_y = _position_y(child) or 0.0
    old_bottom = child_y + child_before
    slide = child_y + (_height_of(child) or 0.0) - old_bottom

    for sibling in _children_of(parent):
        if sibling is child:
            continue
        height = _height_of(sibling)
        if height is None or not _overlaps_x(sibling, child):
            continue
        y = _position_y(sibling) or 0.0
        if y >= old_bottom - 0.5:
            position = sibling.setdefault("position", {})
            if isinstance(position, dict):
                position["y"] = round(y + slide, 2)
        elif y <= 0.5 and y + height >= was - 2.0:
            set_height(sibling, now - y)


def grow_for(element, target, ancestors, log):
    """Grow ``element`` to ``target`` and lift every ancestor that must follow.

    ``ancestors`` runs outward from the element's immediate parent, and the first
    one that already has room ends the walk -- everything beyond it is unaffected.
    """
    before = _height_of(element)
    if before is None or target <= before + HEIGHT_EPSILON:
        return
    set_height(element, target)

    child, child_before = element, before
    for parent in ancestors:
        required = content_height(parent)
        parent_height = _height_of(parent)
        if parent_height is None:
            # An auto-sized ancestor has no box to enlarge, but its own content is
            # now taller -- the growth still has to reach whatever holds it.
            child, child_before = parent, required
            continue
        if required <= parent_height + HEIGHT_EPSILON:
            break  # this ancestor already has the room
        set_height(parent, required)
        if parent.get("type") != "flex":
            _reflow_absolute(parent, child, child_before, parent_height, required)
            slipped = content_height(parent)
            if slipped > required + HEIGHT_EPSILON:
                set_height(parent, slipped)  # a sibling slid past the new bottom
        log.append((parent, parent_height, _height_of(parent)))
        child, child_before = parent, parent_height


def _designed_size(element):
    """The element's designed font size, as the render path defaults it."""
    font = element.get("font")
    raw = font.get("size") if isinstance(font, dict) else None
    return float(raw) if isinstance(raw, (int, float)) else 18.0


def _is_description_text(element):
    """Whether ``element`` belongs to the description tier."""
    if element.get("type") != "text":
        return False
    name = element.get("name")
    return isinstance(name, str) and DESCRIPTION_MARKER in name.lower()


def _is_lead_text(element):
    """Whether ``element`` is a lead paragraph, matched by whole name."""
    if element.get("type") != "text":
        return False
    name = element.get("name")
    return isinstance(name, str) and LEAD_NAME_RE.match(name) is not None


def _is_measured_text(element):
    """Whether the guard numbers ``element`` -- its filter, mirrored.

    Indices are counted per layout and per name, so this only has to agree with
    the guard about which names it sees; skipping a name the guard numbers would
    shift every later index for that name only.
    """
    if element.get("type") != "text":
        return False
    name = element.get("name")
    return isinstance(name, str) and MEASURED_NAME_RE.search(name) is not None


def _is_starved_description(element):
    """A description the typography pass had to leave below the trigger."""
    return _is_description_text(element) and 0 < _designed_size(element) < FONT_TRIGGER


def _measured_key(counters, layout_id, element):
    """Reproduce the guard's per-layout index for a description element.

    The guard numbers only the elements it actually measures, so the count has to
    sit behind the same filters -- otherwise the two walks drift apart on the
    first malformed node and every key after it names the wrong element.
    """
    name = element.get("name")
    size = _read_size(element) or {}
    width, height = size.get("width"), size.get("height")
    if not isinstance(width, (int, float)) or width <= 0:
        return None
    if not isinstance(height, (int, float)) or height <= 0:
        return None
    if _designed_size(element) <= 0:
        return None
    if not [r for r in (element.get("runs") or []) if isinstance(r, dict)]:
        return None

    key = (layout_id, name)
    index = counters.get(key, 0)
    counters[key] = index + 1
    return f"{layout_id}|{name}|{index}"


def grow_layout(template, layout, layout_id, candidates, needs, report):
    """Make room for the copy whose parent container had none."""
    before_bottom = _layout_bottom(layout)
    backup = copy.deepcopy(layout)
    grown = []

    for element, measured, ancestors, kind in candidates:
        need = needs.get((template, measured))
        current = _height_of(element)
        if need is None or current is None or need <= current + HEIGHT_EPSILON:
            continue
        log = []
        grow_for(element, round(need, 2), ancestors, log)
        if not log and kind == "description":
            # No container had to move. That means the room was there all along and
            # the typography pass should have taken it -- leave the box as it was
            # rather than ship a taller box with the same small type in it.
            #
            # A lead box is the other way round: its parent hosting the taller copy
            # is exactly the outcome wanted, and reverting would put the copy back
            # into a box too short for the size it was just given.
            set_height(element, current)
            continue
        if kind == "description":
            apply_text_font(element, MIN_DESCRIPTION_FONT)
        grown.append((element, log, kind))

    if not grown:
        return 0

    ceiling = max(before_bottom, CANVAS_HEIGHT)
    after_bottom = _layout_bottom(layout)
    if after_bottom > ceiling + 0.5:
        # Leaving the slide is worse than small type: put the layout back.
        layout.clear()
        layout.update(copy.deepcopy(backup))
        report.append(
            (
                layout_id,
                "layout",
                "container",
                [f"SKIPPED: grew to y={round(after_bottom, 1)} past {round(ceiling, 1)}"],
            )
        )
        return 0

    for element, log, kind in grown:
        note = (
            f"font->{MIN_DESCRIPTION_FONT}"
            if kind == "description"
            else f"lead font->{_designed_size(element)}"
        )
        report.append(
            (
                layout_id,
                element.get("name"),
                "text",
                [note, f"h->{round(_height_of(element), 2)}"]
                + [f"{p.get('name')} {round(w, 1)}->{round(n, 1)}" for p, w, n in log],
            )
        )
    return len(grown)


def walk(node, parent, chain, overrides, counters, layout_id, report, candidates, measured):
    if isinstance(node, list):
        for child in node:
            walk(child, parent, chain, overrides, counters, layout_id, report, candidates, measured)
        return
    if not isinstance(node, dict):
        return

    if node.get("type"):
        name = node.get("name")
        spec = overrides.get(node.get("type"), {}).get(name) if isinstance(name, str) else None
        changed = []
        if spec:
            key = (layout_id, name)
            index = counters.get(key, 0)
            counters[key] = index + 1
            changed = apply_override(node, spec, index)
        elif _is_lead_text(node):
            changed = normalize_lead(node)
        else:
            changed = normalize_generic(node, parent)
        if _is_measured_text(node):
            # Advance the index for every element the guard numbers, whether this
            # pass touched it or not, so the keys stay aligned with its rows.
            measured_key = _measured_key(measured, layout_id, node)
            if measured_key is not None:
                if _lead_target(node) is not None:
                    # The lead pass sets type only. Its box still has to be sized
                    # for that type, and only the fitter knows by how much.
                    candidates.append((node, measured_key, chain, "lead"))
                elif not changed and _is_starved_description(node):
                    # Give the growth pass a chance to make the room this element
                    # lacks.
                    candidates.append((node, measured_key, chain, "description"))
        if changed:
            report.append((layout_id, name, node.get("type"), changed))

    next_parent = node if node.get("type") in ("group", "flex", "container") else parent
    next_chain = [node] + chain
    for key in ("components", "elements", "children"):
        if key in node:
            walk(node[key], next_parent, next_chain, overrides, counters, layout_id, report, candidates, measured)


def write_template(path, data, original):
    """Write ``data`` the way the rest of the repository stores templates."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    if original.endswith("\n"):
        text += "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def normalize_template(
    template, dry_run=False, needs=None, staging_root=None, width_safety=PLAN_WIDTH_SAFETY
):
    path = os.path.join(TEMPLATES_ROOT, template, "template.json")
    if not os.path.isfile(path):
        return None

    with open(path, encoding="utf-8") as handle:
        original = handle.read()
    data = json.loads(original)

    report = []
    counters = {}
    measured = {}
    grown = 0
    pending = []
    for layout in data.get("layouts", []):
        layout_id = layout.get("id")
        overrides = TEMPLATE_OVERRIDES.get(template, {}).get(layout_id, {})
        candidates = []
        walk(
            layout.get("components", []),
            None,
            [],
            overrides,
            counters,
            layout_id,
            report,
            candidates,
            measured,
        )
        pending.append((layout, layout_id, candidates))

    # Typography first, then measure, then grow. A height asked for before the
    # fonts moved would be the height for the old, smaller type.
    staged = needs is None and staging_root is not None and any(c for _, _, c in pending)
    if staged:
        staged_path = os.path.join(staging_root, template, "template.json")
        os.makedirs(os.path.dirname(staged_path), exist_ok=True)
        write_template(staged_path, data, original)
        needs = load_fitter_needs([template], width_safety, root=staging_root)

    for layout, layout_id, candidates in pending:
        if candidates and needs:
            grown += grow_layout(template, layout, layout_id, candidates, needs, report)

    changed = [entry for entry in report if not str(entry[3][0]).startswith("SKIPPED")]
    if not changed:
        return (template, 0, report)

    if not dry_run:
        write_template(path, data, original)

    return (template, len(changed), report)


V2_TEMPLATES = [
    "civic",
    "dynamic",
    "editorial",
    "executive",
    "horizon",
    "landmark",
    "modern",
    "momentum",
    "mosaic",
    "nova",
    "pulse",
    "signal",
    "swift",
    "verdant",
]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--templates",
        help="Comma-separated template names (default: every V2 template).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the changes without writing them.",
    )
    parser.add_argument(
        "--no-grow",
        action="store_true",
        help="Skip the container-growth pass and only rebalance typography.",
    )
    parser.add_argument(
        "--width-safety",
        type=float,
        default=PLAN_WIDTH_SAFETY,
        help="Column the growth plan is measured against (default: %(default)s).",
    )
    args = parser.parse_args(argv)

    templates = (
        [t.strip() for t in args.templates.split(",") if t.strip()]
        if args.templates
        else list(V2_TEMPLATES)
    )
    unknown = [t for t in templates if t not in V2_TEMPLATES]
    if unknown:
        parser.error(f"unknown template(s): {', '.join(unknown)}")

    # Each template that has work to size is written to a scratch copy first, so
    # the fitter can be asked about the fonts that will actually ship.
    staging_root = None if args.no_grow else tempfile.mkdtemp(prefix="template-typography-")

    total = 0
    try:
        for template in templates:
            result = normalize_template(
                template,
                dry_run=args.dry_run,
                staging_root=staging_root,
                width_safety=args.width_safety,
            )
            if result is None:
                print(f"  {template:12s} SKIP (no template.json)")
                continue
            _, count, report = result
            total += count
            print(f"  {template:12s} {count} element(s) updated")
            for layout_id, name, element_type, changed in report:
                print(f"       {str(layout_id)[:52]:52s} {str(name):28s} {','.join(changed)}")
    finally:
        if staging_root is not None:
            shutil.rmtree(staging_root, ignore_errors=True)

    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {total} element(s) across {len(templates)} template(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
