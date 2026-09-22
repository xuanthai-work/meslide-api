"""Shared presentation response serialization.

Extracted from ``api.v1.ppt.endpoints.presentation`` so both the router and
the streaming modules can build responses without importing the router
module (which would be circular).
"""

from models.sql.presentation import PresentationModel


def _presentation_response_data(presentation: PresentationModel) -> dict:
    return presentation.model_dump(exclude={"layout", "structure", "theme"})


