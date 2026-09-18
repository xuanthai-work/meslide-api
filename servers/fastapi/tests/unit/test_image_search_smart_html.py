"""Smart-mode image-search tests (Phase 6).

Covers the ``<img data-image-query>`` slot contract end to end:

- slot extraction and offset-exact rewriting (``services.image_search.smart_html``)
- query instructions appearing in the Smart prompt only when web search is on
- the ``_stream_smart_presentation`` resolution flow: placeholder streams
  first, ``slide_assets`` events replace only the intended ``img[src]``,
  invented remote URLs are neutralized, failures keep valid placeholder
  HTML with coded warnings, and no ``data-image-query`` marker is ever
  stored or streamed after resolution.
"""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from api.v1.ppt.endpoints import presentation_smart_stream
from models.sql.image_asset import ImageAsset
from models.sql.presentation import PresentationModel
from services.image_search.constants import limits
from services.image_search.models import ImageSearchOutcome
from services.image_search.smart_html import (
    PLACEHOLDER_SRC,
    SmartHtmlRewriteError,
    SmartImageSlot,
    apply_smart_image_resolutions,
    extract_image_query_slots,
    neutralize_invented_remote_images,
    strip_image_query_markers,
)
from services.image_search.warnings import IMAGE_SEARCH_NOT_CONFIGURED_CODE
from models.sql.slide import SlideModel
from tests.conftest import FakeAsyncSession
from utils.llm_calls.generate_smart_presentation import get_smart_messages

# ---------------------------------------------------------------------------
# Shared HTML fixtures
# ---------------------------------------------------------------------------

SECTION_OPEN = (
    '<section data-slide-type="content" data-slide-title="Slide" '
    'class="relative h-[720px] w-[1280px] overflow-hidden">'
)


def _marked_img(query="Hanoi skyline at sunset", alt="Hanoi skyline"):
    return (
        f'<img src="{PLACEHOLDER_SRC}" data-image-query="{query}" alt="{alt}" '
        'class="h-[320px] w-[480px]">'
    )


def _section(body: str) -> str:
    return f"{SECTION_OPEN}{body}</section>"


CHART_SCRIPT = (
    "<script>(() => { const canvas = document.querySelector('#chart-f81a12'); "
    "new Chart(canvas, {type: 'bar', data: {labels: ['A'], datasets: "
    "[{data: [1]}]}, options: {responsive: false, animation: false}}); })();</script>"
)


# ---------------------------------------------------------------------------
# Slot extraction
# ---------------------------------------------------------------------------


def test_extract_finds_only_marked_slots_and_captures_attributes():
    html = _section(
        _marked_img(query="wind turbine field", alt="Turbines")
        + '<img src="/static/images/placeholder.jpg" alt="No marker">'
    )

    slots = extract_image_query_slots(html)

    assert len(slots) == 1
    assert slots[0].query == "wind turbine field"
    assert slots[0].alt == "Turbines"
    assert slots[0].src == PLACEHOLDER_SRC
    # The offset points at the literal "<img" start tag.
    assert html[slots[0].offset : slots[0].offset + 4] == "<img"
    assert slots[0].tag_text.startswith("<img")


def test_extract_reports_absolute_offsets_across_lines():
    html = (
        "<div>\n"
        '  <p>caption</p>\n'
        f'  {_marked_img(query="coastal cliffs")}\n'
        "</div>"
    )

    slots = extract_image_query_slots(html)

    assert len(slots) == 1
    assert html[slots[0].offset : slots[0].offset + 4] == "<img"


def test_extract_ignores_empty_and_whitespace_only_queries():
    html = _section(
        '<img src="/a.jpg" data-image-query="" alt="empty">'
        '<img src="/b.jpg" data-image-query="   " alt="blank">'
    )

    assert extract_image_query_slots(html) == []


# ---------------------------------------------------------------------------
# Resolution rewriting
# ---------------------------------------------------------------------------


def test_apply_resolutions_replaces_only_the_intended_src():
    html = _section(
        _marked_img(query="first query", alt="First")
        + _marked_img(query="second query", alt="Second")
        + '<img src="/static/images/placeholder.jpg" alt="Untouched">'
        + CHART_SCRIPT
    )
    slots = extract_image_query_slots(html)

    updated = apply_smart_image_resolutions(
        html,
        [
            (slots[0], "/app_data/images/one.jpg"),
            (slots[1], "/app_data/images/two.jpg"),
        ],
    )

    assert 'src="/app_data/images/one.jpg"' in updated
    assert 'src="/app_data/images/two.jpg"' in updated
    assert updated.count('alt="Untouched"') == 1
    assert updated.count(PLACEHOLDER_SRC) == 1  # only the unmarked img
    assert "data-image-query" not in updated
    # Everything around the edited tags is byte-identical.
    assert CHART_SCRIPT in updated
    assert "first query" not in updated and "second query" not in updated


def test_apply_resolutions_keeps_placeholder_for_failed_slot():
    html = _section(_marked_img(query="no results query"))
    slots = extract_image_query_slots(html)

    updated = apply_smart_image_resolutions(html, [(slots[0], None)])

    assert f'src="{PLACEHOLDER_SRC}"' in updated
    assert "data-image-query" not in updated


def test_apply_resolutions_inserts_src_when_the_model_omitted_it():
    html = _section('<img data-image-query="solar panel roof" alt="Solar">')
    slots = extract_image_query_slots(html)

    updated = apply_smart_image_resolutions(html, [(slots[0], "/app_data/images/x.jpg")])

    assert updated.startswith(f"{SECTION_OPEN}<img src=\"/app_data/images/x.jpg\"")
    assert 'alt="Solar"' in updated
    assert "data-image-query" not in updated


def test_apply_resolutions_escapes_attribute_hostile_urls():
    html = _section(_marked_img(query="q"))
    slots = extract_image_query_slots(html)

    updated = apply_smart_image_resolutions(
        html, [(slots[0], '/app_data/images/we"ird&name.jpg')]
    )

    assert 'src="/app_data/images/we&quot;ird&amp;name.jpg"' in updated


def test_apply_resolutions_handles_single_quoted_and_unquoted_src():
    html = _section(
        "<img data-image-query='single query' src='/old.jpg' alt='single'>"
        + '<img data-image-query="unquoted query" src=old.jpg alt="unquoted">'
    )
    slots = extract_image_query_slots(html)

    updated = apply_smart_image_resolutions(
        html,
        [(slots[0], "/app_data/images/a.jpg"), (slots[1], "/app_data/images/b.jpg")],
    )

    assert 'src="/app_data/images/a.jpg"' in updated
    assert 'src="/app_data/images/b.jpg"' in updated
    assert "/old.jpg" not in updated
    assert "data-image-query" not in updated


def test_apply_resolutions_matches_case_insensitive_tags():
    html = _section('<IMG SRC="https://evil.example/x.jpg" DATA-IMAGE-QUERY="mixed case" ALT="Case">')

    slots = extract_image_query_slots(html)
    assert len(slots) == 1 and slots[0].query == "mixed case"

    updated = apply_smart_image_resolutions(html, [(slots[0], "/app_data/images/c.jpg")])
    assert 'src="/app_data/images/c.jpg"' in updated


def test_apply_resolutions_rejects_mismatched_offsets():
    html = _section(_marked_img())
    forged = SmartImageSlot(offset=0, tag_text="<img wrong>", query="q")

    with pytest.raises(SmartHtmlRewriteError):
        apply_smart_image_resolutions(html, [(forged, "/app_data/images/x.jpg")])


# ---------------------------------------------------------------------------
# Marker stripping and remote-URL neutralization
# ---------------------------------------------------------------------------


def test_strip_markers_removes_markers_and_keeps_src():
    html = _section(_marked_img() + '<img src="/plain.jpg" alt="plain">')

    stripped = strip_image_query_markers(html)

    assert "data-image-query" not in stripped
    assert f'src="{PLACEHOLDER_SRC}"' in stripped
    assert 'src="/plain.jpg"' in stripped
    assert 'alt="Hanoi skyline"' in stripped


def test_strip_markers_is_idempotent_on_clean_html():
    html = _section('<img src="/app_data/images/one.jpg" alt="resolved">')

    assert strip_image_query_markers(html) == html


def test_neutralize_replaces_invented_remote_urls_but_keeps_markers():
    html = _section(
        '<img src="https://evil.example/a.jpg" data-image-query="real query" alt="a">'
        + '<img src="http://evil.example/b.jpg" alt="b">'
        + '<img src="//evil.example/c.jpg" alt="c">'
    )

    updated = neutralize_invented_remote_images(html)

    assert updated.count(f'src="{PLACEHOLDER_SRC}"') == 3
    assert 'data-image-query="real query"' in updated  # markers preserved
    assert "evil.example" not in updated


def test_neutralize_leaves_local_relative_and_data_srcs_untouched():
    html = _section(
        f'{_marked_img(query="local marked")}'
        + '<img src="/static/images/other.jpg" alt="relative">'
        + '<img src="data:image/png;base64,AAAA" alt="inline">'
    )

    assert neutralize_invented_remote_images(html) == html


# ---------------------------------------------------------------------------
# Prompt threading (query slots are requested only when web search is on)
# ---------------------------------------------------------------------------


def _smart_messages(web_search: bool):
    return get_smart_messages(
        content="Build a travel deck",
        n_slides=4,
        language="English",
        tone=None,
        verbosity=None,
        instructions=None,
        include_title_slide=True,
        include_table_of_contents=False,
        source_context="",
        community_design_context="",
        web_search=web_search,
    )


def test_smart_prompt_includes_image_query_instructions_when_web_search_on():
    prompt = str(_smart_messages(web_search=True)[1].content)

    assert "data-image-query" in prompt
    assert PLACEHOLDER_SRC in prompt
    assert "Never invent, guess, or embed remote image URLs" in prompt
    assert "concise searchable English phrase" in prompt


def test_smart_prompt_omits_image_query_instructions_when_web_search_off():
    prompt = str(_smart_messages(web_search=False)[1].content)

    assert "data-image-query" not in prompt
    assert "Web image search is enabled" not in prompt


# ---------------------------------------------------------------------------
# _stream_smart_presentation integration (search / unavailable flows)
# ---------------------------------------------------------------------------


class _ConnectedRequest:
    """Fake Starlette request that never reports a disconnect."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}

    async def is_disconnected(self) -> bool:
        return False


class _FakeSearchService:
    """Records queries and returns one scripted outcome per call."""

    def __init__(self, outcome_factory=None):
        self._outcome_factory = outcome_factory
        self.queries: list[str] = []
        self.statuses: list[str] = []

    async def search_and_import(self, query, context=None, on_status=None):
        self.queries.append(query)
        if on_status is not None:
            on_status("images.searching_web")
            on_status("images.importing")
        return self._outcome_factory(query, len(self.queries))


def _ok_outcome(_query, number):
    return ImageSearchOutcome(
        asset=ImageAsset(
            owner_id=uuid.uuid4(),
            path=f"D:/app_data/images/imported-{number}.jpg",
            is_uploaded=False,
            extras={"origin": "web_search"},
        )
    )


def _failed_outcome(_query, _number):
    return ImageSearchOutcome(asset=None, failure_code="image_search.no_results")


def _smart_presentation(presentation_id, *, web_search=True):
    now = datetime.now()
    return PresentationModel(
        id=presentation_id,
        generation_mode="smart",
        content="Build a Hanoi travel deck",
        n_slides=1,
        language="English",
        title="Deck",
        web_search=web_search,
        outlines=None,
        layout=None,
        structure=None,
        tone="default",
        verbosity="standard",
        created_at=now,
        updated_at=now,
    )


def _data_payloads(chunks):
    payloads = []
    for chunk in chunks:
        for line in str(chunk).splitlines():
            if not line.startswith("data:"):
                continue
            try:
                payloads.append(json.loads(line[len("data:") :].strip()))
            except json.JSONDecodeError:
                pass
    return payloads


async def _consume(response):
    return [chunk async for chunk in response.body_iterator]


def _run_smart_stream(monkeypatch, *, slide_html, outcome_factory=None, service=None):
    """Drive ``_stream_smart_presentation`` with a one-slide fake LLM.

    Returns ``(payloads, session, service, captured_llm_kwargs)`` where
    ``payloads`` are the parsed SSE data frames.
    """
    presentation_id = uuid.uuid4()
    presentation = _smart_presentation(presentation_id)
    session = FakeAsyncSession({presentation_id: presentation})
    if service is None:
        service = _FakeSearchService(outcome_factory)
    captured: dict = {}

    async def fake_generate_smart_presentation(**kwargs):
        captured.update(kwargs)
        await kwargs["on_slide"](0, {"title": "Hanoi", "html": slide_html})
        return {"title": "Deck", "slides": [{"title": "Hanoi", "html": slide_html}]}

    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://localhost:8888")
    monkeypatch.setattr(
        presentation_smart_stream, "generate_smart_presentation", fake_generate_smart_presentation
    )
    monkeypatch.setattr(
        presentation_smart_stream,
        "load_community_references",
        _async_list([]),
    )
    monkeypatch.setattr(
        presentation_smart_stream, "build_community_design_context", lambda _refs: ""
    )
    monkeypatch.setattr(presentation_smart_stream, "merge_reference_fonts", lambda _refs: {})
    monkeypatch.setattr(
        presentation_smart_stream,
        "get_web_search_context",
        _async_str(""),
    )
    monkeypatch.setattr(
        presentation_smart_stream, "get_image_search_service", lambda: service
    )
    monkeypatch.setattr(
        presentation_smart_stream,
        "filesystem_image_path_to_app_data_url",
        lambda path: f"/app_data/images/{Path(path).name}",
    )

    request = _ConnectedRequest()
    response = asyncio.run(
        _drive(
            presentation_smart_stream._stream_smart_presentation(
                presentation, session, request
            )
        )
    )
    return response, session, service, captured


async def _drive(coroutine):
    response = await coroutine
    return await _consume(response)


def _async_list(value):
    async def _stub(*_args, **_kwargs):
        return value

    return _stub


def _async_str(value):
    async def _stub(*_args, **_kwargs):
        return value

    return _stub


def _slide_assets_payload(payloads):
    matches = [p for p in payloads if p.get("type") == "slide_assets"]
    assert len(matches) == 1, f"expected exactly one slide_assets event, got {len(matches)}"
    return matches[0]


def test_smart_stream_search_mode_resolves_slot_and_emits_assets(monkeypatch):
    slide_html = _section(f"{_marked_img()}<p>Short caption.</p>")
    response, session, service, captured = _run_smart_stream(
        monkeypatch, slide_html=slide_html, outcome_factory=_ok_outcome
    )
    payloads = _data_payloads(response)

    # The LLM must be told web search is on (prompt threading).
    assert captured["web_search"] is True
    # Exactly one query was searched.
    assert service.queries == ["Hanoi skyline at sunset"]

    # The placeholder HTML (with its marker) streams first...
    first_slide = next(p for p in payloads if p.get("type") == "slide_html")
    assert "data-image-query" in first_slide["html"]

    # ...then the slide_assets event replaces only the intended img[src].
    assets_event = _slide_assets_payload(payloads)
    resolved_html = assets_event["slide"]["html_content"]
    assert 'src="/app_data/images/imported-1.jpg"' in resolved_html
    assert "data-image-query" not in resolved_html
    assert assets_event["warnings"] == []

    # Image-search progress streams once per phase, never blocking slides.
    status_codes = [p["status_code"] for p in payloads if p.get("type") == "status"]
    assert status_codes.count("images.searching_web") == 1
    assert status_codes.count("images.importing") == 1

    # The stored deck has local URLs, no markers, and the imported asset.
    stored_slide_models = [m for m in session.added_all if isinstance(m, SlideModel)]
    assert len(stored_slide_models) == 1
    assert 'src="/app_data/images/imported-1.jpg"' in stored_slide_models[0].html_content
    assert "data-image-query" not in stored_slide_models[0].html_content
    stored_assets = [m for m in session.added_all if isinstance(m, ImageAsset)]
    assert len(stored_assets) == 1
    assert session.commit_count == 1


def test_smart_stream_failed_slot_keeps_placeholder_with_coded_warning(monkeypatch):
    slide_html = _section(f"{_marked_img()}<p>Short caption.</p>")
    response, session, _service, _captured = _run_smart_stream(
        monkeypatch, slide_html=slide_html, outcome_factory=_failed_outcome
    )
    payloads = _data_payloads(response)

    assets_event = _slide_assets_payload(payloads)
    resolved_html = assets_event["slide"]["html_content"]
    # Valid HTML: the placeholder stays, the marker goes, a coded warning is sent.
    assert f'src="{PLACEHOLDER_SRC}"' in resolved_html
    assert "data-image-query" not in resolved_html
    assert resolved_html.startswith("<section") and resolved_html.endswith("</section>")
    assert [w["error_code"] for w in assets_event["warnings"]] == [
        "image_search.no_results"
    ]

    stored_slide_models = [m for m in session.added_all if isinstance(m, SlideModel)]
    assert f'src="{PLACEHOLDER_SRC}"' in stored_slide_models[0].html_content
    assert not [m for m in session.added_all if isinstance(m, ImageAsset)]


def test_smart_stream_unavailable_mode_neutralizes_markers_without_search(monkeypatch):
    slide_html = _section(
        f'{_marked_img(query="Hanoi skyline at sunset")}'
        '<img src="https://invented.example/x.jpg" alt="invented">'
        "<p>Short caption.</p>"
    )
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "true")
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    presentation_id = uuid.uuid4()
    presentation = _smart_presentation(presentation_id)
    session = FakeAsyncSession({presentation_id: presentation})
    captured: dict = {}

    async def fake_generate_smart_presentation(**kwargs):
        captured.update(kwargs)
        await kwargs["on_slide"](0, {"title": "Hanoi", "html": slide_html})
        return {"title": "Deck", "slides": [{"title": "Hanoi", "html": slide_html}]}

    def _forbidden_service():
        raise AssertionError("image search must not be constructed when unconfigured")

    monkeypatch.setattr(
        presentation_smart_stream,
        "generate_smart_presentation",
        fake_generate_smart_presentation,
    )
    monkeypatch.setattr(
        presentation_smart_stream, "load_community_references", _async_list([])
    )
    monkeypatch.setattr(
        presentation_smart_stream, "build_community_design_context", lambda _refs: ""
    )
    monkeypatch.setattr(presentation_smart_stream, "merge_reference_fonts", lambda _refs: {})
    monkeypatch.setattr(presentation_smart_stream, "get_web_search_context", _async_str(""))
    monkeypatch.setattr(
        presentation_smart_stream, "get_image_search_service", _forbidden_service
    )
    monkeypatch.setattr(
        presentation_smart_stream,
        "filesystem_image_path_to_app_data_url",
        lambda path: f"/app_data/images/{Path(path).name}",
    )

    response = asyncio.run(
        _drive(
            presentation_smart_stream._stream_smart_presentation(
                presentation, session, _ConnectedRequest()
            )
        )
    )
    payloads = _data_payloads(response)

    assert captured["web_search"] is True
    assets_event = _slide_assets_payload(payloads)
    resolved_html = assets_event["slide"]["html_content"]
    # Markers stripped, invented remote URL neutralized, placeholders kept.
    assert "data-image-query" not in resolved_html
    assert "invented.example" not in resolved_html
    assert resolved_html.count(f'src="{PLACEHOLDER_SRC}"') == 2
    assert [
        w["error_code"] for w in assets_event["warnings"]
    ] == [IMAGE_SEARCH_NOT_CONFIGURED_CODE]

    stored_slide_models = [m for m in session.added_all if isinstance(m, SlideModel)]
    assert "data-image-query" not in stored_slide_models[0].html_content
    assert not [m for m in session.added_all if isinstance(m, ImageAsset)]


def test_smart_stream_generation_mode_emits_no_assets_event(monkeypatch):
    # Rollout flag off: web_search=true presentation still gets the complete
    # pre-feature behavior — placeholders streamed, no slide_assets event,
    # no marker stripping pass, nothing searched.
    slide_html = _section(f"{_marked_img()}<p>Short caption.</p>")
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "false")
    presentation_id = uuid.uuid4()
    presentation = _smart_presentation(presentation_id)
    session = FakeAsyncSession({presentation_id: presentation})
    captured: dict = {}

    async def fake_generate_smart_presentation(**kwargs):
        captured.update(kwargs)
        await kwargs["on_slide"](0, {"title": "Hanoi", "html": slide_html})
        return {"title": "Deck", "slides": [{"title": "Hanoi", "html": slide_html}]}

    monkeypatch.setattr(
        presentation_smart_stream,
        "generate_smart_presentation",
        fake_generate_smart_presentation,
    )
    monkeypatch.setattr(
        presentation_smart_stream, "load_community_references", _async_list([])
    )
    monkeypatch.setattr(
        presentation_smart_stream, "build_community_design_context", lambda _refs: ""
    )
    monkeypatch.setattr(presentation_smart_stream, "merge_reference_fonts", lambda _refs: {})
    monkeypatch.setattr(presentation_smart_stream, "get_web_search_context", _async_str(""))

    response = asyncio.run(
        _drive(
            presentation_smart_stream._stream_smart_presentation(
                presentation, session, _ConnectedRequest()
            )
        )
    )
    payloads = _data_payloads(response)

    assert captured["web_search"] is True  # prompt still says search is on
    assert not [p for p in payloads if p.get("type") == "slide_assets"]
    stored_slide_models = [m for m in session.added_all if isinstance(m, SlideModel)]
    # Generation mode keeps the raw LLM HTML exactly as streamed.
    assert stored_slide_models[0].html_content == slide_html


class _CrashingSearchService:
    """Simulates an unexpected crash inside the search service."""

    async def search_and_import(self, query, context=None, on_status=None):
        raise RuntimeError("unexpected adapter crash")


def test_smart_stream_crashed_search_keeps_slide_with_coded_warning(monkeypatch):
    slide_html = _section(f"{_marked_img()}<p>Short caption.</p>")
    response, session, _service, _captured = _run_smart_stream(
        monkeypatch, slide_html=slide_html, service=_CrashingSearchService()
    )
    payloads = _data_payloads(response)

    # The crash is not silent: the slide keeps placeholder HTML with its
    # marker stripped and the user sees a coded warning.
    assets_event = _slide_assets_payload(payloads)
    resolved_html = assets_event["slide"]["html_content"]
    assert f'src="{PLACEHOLDER_SRC}"' in resolved_html
    assert "data-image-query" not in resolved_html
    assert [
        w["error_code"] for w in assets_event["warnings"]
    ] == ["image_search.placeholder_fallback"]

    stored_slide_models = [m for m in session.added_all if isinstance(m, SlideModel)]
    assert "data-image-query" not in stored_slide_models[0].html_content
    assert not [m for m in session.added_all if isinstance(m, ImageAsset)]


def test_smart_stream_caps_slots_per_slide(monkeypatch):
    queries = [f"subject {number}" for number in range(limits.max_slots_per_smart_slide + 1)]
    slide_html = _section(
        "".join(_marked_img(query=query, alt=query) for query in queries)
        + "<p>Short caption.</p>"
    )
    response, session, service, _captured = _run_smart_stream(
        monkeypatch, slide_html=slide_html, outcome_factory=_ok_outcome
    )
    payloads = _data_payloads(response)

    # Only the first max_slots_per_smart_slide slots are searched.
    assert len(service.queries) == limits.max_slots_per_smart_slide
    assert service.queries == queries[: limits.max_slots_per_smart_slide]

    assets_event = _slide_assets_payload(payloads)
    resolved_html = assets_event["slide"]["html_content"]
    assert "data-image-query" not in resolved_html
    # Every resolved slot got a distinct local URL; the overflowed slot keeps
    # its placeholder.
    assert resolved_html.count("src=\"/app_data/images/imported-") == limits.max_slots_per_smart_slide
    assert f'src="{PLACEHOLDER_SRC}"' in resolved_html
    stored_assets = [m for m in session.added_all if isinstance(m, ImageAsset)]
    assert len(stored_assets) == limits.max_slots_per_smart_slide