"""Contract tests: `presentation.web_search` persists across every flow.

The automatic web image-search feature (docs/gslide-searxng-image-search-spec.md
FR-1) reuses the persisted per-presentation `web_search` flag as its only
user switch. These tests lock the contract that create, duplicate/derive
(`get_new_presentation`), regenerate (stream with `?regenerate=true`), and the
legacy `/generate` handler all preserve the value, and that no endpoint
mutates it after creation.
"""

import uuid
from pathlib import Path

from api.v1.ppt.endpoints import presentation_generate
from api.v1.ppt.endpoints import presentation_smart_stream
from models.sql.presentation import PresentationModel
from tests.conftest import FakeAsyncSession

import api.v1.ppt.endpoints.presentation as presentation_endpoint


def _endpoint_source() -> str:
    return Path(presentation_endpoint.__file__).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Create endpoint persists the request value (both states)
# ---------------------------------------------------------------------------


def test_create_presentation_persists_web_search_enabled():
    from api.v1.ppt.endpoints.presentation import create_presentation

    import asyncio

    session = FakeAsyncSession()
    created = asyncio.run(
        create_presentation(content="Topic", web_search=True, sql_session=session)
    )
    assert created.web_search is True
    # The same row object was handed to the session for persistence.
    assert session.added == [created]


def test_create_presentation_persists_web_search_default_false():
    from api.v1.ppt.endpoints.presentation import create_presentation

    import asyncio

    session = FakeAsyncSession()
    created = asyncio.run(
        create_presentation(content="Topic", sql_session=session)
    )
    assert created.web_search is False


# ---------------------------------------------------------------------------
# Duplicate / derive copy the flag (both go through get_new_presentation)
# ---------------------------------------------------------------------------


def test_get_new_presentation_copies_web_search():
    original = PresentationModel(content="Topic", web_search=True)
    copy = original.get_new_presentation()
    assert copy.web_search is True
    assert copy.id != original.id


def test_duplicate_presentation_copies_web_search():
    from datetime import datetime, timezone
    from unittest import mock

    from api.v1.ppt.endpoints.presentation import duplicate_presentation

    import asyncio

    presentation_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    original = PresentationModel(
        id=presentation_id,
        content="Topic",
        n_slides=3,
        web_search=True,
        created_at=now,
        updated_at=now,
    )
    session = FakeAsyncSession(get_results={presentation_id: original})

    # get_new_presentation() does not carry DB-managed timestamps, so the
    # response wrapper would fail validation; the contract under test is the
    # persisted row, not response serialization.
    with mock.patch.object(presentation_endpoint, "PresentationWithSlides"):
        with mock.patch.object(
            presentation_endpoint._presentation_crud, "PresentationWithSlides"
        ):
            asyncio.run(
                duplicate_presentation(id=presentation_id, sql_session=session)
            )

    persisted = session.added[0]
    assert isinstance(persisted, PresentationModel)
    assert persisted.web_search is True


# ---------------------------------------------------------------------------
# Legacy `/generate` handler must persist web_search on the created row
# (it consumes `request.web_search` for outline generation, so dropping it
# on the persisted row would split the two pipelines' switch).
# ---------------------------------------------------------------------------


def test_legacy_generate_handler_persists_web_search_in_source():
    source = Path(presentation_generate.__file__).read_text(encoding="utf-8")
    handler_start = source.index("async def generate_presentation_handler(")
    handler_body = source[handler_start:]
    model_start = handler_body.index("presentation = PresentationModel(")
    # Match the construction's real closing paren (nested calls contain
    # their own parens, so simple search would stop early).
    depth = 0
    model_end = None
    for offset, char in enumerate(handler_body[model_start:], start=model_start):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                model_end = offset + 1
                break
    assert model_end is not None
    model_construction = handler_body[model_start:model_end]
    assert "web_search=request.web_search" in model_construction, (
        "legacy /generate handler must persist request.web_search on the "
        "PresentationModel row"
    )


# ---------------------------------------------------------------------------
# No flow may mutate web_search after creation: stream (regenerate), prepare,
# mode switch, and update must only read it. A write would silently flip the
# user's choice mid-lifecycle.
# ---------------------------------------------------------------------------


def test_no_endpoint_reassigns_web_search_after_creation():
    source = _endpoint_source()
    assert "presentation.web_search =" not in source, (
        "presentation.web_search must be immutable after create; regeneration, "
        "prepare, and update flows must only read it"
    )


def test_stream_endpoint_reads_web_search_for_smart_flow():
    # The Smart streaming flow lives in presentation_smart_stream now.
    smart_source = Path(presentation_smart_stream.__file__).read_text(
        encoding="utf-8"
    )
    smart_start = smart_source.index("async def _stream_smart_presentation(")
    smart_body = smart_source[smart_start:]
    assert "presentation.web_search" in smart_body, (
        "Smart stream must branch text (and later image) search on the "
        "persisted presentation.web_search value"
    )