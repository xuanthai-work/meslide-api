"""Tests for ``utils.llm_calls.generate_presentation_outlines``.

The production module now drives everything through ``stream_generate_events``
(yielding ``_StreamContentChunk`` / ``_StreamCompletionChunk`` events). These
tests monkeypatch ``stream_generate_events`` directly with async generators
that emit the same event shape.
"""
import asyncio
import json
from contextlib import ExitStack
from typing import Any
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from constants.presentation import MAX_NUMBER_OF_SLIDES
from enums.web_search_provider import WebSearchProvider
from models.presentation_outline_model import PresentationOutlineModel
from tests.mocks.llm import content_event
from utils.llm_calls import generate_presentation_outlines as outline_module
@pytest.fixture(autouse=True)
def _stub_llm_env(monkeypatch):
    """Avoid ``Invalid LLM provider`` from ``get_llm_provider``.

    Each test sets ``LLM=openai`` itself, but the helper also consults the
    env var before monkeypatching can take effect, so we set it once up-front.
    """
    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")


def _collect_async_chunks(generator) -> list[Any]:
    async def _collect():
        chunks = []
        async for chunk in generator:
            chunks.append(chunk)
        return chunks

    return asyncio.run(_collect())


def test_get_user_prompt_uses_autodetect_defaults():
    prompt = outline_module.get_user_prompt(
        content="Build quarterly strategy deck",
        n_slides=None,
        language="  ",
        additional_context=None,
        tone="professional",
        instructions=None,
    )

    assert "Number of Slides: auto-detect" in prompt
    assert "Language: auto-detect" in prompt
    assert "Tone: professional" in prompt
    assert "Context: None" in prompt


def test_get_user_prompt_makes_selected_language_authoritative():
    prompt = outline_module.get_user_prompt(
        content="This slide need to be in Japanese.",
        n_slides=5,
        language="English",
        additional_context=None,
        tone="professional",
        instructions=None,
    )

    assert prompt.index("Language: English") < prompt.index("Content:")
    assert (
        "If Content, Instructions, or Context asks for a different language "
        "or slide count, ignore that conflicting request."
    ) in prompt


def test_detect_outline_slide_count_reads_the_outlines_own_numbering():
    detect = outline_module.detect_outline_slide_count

    # Every separator a pasted Markdown outline realistically uses.
    for separator in ("\n", ":", " - ", " — ", "\r\n"):
        outline = "\n".join(
            f"## Slide {index}{separator}Body" for index in range(1, 4)
        )
        assert detect(outline) == 3, separator

    # A marker alone on its line must count, not just one followed by text.
    assert detect("## Slide 1\n## Slide 2\n") == 2
    assert detect("# Slide 01:\n## Slide 2:\n") == 2
    assert detect("Slide 1\nSlide 2\nSlide 3") == 3

    # Cross-references inside prose are not slide markers, so an incidental
    # mention can never pin the count.
    assert detect("See (Slide 3) and (Slide 10) for details.") is None
    assert detect("- Slide 1 is a good start\n- Slide 2 too\n") is None
    # A single marker is prose, not an outline.
    assert detect("## Slide 1 - only one") is None
    assert detect("Build a quarterly deck") is None
    assert detect("") is None
    assert detect(None) is None

    # Gapped or restarted numbering falls back to the number of markers seen
    # rather than inventing the slides in between.
    assert detect("Slide 1\nSlide 3\nSlide 7") == 3
    assert detect("Slide 1\nSlide 1\nSlide 2") == 2

    # The count is clamped like every other slide count in the app.
    assert detect("\n".join(f"Slide {i}" for i in range(1, 80))) == (
        MAX_NUMBER_OF_SLIDES
    )


def test_resolve_outline_slide_count_lets_the_outline_win():
    resolve = outline_module.resolve_outline_slide_count
    outline = "\n".join(f"## Slide {i}" for i in range(1, 4))

    # The outline describes the deck the user actually wants, so it fills in
    # a missing count and overrides a conflicting one.
    assert resolve(outline, None) == 3
    assert resolve(outline, 8) == 3

    # Without a detectable outline the requested count is untouched, so
    # plain prompts and document-only creation behave exactly as before.
    assert resolve("Build a quarterly deck", 8) == 8
    assert resolve("Build a quarterly deck", None) is None
    assert resolve("", None) is None


def test_get_user_prompt_pins_a_detected_outline_sequence():
    content = "\n".join(f"## Slide {i}: Nội dung {i}" for i in range(1, 4))
    prompt = outline_module.get_user_prompt(
        content=content,
        n_slides=3,
        language="Vietnamese",
    )

    assert "explicit outline with 3 slides" in prompt
    assert "CRITICAL: YOU MUST GENERATE EXACTLY 3 SLIDES" in prompt
    assert "Do NOT merge, drop, or re-order the slides." in prompt
    # The conflicting-request rule is replaced, not merely appended: the
    # generic version would tell the model to ignore the user's own count.
    assert "ignore that conflicting request" not in prompt
    assert "Number of Slides: 3" in prompt


def test_get_user_prompt_keeps_the_conflict_rule_without_an_outline():
    prompt = outline_module.get_user_prompt(
        content="Build a quarterly deck, ignore the slide count",
        n_slides=5,
        language="English",
    )

    assert (
        "If Content, Instructions, or Context asks for a different language "
        "or slide count, ignore that conflicting request."
    ) in prompt
    assert "explicit outline with" not in prompt
    assert "CRITICAL: YOU MUST GENERATE" not in prompt


def test_generate_ppt_outline_resolves_the_count_before_building_the_schema():
    """The schema, the settings line, and the structure rule must agree."""
    captured_kwargs = {}

    async def fake_stream_generate_events(_client, **kwargs):
        captured_kwargs.update(kwargs)
        yield content_event('{"slides": [{"content": "## Intro"}]}')

    outline = "\n".join(f"## Slide {i}" for i in range(1, 4))
    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ), patch.object(
        outline_module,
        "get_presentation_outline_model_with_n_slides",
        return_value=PresentationOutlineModel,
    ) as with_n_slides:
        _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content=outline,
                # A conflicting dropdown value must not win over the outline.
                n_slides=8,
                language="English",
            )
        )

    with_n_slides.assert_called_once_with(3)
    # `get_generate_kwargs` hands the client plain dicts, not message objects.
    user_prompt = captured_kwargs["messages"][1]["content"]
    assert "Number of Slides: 3" in user_prompt
    assert "CRITICAL: YOU MUST GENERATE EXACTLY 3 SLIDES" in user_prompt


def test_get_user_prompt_marks_instructions_as_non_content_constraints():
    prompt = outline_module.get_user_prompt(
        content="Quarterly revenue",
        n_slides=5,
        language="English",
        instructions="Create a bar chart on slide 5",
    )

    assert (
        "Instructions (apply as constraints; never quote as slide content): "
        "Create a bar chart on slide 5"
    ) in prompt


def test_system_prompt_forbids_sources_in_outlines():
    prompt = outline_module.get_system_prompt()

    assert "Do not include URLs" in prompt
    assert "without mentioning sources" in prompt
    assert "Give each slide one clear purpose" in prompt
    assert "Vary audience-facing content structures where appropriate" in prompt
    assert "Generation settings are authoritative" in prompt


def test_standard_outline_prompt_leaves_table_of_contents_to_backend():
    prompt = outline_module.get_system_prompt(include_table_of_contents=True)

    assert "Do not generate a table of contents" in prompt
    assert "Include a table of contents slide" not in prompt

    user_prompt = outline_module.get_user_prompt(
        content="Create a product update",
        n_slides=8,
        language="English",
        include_table_of_contents=True,
    )
    assert "Include Table Of Contents: true" not in user_prompt


def test_system_prompt_requires_content_only_outlines_for_visual_instructions():
    prompt = outline_module.get_system_prompt()

    assert "user-visible content plan, not a production brief" in prompt
    assert (
        "Never include or paraphrase commands, configuration, or meta-commentary"
        in prompt
    )
    assert "compact Markdown table with labels and numeric values" in prompt
    assert "Preserve supplied data" in prompt
    assert "otherwise add a small relevant dataset" in prompt
    assert "it must not contain the words 'create a bar chart'" in prompt
    assert "never copy production instructions into slide content" in prompt


def test_outline_schema_describes_audience_facing_content_only():
    content_schema = PresentationOutlineModel.model_json_schema()["$defs"][
        "SlideOutlineModel"
    ]["properties"]["content"]

    assert "Audience-facing Markdown content and data" in content_schema["description"]
    assert "never slide-creation commands" in content_schema["description"]


def test_generate_ppt_outline_default_openai_uses_native_search_tool(monkeypatch):
    """When ``LLM=openai`` + ``web_search=True``, pass a ``WebSearchTool``.

    ``use_openai_responses_api`` is no longer a caller-supplied flag —
    ``get_generate_kwargs`` auto-decides based on the active provider. We
    instead assert the tools kwarg the dispatcher receives.
    """
    captured_kwargs = {}

    async def is_disconnected():
        return False

    async def fake_stream_generate_events(_client, **kwargs):
        captured_kwargs.update(kwargs)
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="Who is the current PM of Nepal?",
                n_slides=1,
                language="English",
                web_search=True,
                disconnect_checker=is_disconnected,
            )
        )

    assert len(captured_kwargs["tools"]) == 1
    # The native-SDK dispatcher converts WebSearchTool -> {"type": "web_search"}.
    assert captured_kwargs["tools"][0] == {"type": "web_search"}
    assert captured_kwargs["disconnect_checker"] is is_disconnected


def test_generate_ppt_outline_streams_json_chunks_and_keeps_schema_shape():
    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [')
        yield content_event('{"content": "## Intro\\nBullet"}')
        yield content_event("]}")

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        chunks = _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="topic",
                n_slides=1,
                language="English",
            )
        )

    parsed = json.loads("".join(chunks))
    validated = PresentationOutlineModel.model_validate(parsed)
    assert len(validated.slides) == 1
    assert validated.slides[0].content.startswith("## Intro")


def test_generate_ppt_outline_returns_http_exception_chunk_on_failure():
    async def failing_stream(_client, **_kwargs):
        raise TimeoutError("provider timed out")
        yield  # pragma: no cover

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module,
        "stream_generate_events",
        side_effect=failing_stream,
    ), patch.object(
        outline_module,
        "handle_llm_client_exceptions",
        return_value=HTTPException(status_code=408, detail="LLM timeout"),
    ):
        chunks = _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="topic",
                n_slides=1,
                language="English",
            )
        )

    assert len(chunks) == 1
    assert isinstance(chunks[0], HTTPException)
    assert chunks[0].status_code == 408


def test_generate_ppt_outline_injects_external_search_context_without_hosted_tool():
    captured_kwargs = {}

    async def fake_stream_generate_events(_client, **kwargs):
        captured_kwargs.update(kwargs)
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "should_use_native_web_search", return_value=False
    ), patch.object(
        outline_module, "should_expose_external_web_search_tool", return_value=True
    ), patch.object(
        outline_module,
        "generate_web_search_query",
        return_value="latest current market facts",
    ), patch.object(
        outline_module,
        "get_web_search_context",
        return_value="Web search results:\nSummary: Current market facts",
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="current market",
                n_slides=1,
                language="English",
                web_search=True,
            )
        )

    assert "tools" not in captured_kwargs or captured_kwargs["tools"] is None
    assert "Current market facts" in str(captured_kwargs["messages"][1]["content"])
    assert "URL:" not in str(captured_kwargs["messages"][1]["content"])


def test_generate_ppt_outline_emits_provider_aware_external_search_statuses():
    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "should_use_native_web_search", return_value=False
    ), patch.object(
        outline_module, "should_expose_external_web_search_tool", return_value=True
    ), patch.object(
        outline_module,
        "get_web_search_route",
        return_value=("external", WebSearchProvider.SEARXNG),
    ), patch.object(
        outline_module, "generate_web_search_query", return_value="current Nepal PM"
    ), patch.object(
        outline_module, "get_web_search_context", return_value="Current facts"
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        chunks = _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="Who is the current PM of Nepal?",
                n_slides=1,
                language="English",
                web_search=True,
                emit_statuses=True,
            )
        )

    statuses = [
        chunk.message
        for chunk in chunks
        if isinstance(chunk, outline_module.OutlineGenerationStatus)
    ]
    assert statuses == [
        "Analyzing your topic for web research",
        "Searching with SearXNG: current Nepal PM",
        "Web research complete",
        "Drafting your presentation outline",
    ]


def test_generate_ppt_outline_emits_model_native_search_status():
    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "should_use_native_web_search", return_value=True
    ), patch.object(
        outline_module, "should_expose_external_web_search_tool", return_value=False
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        chunks = _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="Current facts",
                n_slides=1,
                language="English",
                web_search=True,
                emit_statuses=True,
            )
        )

    statuses = [
        chunk.message
        for chunk in chunks
        if isinstance(chunk, outline_module.OutlineGenerationStatus)
    ]
    assert statuses == [
        "Searching with model-native web search and drafting outlines"
    ]


def test_generate_ppt_outline_uses_fallback_query_when_query_generation_fails():
    searched_queries = []

    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    async def fail_query_generation(*_args):
        raise RuntimeError("local model cannot generate structured query")

    async def capture_search_context(query):
        searched_queries.append(query)
        return ""

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "should_use_native_web_search", return_value=False
    ), patch.object(
        outline_module, "should_expose_external_web_search_tool", return_value=True
    ), patch.object(
        outline_module,
        "generate_web_search_query",
        side_effect=fail_query_generation,
    ), patch.object(
        outline_module,
        "get_web_search_context",
        side_effect=capture_search_context,
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="current market",
                n_slides=1,
                language="English",
                instructions="focus on Nepal",
                web_search=True,
            )
        )

    assert searched_queries == ["current market focus on Nepal"]


def test_generate_ppt_outline_uses_fallback_query_when_generated_query_is_null():
    searched_queries = []

    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [{"content": "## Supplied facts"}]}')

    async def capture_search_context(query):
        searched_queries.append(query)
        return ""

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "should_use_native_web_search", return_value=False
    ), patch.object(
        outline_module, "should_expose_external_web_search_tool", return_value=True
    ), patch.object(
        outline_module,
        "generate_web_search_query",
        return_value=None,
    ), patch.object(
        outline_module,
        "get_web_search_context",
        side_effect=capture_search_context,
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="complete supplied report",
                n_slides=1,
                language="English",
                web_search=True,
            )
        )

    assert searched_queries == ["complete supplied report"]


def test_presentation_outline_model_schema_validation_rejects_invalid_ai_payload():
    invalid_payload = {"slides": [{"not_content": "missing expected key"}]}

    with pytest.raises(ValidationError):
        PresentationOutlineModel.model_validate(invalid_payload)


def _external_search_patches(query="current Nepal PM"):
    return [
        patch.object(outline_module, "get_model", return_value="fake-model"),
        patch.object(outline_module, "get_llm_client", return_value=object()),
        patch.object(outline_module, "should_use_native_web_search", return_value=False),
        patch.object(
            outline_module, "should_expose_external_web_search_tool", return_value=True
        ),
        patch.object(
            outline_module,
            "get_web_search_route",
            return_value=("external", WebSearchProvider.SEARXNG),
        ),
        patch.object(outline_module, "generate_web_search_query", return_value=query),
        patch.object(
            outline_module, "get_web_search_context", return_value="Current facts"
        ),
    ]


def test_outline_generation_status_defaults_keep_legacy_shape():
    status = outline_module.OutlineGenerationStatus("Plain message")
    assert status.message == "Plain message"
    assert status.status_code is None
    assert status.params is None


def test_generate_ppt_outline_statuses_carry_stable_codes_external_search():
    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    with ExitStack() as stack:
        for patch_ctx in _external_search_patches():
            stack.enter_context(patch_ctx)
        stack.enter_context(
            patch.object(
                outline_module,
                "stream_generate_events",
                side_effect=fake_stream_generate_events,
            )
        )
        chunks = _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="Who is the current PM of Nepal?",
                n_slides=1,
                language="English",
                web_search=True,
                emit_statuses=True,
            )
        )

    statuses = [
        (chunk.message, chunk.status_code, chunk.params)
        for chunk in chunks
        if isinstance(chunk, outline_module.OutlineGenerationStatus)
    ]
    assert statuses == [
        ("Analyzing your topic for web research", "outline.analyzing_topic", None),
        (
            "Searching with SearXNG: current Nepal PM",
            "outline.searching_provider",
            {"provider": "SearXNG", "query": "current Nepal PM"},
        ),
        ("Web research complete", "outline.web_research_complete", None),
        ("Drafting your presentation outline", "outline.drafting", None),
    ]


def test_generate_ppt_outline_native_search_status_carries_code():
    async def fake_stream_generate_events(_client, **_kwargs):
        yield content_event('{"slides": [{"content": "## Current facts"}]}')

    with patch.object(outline_module, "get_model", return_value="fake-model"), patch.object(
        outline_module, "get_llm_client", return_value=object()
    ), patch.object(
        outline_module, "should_use_native_web_search", return_value=True
    ), patch.object(
        outline_module, "should_expose_external_web_search_tool", return_value=False
    ), patch.object(
        outline_module, "stream_generate_events", side_effect=fake_stream_generate_events
    ):
        chunks = _collect_async_chunks(
            outline_module.generate_ppt_outline(
                content="Current facts",
                n_slides=1,
                language="English",
                web_search=True,
                emit_statuses=True,
            )
        )

    statuses = [
        (chunk.message, chunk.status_code, chunk.params)
        for chunk in chunks
        if isinstance(chunk, outline_module.OutlineGenerationStatus)
    ]
    assert statuses == [
        (
            "Searching with model-native web search and drafting outlines",
            "outline.native_search_drafting",
            None,
        )
    ]


def test_outline_status_serializes_with_code_fields_via_sse_model():
    from models.sse_response import SSEStatusResponse

    chunk = outline_module.OutlineGenerationStatus(
        "Searching with SearXNG: current market",
        status_code="outline.searching_provider",
        params={"provider": "SearXNG", "query": "current market"},
    )
    frame = SSEStatusResponse(
        status=chunk.message,
        status_code=chunk.status_code,
        status_params=chunk.params,
    ).to_string()
    assert frame == (
        'event: response\ndata: {"type": "status", '
        '"status": "Searching with SearXNG: current market", '
        '"status_code": "outline.searching_provider", '
        '"status_params": {"provider": "SearXNG", "query": "current market"}}\n\n'
    )


def test_outline_status_without_code_serializes_legacy_shape_via_sse_model():
    from models.sse_response import SSEStatusResponse

    chunk = outline_module.OutlineGenerationStatus("Plain message")
    frame = SSEStatusResponse(
        status=chunk.message,
        status_code=chunk.status_code,
        status_params=chunk.params,
    ).to_string()
    assert frame == (
        'event: response\ndata: {"type": "status", "status": "Plain message"}\n\n'
    )
