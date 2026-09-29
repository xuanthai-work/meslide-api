"""Standard structured-output image-search integration tests (Phase 5).

The single resolution branch: web_search on + rollout flag on + SearXNG
configured -> search/import (never paid generation); rollout flag off ->
complete pre-feature generation behavior; flag on but unconfigured ->
placeholder + coded warning. Old structured output without ``image_query``
stays valid through the deterministic prompt fallback.
"""

import asyncio
import os
import uuid


import utils.process_slides as process_slides
from models.sql.slide import SlideModel
from services.image_search.models import (
    DeckResolutionContext,
    ImageSearchOutcome,
)
from templates.v2.content import template_asset_prompt
from templates.v2.schema import (
    _component_content_field_schema,
    _content_schema_for_element,
)
from utils.llm_calls import generate_slide_content
from utils.llm_calls.generate_slide_content import (
    get_messages,
    get_system_prompt,
)

PLACEHOLDER = "/static/images/placeholder.jpg"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeAsset:
    def __init__(self, path="/data/images/imported.jpeg"):
        self.path = path
        self.id = uuid.uuid4()
        self.owner_id = None


class FakeImageSearchService:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.queries = []
        self.contexts = []

    async def search_and_import(
        self, query, *, owner_id=None, aspect_ratio=None, context=None, on_status=None
    ):
        self.queries.append(query)
        self.contexts.append(context)
        if not self.outcomes:
            return ImageSearchOutcome(asset=FakeAsset())
        if len(self.outcomes) > 1:
            return self.outcomes.pop(0)
        return self.outcomes[0]


class FakeGenerationService:
    def __init__(self, results=None):
        self.prompts = []
        self.results = list(results or [])

    async def generate_image(self, image_prompt):
        self.prompts.append(image_prompt.prompt)
        if not self.results:
            return "https://generated.example.org/image.png"
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


class FakeIconFinder:
    def __init__(self):
        self.queries = []

    async def search_icons(self, query, weight=None):
        self.queries.append(query)
        return []


def _slide(content, *, ui=None):
    return SlideModel(
        presentation=uuid.uuid4(),
        layout_group="group",
        layout="layout",
        index=0,
        content=content,
        ui=ui,
    )


def _enable_search(monkeypatch, search_service, *, configured=True):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: True)
    monkeypatch.setattr(process_slides, "web_image_search_configured", lambda: configured)
    monkeypatch.setattr(
        process_slides, "get_image_search_service", lambda: search_service
    )
    monkeypatch.setattr(
        process_slides, "ICON_FINDER_SERVICE", FakeIconFinder()
    )
    monkeypatch.setattr(
        process_slides,
        "filesystem_image_path_to_app_data_url",
        lambda path: "/app_data/images/" + os.path.basename(path),
    )


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_mode_generation_when_web_search_off(monkeypatch):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: True)
    monkeypatch.setattr(process_slides, "web_image_search_configured", lambda: True)
    assert (
        process_slides.resolve_web_image_search_mode(False)
        == process_slides.IMAGE_SEARCH_MODE_GENERATION
    )


def test_mode_generation_when_rollout_flag_off(monkeypatch):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: False)
    assert (
        process_slides.resolve_web_image_search_mode(True)
        == process_slides.IMAGE_SEARCH_MODE_GENERATION
    )


def test_mode_unavailable_when_flag_on_but_unconfigured(monkeypatch):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: True)
    monkeypatch.setattr(process_slides, "web_image_search_configured", lambda: False)
    assert (
        process_slides.resolve_web_image_search_mode(True)
        == process_slides.IMAGE_SEARCH_MODE_UNAVAILABLE
    )


def test_mode_search_when_enabled_and_configured(monkeypatch):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: True)
    monkeypatch.setattr(process_slides, "web_image_search_configured", lambda: True)
    assert (
        process_slides.resolve_web_image_search_mode(True)
        == process_slides.IMAGE_SEARCH_MODE_SEARCH
    )


# ---------------------------------------------------------------------------
# process_slide_and_fetch_assets branch
# ---------------------------------------------------------------------------


def test_search_mode_searches_and_never_generates(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    generation_service = FakeGenerationService()
    slide = _slide({"hero": {"image_prompt": "A solar farm", "image_query": "solar farm"}})

    assets = asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            generation_service,
            slide,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert search_service.queries == ["solar farm"]
    assert generation_service.prompts == []
    assert len(assets) == 1
    assert slide.content["hero"]["image_url"] == "/app_data/images/imported.jpeg"
    # SearXNG-sourced photos default to "contain" so nothing is cropped.
    assert slide.content["hero"]["fit"] == "contain"


def test_search_failure_places_placeholder_and_coded_warning(monkeypatch):
    search_service = FakeImageSearchService(
        outcomes=[ImageSearchOutcome(failure_code="image_search.no_usable_candidate")]
    )
    _enable_search(monkeypatch, search_service)
    slide = _slide({"hero": {"image_prompt": "x", "image_query": "q"}})
    warnings: list[dict] = []

    assets = asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            image_warnings=warnings,
            web_search=True,
        )
    )

    assert assets == []
    assert slide.content["hero"]["image_url"] == PLACEHOLDER
    assert len(warnings) == 1
    assert warnings[0]["error_code"] == "image_search.no_usable_candidate"


def test_unconfigured_mode_placeholder_without_search_or_generation(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service, configured=False)
    generation_service = FakeGenerationService()
    slide = _slide({"hero": {"image_prompt": "A solar farm"}})
    warnings: list[dict] = []

    assets = asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            generation_service,
            slide,
            allow_image_fallback=True,
            image_warnings=warnings,
            web_search=True,
        )
    )

    assert assets == []
    assert slide.content["hero"]["image_url"] == PLACEHOLDER
    assert search_service.queries == []
    assert generation_service.prompts == []
    assert warnings[0]["error_code"] == "image_search.not_configured"


def test_rollout_flag_off_preserves_generation_behavior(monkeypatch):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: False)
    monkeypatch.setattr(
        process_slides,
        "get_image_search_service",
        lambda: (_ for _ in ()).throw(AssertionError("must not build search service")),
    )
    generation_service = FakeGenerationService()
    slide = _slide(
        {"hero": {"image_prompt": "A solar farm", "image_query": "solar farm"}}
    )

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            generation_service,
            slide,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert generation_service.prompts == ["A solar farm"]
    assert slide.content["hero"]["image_url"] == "https://generated.example.org/image.png"


def test_web_search_off_uses_generation(monkeypatch):
    monkeypatch.setattr(process_slides, "web_image_search_feature_enabled", lambda: True)
    monkeypatch.setattr(process_slides, "web_image_search_configured", lambda: True)
    monkeypatch.setattr(
        process_slides,
        "get_image_search_service",
        lambda: (_ for _ in ()).throw(AssertionError("must not build search service")),
    )
    generation_service = FakeGenerationService()
    slide = _slide({"hero": {"image_prompt": "A solar farm"}})

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            generation_service,
            slide,
            allow_image_fallback=True,
            web_search=False,
        )
    )

    assert generation_service.prompts == ["A solar farm"]


def test_outline_url_bypasses_search_and_generation(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    generation_service = FakeGenerationService()
    slide = _slide({"hero": {"image_prompt": "A solar farm", "image_query": "solar farm"}})

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            generation_service,
            slide,
            outline_image_urls=["https://source.example.org/photo.jpg"],
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert slide.content["hero"]["image_url"] == "https://source.example.org/photo.jpg"
    # Web-search outline images also default to "contain".
    assert slide.content["hero"]["fit"] == "contain"
    assert search_service.queries == []
    assert generation_service.prompts == []


def test_image_query_used_when_present(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    slide = _slide(
        {"hero": {"image_prompt": "stylized prompt ignored", "image_query": "Hanoi skyline"}}
    )

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert search_service.queries == ["Hanoi skyline"]


def test_image_query_fallback_normalizes_image_prompt(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    slide = _slide(
        {"hero": {"image_prompt": "Cinematic 4k photo of Vietnam solar farm"}}
    )

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert search_service.queries == ["Vietnam solar farm"]


def test_generic_icon_query_slot_never_triggers_image_search(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    icon_finder = FakeIconFinder()
    monkeypatch.setattr(process_slides, "ICON_FINDER_SERVICE", icon_finder)
    slide = _slide({"icon_slot": {"query": "sun icon"}})

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert search_service.queries == []
    assert icon_finder.queries == ["sun icon"]


def test_deck_context_passed_to_search(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    context = DeckResolutionContext()
    slide = _slide({"hero": {"image_query": "solar farm"}})

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            web_search=True,
            deck_image_context=context,
        )
    )

    assert search_service.contexts == [context]


def test_search_cancellation_places_placeholder_without_warning(monkeypatch):
    class CancellingSearchService:
        queries = []

        async def search_and_import(self, query, **kwargs):
            type(self).queries.append(query)
            raise asyncio.CancelledError()

    _enable_search(monkeypatch, CancellingSearchService())
    slide = _slide({"hero": {"image_query": "solar farm"}})
    warnings: list[dict] = []

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            image_warnings=warnings,
            web_search=True,
        )
    )

    assert slide.content["hero"]["image_url"] == PLACEHOLDER
    assert warnings == []


# ---------------------------------------------------------------------------
# process_old_and_new_slides_and_fetch_assets
# ---------------------------------------------------------------------------


def test_old_and_new_reuses_image_by_query(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    old_content = {
        "hero": {
            "image_prompt": "old prompt",
            "image_query": "solar farm",
            "image_url": "/app_data/images/old.jpeg",
        }
    }
    new_content = {"hero": {"image_prompt": "new prompt", "image_query": "solar farm"}}

    assets = asyncio.run(
        process_slides.process_old_and_new_slides_and_fetch_assets(
            FakeGenerationService(),
            old_content,
            new_content,
            use_template_asset_fields=True,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert new_content["hero"]["image_url"] == "/app_data/images/old.jpeg"
    assert assets == []
    assert search_service.queries == []


def test_old_and_new_searches_changed_query(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    old_content = {
        "hero": {
            "image_prompt": "old prompt",
            "image_query": "solar farm",
            "image_url": "/app_data/images/old.jpeg",
        }
    }
    new_content = {"hero": {"image_prompt": "new prompt", "image_query": "Hanoi skyline"}}

    assets = asyncio.run(
        process_slides.process_old_and_new_slides_and_fetch_assets(
            FakeGenerationService(),
            old_content,
            new_content,
            use_template_asset_fields=True,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    assert search_service.queries == ["Hanoi skyline"]
    assert new_content["hero"]["image_url"] == "/app_data/images/imported.jpeg"
    assert len(assets) == 1
    # Regenerated slides fetch through SearXNG, so they default to "contain".
    assert new_content["hero"]["fit"] == "contain"


def test_old_and_new_unconfigured_warning(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service, configured=False)
    new_content = {"hero": {"image_prompt": "p"}}
    warnings: list[dict] = []

    asyncio.run(
        process_slides.process_old_and_new_slides_and_fetch_assets(
            FakeGenerationService(),
            {},
            new_content,
            use_template_asset_fields=True,
            allow_image_fallback=True,
            image_warnings=warnings,
            web_search=True,
        )
    )

    assert new_content["hero"]["image_url"] == PLACEHOLDER
    assert warnings[0]["error_code"] == "image_search.not_configured"


# ---------------------------------------------------------------------------
# Slide-content LLM prompt (insertion point B)
# ---------------------------------------------------------------------------


def test_system_prompt_includes_image_search_rules_only_when_web_search():
    with_rules = get_system_prompt(web_search=True)
    without_rules = get_system_prompt(web_search=False)
    assert "image_query" in with_rules
    assert "image_query" not in without_rules


def test_get_messages_threads_web_search():
    messages = get_messages("outline", "English", web_search=True)
    system_content = messages[0].content
    assert "Web Image Search Rules" in system_content


def test_get_slide_content_threads_web_search_to_messages(monkeypatch):
    captured = {}

    class FakeStructuredGenerator:
        pass

    async def fake_generate(client, model, **kwargs):
        captured["messages"] = kwargs["messages"]
        return {}

    monkeypatch.setattr(
        generate_slide_content,
        "generate_structured_with_schema_retries",
        fake_generate,
    )
    monkeypatch.setattr(
        generate_slide_content, "get_llm_client", lambda: object()
    )
    monkeypatch.setattr(generate_slide_content, "get_model", lambda: "m")

    class FakeLayout:
        json_schema = {"type": "object", "properties": {"title": {"type": "string"}}}

    class FakeOutline:
        content = "outline"

    asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            FakeLayout(), FakeOutline(), "English", web_search=True
        )
    )
    assert "Web Image Search Rules" in captured["messages"][0].content


# ---------------------------------------------------------------------------
# Schema (insertion point A)
# ---------------------------------------------------------------------------


def test_element_image_schema_has_optional_image_query():
    schema = _content_schema_for_element({"type": "image"})
    assert set(schema["properties"]) == {"image_prompt", "image_query"}
    assert schema["required"] == ["image_prompt"]
    assert schema["additionalProperties"] is False


def test_element_icon_schema_unchanged():
    schema = _content_schema_for_element({"type": "image", "is_icon": True})
    assert set(schema["properties"]) == {"icon_query"}
    assert schema["required"] == ["icon_query"]


def test_component_image_schema_has_optional_image_query():
    field = {"element": {"type": "image"}, "name": "hero_image", "path": ["hero_image"]}
    schema = _component_content_field_schema(field)
    properties = schema["properties"]
    assert set(properties) == {"image_prompt", "image_query"}
    assert schema["required"] == ["image_prompt"]


def test_component_icon_schema_unchanged():
    field = {
        "element": {"type": "image", "is_icon": True},
        "name": "icon",
        "path": ["icon"],
    }
    schema = _component_content_field_schema(field)
    assert set(schema["properties"]) == {"icon_query"}


# ---------------------------------------------------------------------------
# template_asset_prompt reads image_query
# ---------------------------------------------------------------------------


def test_template_asset_prompt_prefers_image_prompt():
    value = {"image_prompt": "gen prompt", "image_query": "search query"}
    assert template_asset_prompt(value, is_icon=False) == "gen prompt"


def test_template_asset_prompt_reads_image_query():
    assert template_asset_prompt({"image_query": "search query"}, is_icon=False) == (
        "search query"
    )


def test_template_asset_prompt_ignores_image_query_for_icons():
    assert template_asset_prompt({"image_query": "search query"}, is_icon=True) is None


def test_old_structured_output_without_image_query_still_valid(monkeypatch):
    search_service = FakeImageSearchService()
    _enable_search(monkeypatch, search_service)
    slide = _slide({"hero": {"image_prompt": "A photo of a wind turbine"}})

    asyncio.run(
        process_slides.process_slide_and_fetch_assets(
            FakeGenerationService(),
            slide,
            allow_image_fallback=True,
            web_search=True,
        )
    )

    # Fallback query is the normalized prompt ("a photo of" stripped).
    assert search_service.queries == ["a wind turbine"]
    assert slide.content["hero"]["image_url"] == "/app_data/images/imported.jpeg"