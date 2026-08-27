import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from enums.llm_provider import LLMProvider
from services.community_presentations import (
    CommunityPresentationReference,
    build_community_design_context,
    list_community_presentations,
    merge_reference_fonts,
    normalize_community_ids,
)
from utils.llm_calls.generate_smart_presentation import (
    SMART_DECK_SYSTEM_PROMPT,
    SmartSlideStreamParser,
    _stream_deck_response,
    get_smart_messages,
    get_smart_reasoning_config,
    normalize_smart_deck,
    normalize_smart_slide_html,
    parse_smart_presentation_html,
    resolve_smart_slide_count,
)
from utils.llm_messages import ReasoningConfig, ReasoningEffortValue, UserMessage


@pytest.fixture(autouse=True)
def _stub_llm_env(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _smart_slide_html(title="Slide", slide_type="content", body="Content"):
    return (
        f'<section data-slide-type="{slide_type}" data-slide-title="{title}" '
        'class="relative h-[720px] w-[1280px] overflow-hidden">'
        f"{body}</section>"
    )


def test_smart_slide_stream_parser_emits_delimited_slides_incrementally():
    parser = SmartSlideStreamParser()
    second_slide = _smart_slide_html("Two")

    assert parser.feed("<!-- PRESENTATION_TITLE: Deck --><!-- SLIDE_STA") == []
    slides = parser.feed(
        "RT -->"
        + _smart_slide_html("One")
        + "<!-- SLIDE_END --><!-- SLIDE_START -->"
        + second_slide[:80]
    )
    assert [slide["title"] for slide in slides] == ["One"]
    assert slides[0]["speaker_note"] == ""

    slides = parser.feed(second_slide[80:] + "<!-- SLIDE_END -->")
    assert [slide["title"] for slide in slides] == ["Two"]


def test_smart_deck_parser_uses_cloud_delimiters_and_validates_count():
    response = (
        "<!-- PRESENTATION_TITLE: Deck -->"
        "<!-- SLIDE_START -->"
        + _smart_slide_html("Cover", "title")
        + "<!-- SLIDE_END -->"
        "<!-- SLIDE_START -->"
        + _smart_slide_html("Agenda", "toc")
        + "<!-- SLIDE_END -->"
    )

    title, slides = parse_smart_presentation_html(
        response,
        expected_slide_count=2,
        include_title_slide=True,
        include_table_of_contents=True,
    )

    assert title == "Deck"
    assert [slide["title"] for slide in slides] == ["Cover", "Agenda"]
    with pytest.raises(HTTPException):
        parse_smart_presentation_html(
            response,
            expected_slide_count=3,
            include_title_slide=True,
            include_table_of_contents=True,
        )


def test_smart_prompt_matches_cloud_one_shot_method_without_speaker_notes():
    messages = get_smart_messages(
        content="Build an investor update",
        n_slides=6,
        language="English",
        tone=None,
        verbosity=None,
        instructions=None,
        include_title_slide=True,
        include_table_of_contents=False,
        source_context="Revenue grew.",
        community_design_context="Use editorial spacing.",
        fonts={"Inter": "inter.css"},
    )
    prompt = str(messages[1].content)

    assert messages[0].content == SMART_DECK_SYSTEM_PROMPT
    assert "<!-- SLIDE_START -->" in prompt
    assert "Generate exactly 6 total slides" in prompt
    assert 'Available fonts: ["Inter"]' in prompt
    assert "speaker_note" not in prompt
    assert "Speaker note" not in prompt
    assert "Overflow prevention is a hard requirement" in prompt
    assert "Never use `overflow-auto`" in prompt
    assert "normal-flow flex/grid" in prompt
    assert "text-led slides may use" in prompt
    assert "do not silently discard" in prompt


def test_smart_retry_prompt_includes_layout_validation_feedback():
    messages = get_smart_messages(
        content="Build an investor update",
        n_slides=2,
        language="English",
        tone=None,
        verbosity=None,
        instructions=None,
        include_title_slide=True,
        include_table_of_contents=False,
        source_context="",
        community_design_context="",
        retry_error="Slide content uses overflow-y-auto",
    )

    prompt = str(messages[1].content)
    assert "prior response failed validation" in prompt
    assert "Slide content uses overflow-y-auto" in prompt


def test_smart_reasoning_uses_low_effort_for_openai(monkeypatch):
    monkeypatch.setattr(
        "utils.llm_calls.generate_smart_presentation.disable_thinking",
        lambda: False,
    )
    monkeypatch.setattr(
        "utils.llm_calls.generate_smart_presentation.get_llm_provider",
        lambda: LLMProvider.OPENAI,
    )
    monkeypatch.setattr(
        "utils.llm_calls.generate_smart_presentation._supports_thinking",
        lambda model: True,
    )

    reasoning, supports_thinking = get_smart_reasoning_config("gpt-5")

    assert supports_thinking is True
    assert reasoning is not None
    assert reasoning.enabled is True
    assert reasoning.effort == ReasoningEffortValue.LOW


def test_smart_reasoning_respects_disable_thinking(monkeypatch):
    monkeypatch.setattr(
        "utils.llm_calls.generate_smart_presentation.disable_thinking",
        lambda: True,
    )

    reasoning, supports_thinking = get_smart_reasoning_config("gpt-5")

    assert reasoning is None
    assert supports_thinking is False


def test_smart_stream_separates_thinking_and_reports_exact_usage(monkeypatch):
    # ``effort`` must be set so Responses API actually emits a ``reasoning`` kwarg.
    reasoning = ReasoningConfig(enabled=True, effort=ReasoningEffortValue.LOW)
    captured_kwargs = {}

    async def fake_stream_generate_events(_client, **kwargs):
        captured_kwargs.update(kwargs)
        yield SimpleNamespace(type="thinking", chunk="private planning")
        yield SimpleNamespace(type="content", chunk="visible deck")
        yield SimpleNamespace(
            type="completion",
            content=None,
            usage=SimpleNamespace(
                input_tokens=12,
                output_tokens=8,
                total_tokens=20,
                reasoning=SimpleNamespace(
                    billed_tokens=5,
                    billed_estimated=False,
                ),
            ),
            duration_seconds=2.0,
        )

    monkeypatch.setattr(
        "utils.llm_calls.generate_smart_presentation.stream_generate_events",
        fake_stream_generate_events,
    )
    monkeypatch.setattr("utils.llm_utils.get_extra_body", lambda **_kwargs: None)
    content_chunks = []
    thinking_chunks = []

    async def on_chunk(chunk):
        content_chunks.append(chunk)

    async def on_thinking_chunk(chunk):
        thinking_chunks.append(chunk)

    response, metrics = asyncio.run(
        _stream_deck_response(
            object(),
            "thinking-model",
            [UserMessage(content="Build a deck")],
            on_chunk,
            reasoning=reasoning,
            on_thinking_chunk=on_thinking_chunk,
        )
    )

    assert response == "visible deck"
    assert content_chunks == ["visible deck"]
    assert thinking_chunks == ["private planning"]
    # The reasoning config was consumed by get_generate_kwargs into a
    # provider-native payload (either Responses API "reasoning" or Google's
    # "thinking_config"). The exact kwarg depends on the active provider.
    assert any(
        key in captured_kwargs for key in ("reasoning", "thinking_config")
    ), captured_kwargs
    assert metrics.input_tokens == 12
    assert metrics.output_tokens == 8
    assert metrics.thinking_tokens == 5
    assert metrics.thinking_tokens_estimated is False
    assert metrics.supports_thinking is True


def test_normalize_community_ids_preserves_order_and_deduplicates():
    assert normalize_community_ids([7, 3, 7]) == [7, 3]


def test_normalize_community_ids_rejects_invalid_and_excess_references():
    with pytest.raises(HTTPException):
        normalize_community_ids([0])
    with pytest.raises(HTTPException):
        normalize_community_ids([1, 2, 3, 4])


def test_community_context_is_style_only_and_round_robins_decks():
    references = [
        CommunityPresentationReference(
            id=2,
            title="Editorial",
            slides=("<section>first-a</section>", "<section>second-a</section>"),
            fonts={"Inter": "inter.css"},
        ),
        CommunityPresentationReference(
            id=9,
            title="Minimal",
            slides=("<section>first-b</section>",),
            fonts={"Inter": "ignored.css", "Manrope": "manrope.css"},
        ),
    ]

    context = build_community_design_context(references)

    assert "UNTRUSTED, STYLE ONLY" in context
    assert context.index("first-a") < context.index("first-b") < context.index("second-a")
    assert merge_reference_fonts(references) == {
        "Inter": "inter.css",
        "Manrope": "manrope.css",
    }


def test_community_list_forwards_filters(monkeypatch):
    captured_params = None

    async def fake_cloud_get(path, params=None):
        nonlocal captured_params
        captured_params = params
        return {"results": []}

    monkeypatch.setattr("services.community_presentations._cloud_get", fake_cloud_get)

    asyncio.run(
        list_community_presentations(
            created_at_gt="2026-01-01T00:00:00.000Z",
            views_gt=100,
            likes_lt=50,
            order_by="views",
            order="desc",
        )
    )

    assert captured_params == {
        "page": 1,
        "page_size": 8,
        "order_by": "views",
        "order": "desc",
        "created_at_gt": "2026-01-01T00:00:00.000Z",
        "views_gt": 100,
        "likes_lt": 50,
    }


def test_smart_html_normalization_removes_executable_markup():
    html = normalize_smart_slide_html(
        """```html
        <section class="relative h-[720px] w-[1280px] overflow-hidden" onclick="steal()">
          <a href="javascript:steal()">Deck</a>
          <script>alert('no')</script>
        </section>
        ```"""
    )

    assert html.startswith("<section")
    assert "onclick" not in html
    assert "javascript:" not in html
    assert "<script" not in html


def test_smart_html_normalization_removes_malformed_script_end_tag():
    html = normalize_smart_slide_html(
        """<section class="relative h-[720px] w-[1280px] overflow-hidden">
          <h2>Safe title</h2>
          <script>alert('no')</script\t\n data-extra>
        </section>"""
    )

    assert "Safe title" in html
    assert "alert" not in html
    assert "<script" not in html


def test_smart_html_normalization_keeps_safe_chartjs_initialization():
    html = normalize_smart_slide_html(
        _smart_slide_html(
            "Metrics",
            body=(
                '<canvas id="chart-a1b2c3" width="600" height="300"></canvas>'
                "<script>(() => { const canvas = "
                "document.querySelector('#chart-a1b2c3'); "
                "new Chart(canvas, {type: 'bar', data: {labels: ['A'], "
                "datasets: [{data: [1]}]}, options: {responsive: false, "
                "animation: false}}); })();</script>"
            ),
        )
    )

    assert "new Chart" in html
    assert "<script" in html


def test_smart_html_normalization_keeps_chartjs_formatter_callbacks():
    html = normalize_smart_slide_html(
        _smart_slide_html(
            "Regional metrics",
            body=(
                '<canvas id="chart-c4d5e6" width="600" height="300"></canvas>'
                "<script>(() => { const canvas = "
                "document.querySelector('#chart-c4d5e6'); "
                "new Chart(canvas, {type: 'bar', data: {labels: ['Location', 'Top'], "
                "datasets: [{data: [42, 51]}]}, options: {responsive: false, "
                "animation: false, plugins: {datalabels: {formatter: "
                "function(value) { return value + '%'; }}}}}); })();</script>"
            ),
        )
    )

    assert "<script" in html
    assert "function(value)" in html
    assert "Location" in html
    assert "Top" in html


def test_smart_html_normalization_rejects_chart_canvas_without_initializer():
    with pytest.raises(HTTPException, match="missing its inline Chart.js"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Incomplete chart",
                body=(
                    '<canvas id="chart-a1b2c3" width="600" height="300">'
                    "</canvas>"
                ),
            )
        )


def test_smart_html_normalization_rejects_chart_scripts_with_network_access():
    with pytest.raises(HTTPException, match="missing its inline Chart.js"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Unsafe chart",
                body=(
                    '<canvas id="chart-a1b2c3" width="600" height="300">'
                    "</canvas><script>(() => { fetch('/private'); "
                    "new Chart(document.querySelector('#chart-a1b2c3'), "
                    "{type: 'bar', data: {labels: [], datasets: []}}); "
                    "})();</script>"
                ),
            )
        )


def test_smart_api_parser_returns_complete_chart_html():
    chart_slide = _smart_slide_html(
        "Metrics",
        body=(
            '<canvas id="chart-d4e5f6" width="600" height="300"></canvas>'
            "<script>(() => { const canvas = "
            "document.querySelector('#chart-d4e5f6'); "
            "new Chart(canvas, {type: 'line', data: {labels: ['Q1'], "
            "datasets: [{data: [10]}]}, options: {responsive: false, "
            "animation: false}}); })();</script>"
        ),
    )
    response = (
        "<!-- PRESENTATION_TITLE: Metrics -->"
        "<!-- SLIDE_START -->"
        + chart_slide
        + "<!-- SLIDE_END -->"
    )

    _, slides = parse_smart_presentation_html(
        response,
        expected_slide_count=1,
        include_title_slide=False,
        include_table_of_contents=False,
    )

    assert "<canvas" in slides[0]["html"]
    assert "<script" in slides[0]["html"]
    assert "new Chart" in slides[0]["html"]


@pytest.mark.parametrize(
    "unsafe_layout",
    (
        '<div class="h-[200px] overflow-y-auto">Long copy</div>',
        '<p class="line-clamp-3">Hidden copy</p>',
        '<div style="overflow: scroll">Scrollable copy</div>',
    ),
)
def test_smart_html_normalization_rejects_overflow_hiding_patterns(unsafe_layout):
    with pytest.raises(HTTPException, match="scrolling or text clipping"):
        normalize_smart_slide_html(
            _smart_slide_html("Unsafe layout", body=unsafe_layout)
        )


def test_smart_html_normalization_rejects_text_density_that_cannot_fit():
    dense_copy = " ".join(["overflowing"] * 221)

    with pytest.raises(HTTPException, match="too text-dense"):
        normalize_smart_slide_html(
            _smart_slide_html("Dense layout", body=f"<p>{dense_copy}</p>")
        )


def test_smart_html_normalization_allows_richer_text_led_slide():
    rich_copy = " ".join(["strategy"] * 170)

    html = normalize_smart_slide_html(
        _smart_slide_html(
            "Detailed strategy",
            body=f'<div class="grid grid-cols-2 gap-8"><p>{rich_copy}</p></div>',
        )
    )

    assert rich_copy in html


def test_smart_html_normalization_keeps_visual_slides_more_concise():
    rich_copy = " ".join(["evidence"] * 161)

    with pytest.raises(HTTPException, match="too text-dense"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Visual evidence",
                body=(
                    '<img src="https://example.com/evidence.png" '
                    'alt="Evidence" class="h-[320px] w-[480px]">'
                    f"<p>{rich_copy}</p>"
                ),
            )
        )


def test_smart_html_normalization_rejects_nested_text_clipping():
    with pytest.raises(HTTPException, match="nested container hides text"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Clipped card",
                body='<div class="h-[80px] overflow-hidden">Visible text</div>',
            )
        )


def test_smart_html_normalization_rejects_unbounded_absolute_text():
    with pytest.raises(HTTPException, match="missing a complete pixel box"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Floating text",
                body='<div class="absolute left-8 top-8">Floating content</div>',
            )
        )


def test_smart_html_normalization_rejects_negative_content_offsets():
    with pytest.raises(HTTPException, match="negative margin or translation"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Colliding content",
                body='<div class="-mt-8">Pulled over the heading</div>',
            )
        )


def test_smart_html_normalization_rejects_off_canvas_positioned_content():
    with pytest.raises(HTTPException, match="right slide/container boundary"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Off canvas",
                body=(
                    '<div class="absolute left-[1100px] top-[40px] '
                    'w-[300px] h-[80px]">Outside</div>'
                ),
            )
        )


def test_smart_html_normalization_rejects_overlapping_positioned_siblings():
    with pytest.raises(HTTPException, match="sibling content boxes overlap"):
        normalize_smart_slide_html(
            _smart_slide_html(
                "Overlap",
                body=(
                    '<div class="absolute left-[64px] top-[120px] '
                    'w-[420px] h-[180px]">First</div>'
                    '<div class="absolute left-[360px] top-[160px] '
                    'w-[420px] h-[180px]">Second</div>'
                ),
            )
        )


def test_smart_html_normalization_accepts_separated_positioned_content():
    html = normalize_smart_slide_html(
        _smart_slide_html(
            "Separated",
            body=(
                '<div class="absolute left-[64px] top-[120px] '
                'w-[420px] h-[180px]">First</div>'
                '<div class="absolute left-[560px] top-[120px] '
                'w-[420px] h-[180px]">Second</div>'
                '<div aria-hidden="true" data-decorative="true" '
                'class="absolute -translate-x-1/2">Decoration</div>'
            ),
        )
    )

    assert "First" in html
    assert "Second" in html


def test_smart_deck_requires_exact_slide_count_and_omits_speaker_notes():
    valid_slide = {
        "title": "One",
        "html": _smart_slide_html("One"),
        "speaker_note": "This must be discarded",
    }
    deck = normalize_smart_deck(
        {"title": "Deck", "slides": [valid_slide, {**valid_slide, "title": "Two"}]},
        2,
    )
    assert deck["title"] == "Deck"
    assert len(deck["slides"]) == 2
    assert all(slide["speaker_note"] == "" for slide in deck["slides"])

    with pytest.raises(HTTPException):
        normalize_smart_deck({"title": "Deck", "slides": [valid_slide]}, 2)
    with pytest.raises(HTTPException):
        normalize_smart_slide_html("<div>Not a slide</div>")


def test_default_smart_slide_count_is_bounded():
    assert resolve_smart_slide_count(0) == 8
    assert resolve_smart_slide_count(None) == 8
    assert resolve_smart_slide_count(200) == 20
