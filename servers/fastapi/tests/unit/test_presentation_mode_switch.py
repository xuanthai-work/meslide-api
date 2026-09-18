import asyncio
import uuid

import pytest
from fastapi import HTTPException

from api.v1.ppt.endpoints.presentation import switch_presentation_mode
from models.sql.presentation import PresentationModel, PresentationVersion
from tests.conftest import FakeAsyncSession


def _presentation(**overrides) -> PresentationModel:
    values = {
        "id": uuid.uuid4(),
        "version": PresentationVersion.V2_STANDARD,
        "content": "Quarterly business review",
        "n_slides": 5,
        "language": "English",
        "generation_mode": "standard",
        "outlines": {"slides": [{"content": "Overview"}]},
        "structure": {"slides": [0]},
        "layout": {"slides": [{"id": "title"}]},
    }
    values.update(overrides)
    return PresentationModel(**values)


def test_switches_standard_presentation_to_smart():
    presentation = _presentation()
    session = FakeAsyncSession(get_results={presentation.id: presentation})

    result = asyncio.run(
        switch_presentation_mode(
            id=presentation.id,
            generation_mode="smart",
            sql_session=session,
        )
    )

    assert result is presentation
    assert presentation.generation_mode == "smart"
    assert session.added == [presentation]
    assert session.commit_count == 1


def test_switches_smart_presentation_back_when_standard_data_is_available():
    presentation = _presentation(generation_mode="smart")
    session = FakeAsyncSession(get_results={presentation.id: presentation})

    asyncio.run(
        switch_presentation_mode(
            id=presentation.id,
            generation_mode="standard",
            sql_session=session,
        )
    )

    assert presentation.generation_mode == "standard"
    assert session.commit_count == 1


def test_rejects_legacy_smart_deck_without_standard_structure():
    presentation = _presentation(
        generation_mode="smart",
        outlines=None,
        structure=None,
        layout=None,
    )
    session = FakeAsyncSession(get_results={presentation.id: presentation})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            switch_presentation_mode(
                id=presentation.id,
                generation_mode="standard",
                sql_session=session,
            )
        )

    assert exc_info.value.status_code == 409
    assert session.commit_count == 0


def test_rejects_smart_mode_without_source_content():
    presentation = _presentation(content="", file_paths=None)
    session = FakeAsyncSession(get_results={presentation.id: presentation})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            switch_presentation_mode(
                id=presentation.id,
                generation_mode="smart",
                sql_session=session,
            )
        )

    assert exc_info.value.status_code == 409
    assert session.commit_count == 0
