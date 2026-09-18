"""Smart-mode ``<img data-image-query>`` slot parsing and rewriting (Phase 6).

Slot discovery and rewriting use an HTML parser (``html.parser``), never
regular expressions over the whole document: the parser locates each
``<img>`` start tag and its exact source span, and all attribute edits are
scoped to the isolated tag text. This keeps Chart.js scripts, text, and
arbitrary Tailwind markup untouched.

Only the intended ``img[src]`` attribute is replaced; failed slots keep the
placeholder and lose their marker, and every ``data-image-query`` marker is
stripped before the slide HTML is persisted.
"""

import html as html_module
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Optional

# Local placeholder the Smart prompt tells the model to emit.
PLACEHOLDER_SRC = "/static/images/placeholder.jpg"

_IMAGE_QUERY_ATTRIBUTE = "data-image-query"
_SRC_ATTRIBUTE = re.compile(
    r"""\s+src\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
_IMAGE_QUERY_ATTRIBUTE_RE = re.compile(
    r"""\s+data-image-query\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE
)
_IMG_OPEN = re.compile(r"<img\b", re.IGNORECASE)


class SmartHtmlRewriteError(ValueError):
    """Parser offsets did not match the source; keep the original HTML."""


@dataclass(frozen=True)
class SmartImageSlot:
    """One ``<img>`` tag carrying a search query marker."""

    # Absolute character offset of the ``<img`` start tag in the source HTML.
    offset: int
    # The exact start-tag source text (as reported by the parser).
    tag_text: str
    query: str
    alt: Optional[str] = None
    src: Optional[str] = None


class _ImgTagCollector(HTMLParser):
    """Collects every ``<img>`` start tag with its exact source span."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self._line_starts = [0]
        for position, character in enumerate(source):
            if character == "\n":
                self._line_starts.append(position + 1)
        self.img_tags: list[SmartImageSlot] = []

    def handle_starttag(self, tag, attrs):
        if tag != "img":
            return
        line, column = self.getpos()
        attributes: dict[str, Optional[str]] = {}
        for name, value in attrs:
            attributes.setdefault(name.lower(), value)
        self.img_tags.append(
            SmartImageSlot(
                offset=self._line_starts[line - 1] + column,
                tag_text=self.get_starttag_text() or "",
                query=(attributes.get(_IMAGE_QUERY_ATTRIBUTE) or "").strip(),
                alt=attributes.get("alt"),
                src=attributes.get("src"),
            )
        )


def _parse_img_tags(source: str) -> list[SmartImageSlot]:
    collector = _ImgTagCollector(source)
    collector.feed(source)
    collector.close()
    return collector.img_tags


def extract_image_query_slots(source: str) -> list[SmartImageSlot]:
    """All ``<img>`` tags that carry a non-empty data-image-query."""
    return [slot for slot in _parse_img_tags(source) if slot.query]


def _splice(source: str, edits: list[tuple[int, str, str]]) -> str:
    """Apply non-overlapping (offset, old_text, new_text) edits in order."""
    parts: list[str] = []
    last_end = 0
    for offset, old_text, new_text in sorted(edits, key=lambda edit: edit[0]):
        if offset < last_end or source[offset : offset + len(old_text)] != old_text:
            raise SmartHtmlRewriteError("img tag offsets do not match the source")
        parts.append(source[last_end:offset])
        parts.append(new_text)
        last_end = offset + len(old_text)
    parts.append(source[last_end:])
    return "".join(parts)


def _rewrite_img_tag(
    tag_text: str,
    src: Optional[str],
    *,
    strip_query_marker: bool = True,
) -> str:
    """Rewrite one isolated start-tag text: swap src, drop the marker."""
    rewritten = tag_text
    if strip_query_marker:
        rewritten = _IMAGE_QUERY_ATTRIBUTE_RE.sub("", rewritten, count=1)
    if src is not None:
        rewritten = _SRC_ATTRIBUTE.sub("", rewritten, count=1)
        escaped_src = html_module.escape(src, quote=True)
        rewritten = _IMG_OPEN.sub(
            lambda match: f"{match.group(0)} src=\"{escaped_src}\"",
            rewritten,
            count=1,
        )
    return rewritten


def apply_smart_image_resolutions(
    source: str, resolutions: list[tuple[SmartImageSlot, Optional[str]]]
) -> str:
    """Replace only the intended ``img[src]`` attributes.

    ``resolutions`` pairs each slot with its local asset URL; ``None``
    keeps the placeholder (no usable candidate) and still strips the
    marker so nothing query-shaped reaches the database or the browser.
    """
    edits = [
        (slot.offset, slot.tag_text, _rewrite_img_tag(slot.tag_text, url))
        for slot, url in resolutions
    ]
    return _splice(source, edits)


def strip_image_query_markers(source: str) -> str:
    """Remove every data-image-query marker, leaving src untouched."""
    edits = [
        (slot.offset, slot.tag_text, _rewrite_img_tag(slot.tag_text, None))
        for slot in _parse_img_tags(source)
        if slot.query
    ]
    if not edits:
        return source
    return _splice(source, edits)


def neutralize_invented_remote_images(source: str) -> str:
    """Replace invented remote ``img`` srcs with the local placeholder.

    Search mode only: the prompt forbids remote URLs, but the model may
    invent some anyway. Markers are preserved so real slots still resolve.
    """
    edits = []
    for slot in _parse_img_tags(source):
        src = slot.src or ""
        if src.lower().startswith(("http://", "https://", "//")):
            edits.append(
                (
                    slot.offset,
                    slot.tag_text,
                    _rewrite_img_tag(
                        slot.tag_text, PLACEHOLDER_SRC, strip_query_marker=False
                    ),
                )
            )
    if not edits:
        return source
    return _splice(source, edits)