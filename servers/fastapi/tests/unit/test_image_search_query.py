"""Query normalization tests (FR-4): deterministic, Vietnamese + English."""

from services.image_search.query import (
    image_prompt_to_image_query,
    normalize_image_query,
)


def test_english_generation_prompt_becomes_searchable():
    assert (
        normalize_image_query(
            "Cinematic conceptual illustration of clean energy"
        )
        == "clean energy"
    )


def test_english_genre_openers_removed():
    assert normalize_image_query("A photo of Ha Noi skyline") == "Ha Noi skyline"
    assert (
        normalize_image_query("an image of semiconductor factory interior")
        == "semiconductor factory interior"
    )


def test_english_quality_modifiers_removed():
    assert (
        normalize_image_query(
            "Vietnam solar farm aerial photograph, 8k, highly detailed, "
            "dramatic lighting, vibrant colors"
        )
        == "Vietnam solar farm aerial photograph"
    )


def test_vietnamese_command_and_genre_removed():
    assert (
        normalize_image_query("Tạo hình ảnh nông trại điện mặt trời ở Ninh Thuận")
        == "nông trại điện mặt trời ở Ninh Thuận"
    )


def test_vietnamese_quality_modifiers_removed():
    assert (
        normalize_image_query(
            "Nông trại điện gió Bình Thuận, chất lượng cao, sắc nét, đẹp"
        )
        == "Nông trại điện gió Bình Thuận"
    )


def test_named_entities_places_products_dates_survive():
    for prompt, expected_fragment in (
        ("iPhone 15 Pro on a wooden desk", "iPhone 15 Pro on a wooden desk"),
        ("Gapo office building in 2024", "Gapo office building in 2024"),
    ):
        result = normalize_image_query(prompt)
        assert expected_fragment in result


def test_prompt_to_query_fallback_is_deterministic_and_llm_free():
    # The compatibility fallback must equal normalization — no LLM call,
    # same output every time.
    prompt = "Stunning beautiful 4k render of Da Nang bridge at night"
    first = image_prompt_to_image_query(prompt)
    second = image_prompt_to_image_query(prompt)
    assert first == second
    assert first == "Da Nang bridge at night"


def test_empty_and_whitespace_queries_return_empty():
    assert normalize_image_query("") == ""
    assert normalize_image_query("   \n\t ") == ""


def test_query_length_is_bounded():
    result = normalize_image_query("solar farm " + "detail " * 200)
    assert len(result) <= 200