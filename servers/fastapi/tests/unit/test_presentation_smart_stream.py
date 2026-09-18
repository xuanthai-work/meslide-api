"""Unit tests for the presentation smart-stream and response seams.

``_stream_smart_presentation`` (the Smart HTML streaming flow) and
``_presentation_response_data`` (the shared response serialization) used to
live inside ``presentation.py``; they now live in dedicated modules so the
router module only orchestrates. The Smart pipeline itself is covered by
the streaming/image-search suites through the endpoint re-export; these
tests pin the module boundaries directly.
"""

from api.v1.ppt.endpoints.presentation_responses import (
    _presentation_response_data,
)
from api.v1.ppt.endpoints.presentation_smart_stream import (
    _stream_smart_presentation,
)


def test_smart_stream_is_an_async_function():
    import inspect

    assert inspect.iscoroutinefunction(_stream_smart_presentation)


def test_presentation_response_data_marks_generation_mode():
    from models.sql.presentation import PresentationModel

    presentation = PresentationModel(
        title="Deck", generation_mode="smart"
    )
    data = _presentation_response_data(presentation)

    assert data["type"] == "smart"
    # Heavy payload fields stay out of the standard response.
    assert "layout" not in data
    assert "structure" not in data
    assert "theme" not in data


def test_presentation_response_data_defaults_standard_mode():
    from models.sql.presentation import PresentationModel

    data = _presentation_response_data(PresentationModel(title="Deck"))

    assert data["type"] == "standard"