from contextvars import ContextVar, Token
import uuid


_CURRENT_OWNER_ID: ContextVar[uuid.UUID | None] = ContextVar(
    "gslide_current_owner_id", default=None
)


def get_current_owner_id() -> uuid.UUID | None:
    """Return the current request owner, with a defensive demo-user fallback.

    The primary path is SessionAuthMiddleware, which sets the owner ContextVar
    before handlers run. When no owner was set (a code path that runs outside
    the middleware), fall back to the demo user id so request-handler code
    always sees a concrete owner. Callers that use ``None`` as a "no request
    context" sentinel (ORM event listeners, temp-file service, internal-header
    guard, model default_factory) must use :func:`get_current_owner_id_raw`
    instead, otherwise this fallback would collapse the "no context" case
    with the "demo owner" case and break scoping/stamping/skipping logic.
    """
    owner = _CURRENT_OWNER_ID.get()
    if owner is not None:
        return owner
    from api.v1.auth.demo_user import DEMO_USER_ID

    return DEMO_USER_ID


def get_current_owner_id_raw() -> uuid.UUID | None:
    """Return the raw ContextVar value WITHOUT the demo-user fallback.

    Use this in places that treat ``None`` as "no request context" and skip
    work (ORM owner-scoping/stamping event listeners, temp-file root cleanup,
    internal-header guard, model ``owner_id`` default_factory for shared
    rows). Prefer :func:`get_current_owner_id` in request-handler code.
    """
    return _CURRENT_OWNER_ID.get()


def set_current_owner_id(owner_id: uuid.UUID | None) -> Token:
    return _CURRENT_OWNER_ID.set(owner_id)


def reset_current_owner_id(token: Token) -> None:
    _CURRENT_OWNER_ID.reset(token)
