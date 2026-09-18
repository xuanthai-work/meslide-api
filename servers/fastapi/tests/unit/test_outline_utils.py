import pytest
from pydantic import ValidationError

from constants.presentation import MAX_NUMBER_OF_SLIDES, MAX_OUTLINE_CONTENT_WORDS
from models.presentation_outline_model import PresentationOutlineModel, SlideOutlineModel
from utils.outline_limits import count_outline_words
from utils.outline_utils import (
    _extract_outline_title,
    get_images_for_slides_from_outline,
    get_no_of_outlines_to_generate_for_n_slides,
    get_no_of_toc_required_for_n_outlines,
    get_presentation_outline_model_with_toc,
    get_presentation_title_from_presentation_outline,
    strip_generated_toc_outlines,
)


def test_get_presentation_title_handles_prefixed_page_heading():
    outline = PresentationOutlineModel(
        slides=[SlideOutlineModel(content="## Page 1: Growth/Plan\\Roadmap\nBody")]
    )

    title = get_presentation_title_from_presentation_outline(outline)
    assert title == "GrowthPlanRoadmap Body"


def test_get_presentation_title_for_empty_outline():
    outline = PresentationOutlineModel(slides=[])
    assert get_presentation_title_from_presentation_outline(outline) == "Untitled Presentation"


def test_slide_outline_content_is_trimmed_to_word_limit():
    assert MAX_OUTLINE_CONTENT_WORDS == 100
    content = " ".join(
        f"word{i}" for i in range(MAX_OUTLINE_CONTENT_WORDS + 3)
    )

    slide = SlideOutlineModel(content=content)

    assert count_outline_words(slide.content) == MAX_OUTLINE_CONTENT_WORDS
    assert f"word{MAX_OUTLINE_CONTENT_WORDS - 1}" in slide.content
    assert f"word{MAX_OUTLINE_CONTENT_WORDS}" not in slide.content


def test_presentation_outline_rejects_more_than_max_slides():
    with pytest.raises(ValidationError):
        PresentationOutlineModel(
            slides=[
                SlideOutlineModel(content=f"## Slide {index}")
                for index in range(MAX_NUMBER_OF_SLIDES + 1)
            ]
        )


@pytest.mark.parametrize(
    ("n_outlines", "title_slide", "target_total_slides", "expected"),
    [
        (0, True, None, 0),
        (1, True, None, 0),
        (2, True, None, 1),
        (12, True, None, 1),
        (12, False, None, 1),
        (8, True, 25, 1),
    ],
)
def test_calculate_no_of_toc_required_for_n_outlines(
    n_outlines: int,
    title_slide: bool,
    target_total_slides: int | None,
    expected: int,
):
    assert (
        get_no_of_toc_required_for_n_outlines(
            n_outlines=n_outlines,
            title_slide=title_slide,
            target_total_slides=target_total_slides,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("n_slides", "toc", "title_slide", "expected"),
    [
        (16, True, True, 15),
        (12, True, False, 11),
        (8, False, True, 8),
    ],
)
def test_outlines_to_generate_reserve_one_toc_slot(
    n_slides: int,
    toc: bool,
    title_slide: bool,
    expected: int,
):
    assert (
        get_no_of_outlines_to_generate_for_n_slides(
            n_slides=n_slides,
            toc=toc,
            title_slide=title_slide,
        )
        == expected
    )


def test_strip_generated_toc_outlines_removes_llm_toc_keeps_topic_slides():
    outline = PresentationOutlineModel(
        slides=[
            SlideOutlineModel(content="## Nội dung và Hình thức\nPresenter"),
            SlideOutlineModel(content="## Mục lục\n- Khái niệm nội dung"),
            SlideOutlineModel(content="## Table of Contents\n- Page number: 3, Title: Essence"),
            SlideOutlineModel(content="## Khái niệm Nội dung\nNội dung là phạm trù."),
        ]
    )

    stripped = strip_generated_toc_outlines(outline, title_slide=True)

    assert [slide.content.split("\n", 1)[0] for slide in stripped.slides] == [
        "## Nội dung và Hình thức",
        "## Khái niệm Nội dung",
    ]


def test_get_presentation_outline_model_with_toc_caps_at_one_slide():
    outline = PresentationOutlineModel(
        slides=[
            SlideOutlineModel(content="## Title slide"),
            *[SlideOutlineModel(content=f"## Topic {index}") for index in range(12)],
        ]
    )

    with_toc = get_presentation_outline_model_with_toc(
        outline=outline,
        n_toc_slides=3,
        title_slide=True,
    )

    toc_slides = [
        slide
        for slide in with_toc.slides
        if slide.content.startswith("## Table of Contents")
    ]
    assert len(toc_slides) == 1
    assert with_toc.slides[1].content.startswith("## Table of Contents")
    assert "Title: Topic 0" in with_toc.slides[1].content
    assert "Title: Topic 11" in with_toc.slides[1].content


def test_get_presentation_outline_model_with_toc_inserts_expected_slide_structure():
    outline = PresentationOutlineModel(
        slides=[
            SlideOutlineModel(content="## Title slide"),
            SlideOutlineModel(content="## Market Overview"),
            SlideOutlineModel(content="## Product Strategy"),
        ]
    )

    with_toc = get_presentation_outline_model_with_toc(
        outline=outline,
        n_toc_slides=1,
        title_slide=True,
    )

    assert len(with_toc.slides) == 4
    toc_content = with_toc.slides[1].content
    assert toc_content.startswith("## Table of Contents")
    assert "Page number: 3, Title: Market Overview" in toc_content
    assert "Page number: 4, Title: Product Strategy" in toc_content


def test_extract_outline_title_uses_heading_then_sentence_then_fallback():
    assert _extract_outline_title("## Heading title\nBody") == "Heading title"
    assert _extract_outline_title("First sentence. Second sentence.") == "First sentence."
    assert _extract_outline_title(" \nline fallback\n") == "line fallback"
    assert _extract_outline_title("") == "Slide"


def test_get_images_for_slides_from_outline_deduplicates_and_filters():
    slides = [
        SlideOutlineModel(
            content=(
                "Image https://cdn.example.com/a.png and duplicate "
                "https://cdn.example.com/a.png and invalid https://example.com/nope.txt"
            )
        ),
        SlideOutlineModel(content="No URL here"),
    ]

    extracted = get_images_for_slides_from_outline(slides)

    assert extracted[0] == ["https://cdn.example.com/a.png"]
    assert extracted[1] == []
