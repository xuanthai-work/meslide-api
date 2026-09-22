import asyncio
import inspect
import uuid
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch

from api.v1.ppt.endpoints import presentation as presentation_endpoint
from models.sql.presentation import PresentationModel, PresentationVersion
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


class _DisconnectRequest:
    """Fake Starlette request with a controllable bound ``is_disconnected``."""

    def __init__(self):
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self._calls = 0

    async def is_disconnected(self) -> bool:
        self._calls += 1
        # False on the first poll (let slide 1 run), True thereafter (stop before slide 2).
        return self._calls > 1


# ---------------------------------------------------------------------------
# Step 1a: mandatory source-level signature tests
# ---------------------------------------------------------------------------


def test_stream_presentation_accepts_request():
    params = inspect.signature(presentation_endpoint.stream_presentation).parameters
    assert "request" in params


# ---------------------------------------------------------------------------
# Step 1b: behavioral test that the standard loop passes the disconnect
# checker and stops before the next slide LLM call once disconnected.
# ---------------------------------------------------------------------------


def test_standard_stream_passes_disconnect_checker_and_stops():
    presentation_id = uuid.uuid4()
    now = datetime.now()
    presentation = PresentationModel(
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
    session = FakeAsyncSession(get_results={presentation_id: presentation})

    captured_checkers: list = []
    slide_calls = 0

    async def fake_slide_content(slide_layout, *_args, **kwargs):
        nonlocal slide_calls
        slide_calls += 1
        captured_checkers.append(kwargs.get("disconnect_checker"))
        return {"hero": {"headline": "Causes"}, "__speaker_note__": ""}

    request = _DisconnectRequest()

    async def consume_stream():
        response = await presentation_endpoint.stream_presentation(
            id=presentation_id,
            request=request,
            sql_session=session,
        )
        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    with patch.object(
        presentation_endpoint,
        "get_slide_content_from_type_and_outline",
        new=fake_slide_content,
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
        asyncio.run(consume_stream())

    # The standard loop must pass request.is_disconnected as the disconnect_checker.
    # Bound methods are created fresh on each attribute access, so compare the
    # underlying function and instance rather than identity.
    assert captured_checkers, "disconnect_checker was never passed to the LLM call"
    checker = captured_checkers[0]
    assert checker is not None
    assert checker.__self__ is request
    assert checker.__name__ == "is_disconnected"

    # After disconnect, the loop must not start the next slide's LLM call.
    assert slide_calls == 1, f"expected 1 slide LLM call, got {slide_calls}"
    # The pre-iteration disconnect poll must have run for the second slide.
    assert request._calls >= 2


# ---------------------------------------------------------------------------
# Confirm CancelledError propagation semantics: ``safe_sse_stream`` must
# treat CancelledError as a clean stop (no error event). This is a regression
# guard for the contract the wiring relies on.
# ---------------------------------------------------------------------------


def test_safe_sse_stream_swallows_cancelled_error_cleanly():
    import asyncio

    from utils.sse import safe_sse_stream

    async def inner():
        raise asyncio.CancelledError
        yield  # pragma: no cover - makes inner an async generator

    async def collect():
        return [
            frame
            async for frame in safe_sse_stream(
                inner(),
                logger=__import__("logging").getLogger("test"),
                error_detail="boom",
            )
        ]

    assert asyncio.run(collect()) == []
