import os
import tempfile

# Set APP_DATA_DIRECTORY before importing auth modules (Windows writable path).
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

import uuid

from api.v1.auth.context import (
    _CURRENT_OWNER_ID,
    get_current_owner_id,
    set_current_owner_id,
)


def _reset_owner_var():
    """Force the ContextVar back to its default (None) for a clean slate."""
    token = set_current_owner_id(None)
    # discard the token; we intentionally do not reset to a prior value
    return token


def test_get_current_owner_id_returns_none_when_unset():
    """When no owner was set in the ContextVar, return None.

    Request handlers get an owner from SessionAuthMiddleware. Code outside
    that middleware must not inherit a leftover demo-user id.
    """
    _reset_owner_var()
    assert get_current_owner_id() is None


def test_get_current_owner_id_returns_set_owner_when_present():
    """Primary path: when the middleware set an owner, that owner wins."""
    other = uuid.UUID("11111111-1111-1111-1111-111111111111")
    token = set_current_owner_id(other)
    try:
        assert get_current_owner_id() == other
    finally:
        _CURRENT_OWNER_ID.reset(token)