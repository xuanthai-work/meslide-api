import re
import unicodedata
from typing import Iterable, List, Optional

from models.presentation_outline_model import (
    PresentationOutlineModel,
    SlideOutlineModel,
)


HEADING_PATTERN = re.compile(r"^\s{0,3}#+\s*(.+)$", re.MULTILINE)
FIRST_SENTENCE_PATTERN = re.compile(r"^\s*([^.?!]+?[.?!])", re.DOTALL)
_TOC_TITLES = frozenset(
    {
        "table of contents",
        "toc",
        "agenda",
        "contents",
        "muc luc",
        "dan y",
        "dan bai",
    }
)
_TOC_TITLE_PREFIXES = (
    "table of contents",
    "muc luc",
)
IMAGE_URL_PATTERN = re.compile(
    r"https?://[-\w./%~:!$&'()*+,;=]+?\.(?:jpe?g|png|webp)(?:\?[^\s\"\'\\]*)?",
    re.IGNORECASE | re.UNICODE,
)


def get_presentation_title_from_presentation_outline(
    presentation_outline: PresentationOutlineModel,
) -> str:
    if not presentation_outline.slides:
        return "Untitled Presentation"

    first_content = presentation_outline.slides[0].content or ""

    if re.match(r"^\s*#{1,6}\s*Page\s+\d+\b", first_content):
        first_content = re.sub(
            r"^\s*#{1,6}\s*Page\s+\d+\b[\s,:\-]*",
            "",
            first_content,
            count=1,
        )

    return (
        first_content[:100]
        .replace("#", "")
        .replace("/", "")
        .replace("\\", "")
        .replace("\n", " ")
    )


def _fold_outline_title(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", without_marks).strip().casefold()


def _is_toc_outline(content: str) -> bool:
    title = _fold_outline_title(_extract_outline_title(content))
    if not title:
        return False
    if title in _TOC_TITLES:
        return True
    return any(title.startswith(prefix) for prefix in _TOC_TITLE_PREFIXES)


def strip_generated_toc_outlines(
    outline: PresentationOutlineModel,
    *,
    title_slide: bool,
) -> PresentationOutlineModel:
    """Drop LLM-authored TOC/agenda slides so the app can insert exactly one."""
    kept: List[SlideOutlineModel] = []
    for index, slide in enumerate(outline.slides):
        if title_slide and index == 0:
            kept.append(slide)
            continue
        if _is_toc_outline(slide.content):
            continue
        kept.append(slide)
    if kept == list(outline.slides):
        return outline
    stripped = outline.model_copy(deep=True)
    stripped.slides = kept
    return stripped


def get_no_of_toc_required_for_n_outlines(
    *,
    n_outlines: int,
    title_slide: bool,
    target_total_slides: Optional[int] = None,
) -> int:
    """Return 0 or 1. Extra TOC pages used to pad long decks are rejected.

    ``target_total_slides`` is accepted for call-site compatibility and does
    not create additional TOC slides.
    """
    _ = target_total_slides
    if n_outlines <= 0:
        return 0
    content_count = (n_outlines - 1) if title_slide else n_outlines
    return 1 if content_count > 0 else 0


def get_no_of_outlines_to_generate_for_n_slides(
    *,
    n_slides: int,
    toc: bool,
    title_slide: bool,
) -> int:
    """How many content outlines to request before the app inserts the TOC.

    Title slides stay in the model output. A table of contents is inserted
    afterwards, so ``toc`` reserves exactly one slot. ``title_slide`` is kept
    because every caller already passes the pair of structure flags.
    """
    if toc:
        return max(0, n_slides - 1)
    return n_slides


def get_presentation_outline_model_with_toc(
    *,
    outline: PresentationOutlineModel,
    n_toc_slides: int,
    title_slide: bool,
) -> PresentationOutlineModel:
    if n_toc_slides <= 0:
        return outline
    n_toc_slides = 1

    outline_with_toc = outline.model_copy(deep=True)
    insertion_index = 1 if title_slide else 0
    if insertion_index < len(outline_with_toc.slides) and _is_toc_outline(
        outline_with_toc.slides[insertion_index].content
    ):
        return outline_with_toc

    existing_outlines = outline_with_toc.slides
    outlines_for_toc = existing_outlines[insertion_index:]
    if not outlines_for_toc:
        return outline_with_toc

    sections = _split_outlines_evenly(outlines_for_toc, n_toc_slides)
    if not sections:
        return outline_with_toc

    toc_slides: List[SlideOutlineModel] = []
    outlines_before_toc = 1 if title_slide else 0
    total_toc_slides = len(sections)
    global_outline_index = 0

    for section_index, section in enumerate(sections):
        section_lines = [
            "## Table of Contents",
            "",
        ]

        for outline in section:
            outline_title = _extract_outline_title(outline.content)
            page_number = (
                outlines_before_toc + total_toc_slides + global_outline_index + 1
            )
            section_lines.append(
                f"- Page number: {page_number}, Title: {outline_title}"
            )
            global_outline_index += 1

        toc_slides.append(
            SlideOutlineModel(
                content="\n".join(
                    line for line in section_lines if line is not None
                ).strip()
            )
        )

    for offset, toc_slide in enumerate(toc_slides):
        existing_outlines.insert(insertion_index + offset, toc_slide)

    return outline_with_toc


def _split_outlines_evenly(
    outlines: Iterable[SlideOutlineModel], n_sections: int
) -> List[List[SlideOutlineModel]]:
    """Split outlines into n contiguous sections with near-equal sizes."""
    outlines_list = list(outlines)
    if n_sections <= 0 or not outlines_list:
        return []

    total = len(outlines_list)
    n_sections = max(1, n_sections)
    base_size = total // n_sections
    remainder = total % n_sections

    sections: List[List[SlideOutlineModel]] = []
    start = 0
    for section_index in range(n_sections):
        current_size = base_size + (1 if section_index < remainder else 0)
        end = start + current_size
        sections.append(outlines_list[start:end])
        start = end

    return sections


def _extract_outline_title(content: str) -> str:
    """Get a human-friendly title from an outline's markdown content."""
    text = content or ""

    heading_match = HEADING_PATTERN.search(text)
    if heading_match:
        return heading_match.group(1).strip()

    sentence_match = FIRST_SENTENCE_PATTERN.search(text.strip())
    if sentence_match:
        return sentence_match.group(1).strip()

    for line in text.splitlines():
        stripped_line = line.strip()
        if stripped_line:
            return stripped_line

    return "Slide"


def get_images_for_slides_from_outline(
    slides: List[SlideOutlineModel],
) -> List[List[str]]:
    """
    Extract image URLs (png, jpg, jpeg, webp) from each slide's content in the outline.

    Args:
        outline: PresentationOutlineModel containing slides with content

    Returns:
        List of lists of image URLs, one list per slide
    """
    result: List[List[str]] = []

    for slide in slides:
        content = slide.content or ""
        image_urls = IMAGE_URL_PATTERN.findall(content)
        # Remove duplicates while preserving order
        unique_urls = list(dict.fromkeys(image_urls))
        result.append(unique_urls)

    return result
