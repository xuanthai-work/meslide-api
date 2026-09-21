from contextvars import ContextVar, Token
import uuid


_CURRENT_OWNER_ID: ContextVar[uuid.UUID | None] = ContextVar(
    "gslide_current_owner_id", default=None
)


def get_current_owner_id() -> uuid.UUID | None:
    """Return the current request owner, or None outside a request.

    SessionAuthMiddleware sets this ContextVar before handlers run. Callers
    that treat ``None`` as "no request context" can use this or
    :func:`get_current_owner_id_raw` interchangeably.
    """
    return get_current_owner_id_raw()


def get_current_owner_id_raw() -> uuid.UUID | None:
    """Return the raw ContextVar value without synthesizing an owner.

    Use this in places that treat ``None`` as "no request context" and skip
    work (ORM owner-scoping/stamping event listeners, temp-file root cleanup,
    internal-header guard, model ``owner_id`` default_factory for shared
    rows).
    """
    return _CURRENT_OWNER_ID.get()


def set_current_owner_id(owner_id: uuid.UUID | None) -> Token:
    return _CURRENT_OWNER_ID.set(owner_id)


def reset_current_owner_id(token: Token) -> None:
    _CURRENT_OWNER_ID.reset(token)
