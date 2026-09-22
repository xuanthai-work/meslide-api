import asyncio
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from api.v1.ppt.endpoints import presentation as presentation_endpoint
from models.sql.presentation import PresentationModel, PresentationVersion
from models.sql.slide import SlideModel
from tests.conftest import FakeAsyncSession


def _template_layout_payload() -> dict:
    return {
        "layouts": [
            {
                "id": "template-layout-1",
                "description": "Hero layout",
                "components": [
                    {
                        "id": "hero",
                        "description": "Hero content",
                        "elements": [
                            {
                                "type": "text",
                                "decorative": False,
                                "name": "headline",
                                "runs": [{"text": "Original headline"}],
                            }
                        ],
                    }
                ],
            }
        ]
    }


class _SessionWithSlides(FakeAsyncSession):
    def __init__(self, presentation: PresentationModel, slides: list[SlideModel]):
        super().__init__(get_results={presentation.id: presentation})
        self._slides = slides

    async def scalars(self, *_args, **_kwargs):
        return list(self._slides)


def _presentation(presentation_id: uuid.UUID) -> PresentationModel:
    now = datetime.now()
    return PresentationModel(
        id=presentation_id,
        version=PresentationVersion.V2_STANDARD,
        content="deck",
        n_slides=2,
        language="English",
        title="Deck",
        outlines={"slides": [{"content": "## Causes"}, {"content": "## Effects"}]},
        layout=_template_layout_payload(),
        structure={"slides": [0, 0]},
        tone="default",
        verbosity="standard",
        instructions=None,
        created_at=now,
        updated_at=now,
    )


def _saved_slide(presentation_id: uuid.UUID) -> SlideModel:
    return SlideModel(
        presentation=presentation_id,
        layout_group="template",
        layout="template-layout-1",
        index=0,
        content={"hero": {"headline": "Already done"}},
        speaker_note="kept",
        ui={"id": "template-layout-1"},
    )


def _consume(session, slide_content):
    async def consume_stream():
        response = await presentation_endpoint.stream_presentation(
            id=next(iter(session._get_results)),
            sql_session=session,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    with patch.object(
        presentation_endpoint,
        "get_slide_content_from_type_and_outline",
        new=slide_content,
    ), patch.object(
        presentation_endpoint,
        "process_slide_and_fetch_assets",
        new=AsyncMock(return_value=[]),
    ), patch.object(
        presentation_endpoint,
        "get_images_directory",
        return_value="/tmp",
    ), patch.object(
        presentation_endpoint,
        "ImageGenerationService",
        return_value=Mock(),
    ):
        return asyncio.run(consume_stream())


def test_stream_reuses_saved_slide_and_generates_only_the_remainder():
    presentation_id = uuid.uuid4()
    presentation = _presentation(presentation_id)
    saved = _saved_slide(presentation_id)
    session = _SessionWithSlides(presentation, [saved])
    slide_calls = 0

    async def fake_slide_content(*_args, **_kwargs):
        nonlocal slide_calls
        slide_calls += 1
        return {"hero": {"headline": "Effects"}, "__speaker_note__": ""}

    chunks = _consume(session, fake_slide_content)
    body = "".join(chunks)

    assert slide_calls == 1
    assert "Already done" in body
    assert any(
        isinstance(obj, SlideModel) and obj.index == 1 for obj in session.added
    )
    assert session.commit_count >= 1
    assert saved not in session.deleted


def test_stream_commits_a_finished_slide_before_the_next_llm_call():
    presentation_id = uuid.uuid4()
    presentation = _presentation(presentation_id)
    session = FakeAsyncSession(get_results={presentation_id: presentation})
    slide_calls = 0

    async def fake_slide_content(*_args, **_kwargs):
        nonlocal slide_calls
        slide_calls += 1
        if slide_calls == 1:
            return {"hero": {"headline": "Causes"}, "__speaker_note__": ""}
        raise HTTPException(status_code=502, detail="LLM died")

    chunks = _consume(session, fake_slide_content)

    assert slide_calls == 2
    assert "LLM died" in "".join(chunks)
    assert any(isinstance(obj, SlideModel) and obj.index == 0 for obj in session.added)
    assert session.commit_count >= 1


def test_stream_keeps_saved_slide_when_the_next_llm_call_fails():
    presentation_id = uuid.uuid4()
    presentation = _presentation(presentation_id)
    saved = _saved_slide(presentation_id)
    session = _SessionWithSlides(presentation, [saved])

    async def failing_slide_content(*_args, **_kwargs):
        raise HTTPException(status_code=502, detail="LLM died")

    chunks = _consume(session, failing_slide_content)
    body = "".join(chunks)

    assert "Already done" in body
    assert "LLM died" in body
    assert saved not in session.deleted
    assert not any(
        isinstance(obj, SlideModel) and obj.index == 1 for obj in session.added
    )
