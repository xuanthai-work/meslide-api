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


def test_get_current_owner_id_returns_demo_when_unset():
    """I1: defensive fallback — when no owner was set in the ContextVar,
    get_current_owner_id must return the DEMO_USER_ID (not None). The
    middleware is the primary setter; this is the safety net."""
    _reset_owner_var()
    owner = get_current_owner_id()
    assert owner == uuid.UUID("00000000-0000-0000-0000-000000000001")


def test_get_current_owner_id_returns_set_owner_when_present():
    """Primary path: when the middleware set an owner, that owner wins."""
    other = uuid.UUID("11111111-1111-1111-1111-111111111111")
    token = set_current_owner_id(other)
    try:
        assert get_current_owner_id() == other
    finally:
        _CURRENT_OWNER_ID.reset(token)