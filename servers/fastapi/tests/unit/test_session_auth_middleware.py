import asyncio

from starlette.requests import Request
from starlette.responses import Response

from api import middlewares
from api.middlewares import SessionAuthMiddleware


def test_only_shared_app_data_asset_prefixes_do_not_require_auth():
    middleware = SessionAuthMiddleware(app=None)

    assert middleware._requires_auth("/app_data/images/photo.png") is True
    assert middleware._requires_auth("/app_data/fonts/embedded/font.ttf") is False
    assert (
        middleware._requires_auth("/app_data/pptx-to-html/session/fonts/font.ttf")
        is True
    )
    assert (
        middleware._requires_auth("/app_data/templates/default/thumbnail.png") is False
    )
    assert (
        middleware._requires_auth("/app_data/pptx-to-html/session/images/image.png")
        is True
    )


def test_other_app_data_prefixes_still_require_auth():
    middleware = SessionAuthMiddleware(app=None)

    assert middleware._requires_auth("/app_data/uploads/source.pptx") is True
    assert middleware._requires_auth("/app_data/exports/deck.pdf") is True


class _DummySession:
    def __init__(self, row=None):
        self._row = row

    async def get(self, _model, _id):
        return self._row


class _DummySessionMaker:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def test_auth_disabled_runtime_skips_cloud_proxy_and_serves_locally(monkeypatch):
    async def next_handler(_request):
        return Response("local-response")

    monkeypatch.setattr(middlewares, "is_disable_auth_enabled", lambda: True)
    monkeypatch.setattr(
        middlewares,
        "async_session_maker",
        lambda: _DummySessionMaker(_DummySession()),
    )

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/ppt/presentation/create",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 5001),
        }
    )
    response = asyncio.run(
        SessionAuthMiddleware(app=None).dispatch(request, next_handler)
    )

    assert response.body == b"local-response"


def test_auth_disabled_loads_singleton_into_overlay(monkeypatch):
    from types import SimpleNamespace

    from utils.get_env import get_openai_api_key_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setattr(middlewares, "is_disable_auth_enabled", lambda: True)
    monkeypatch.setattr(
        middlewares,
        "async_session_maker",
        lambda: _DummySessionMaker(
            _DummySession(SimpleNamespace(config={"OPENAI_API_KEY": "sk-local"}))
        ),
    )

    seen = {}

    async def next_handler(_request):
        seen["key"] = get_openai_api_key_env()
        return Response("ok")

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/ppt/presentation/create",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 5001),
        }
    )
    response = asyncio.run(
        SessionAuthMiddleware(app=None).dispatch(request, next_handler)
    )

    assert response.body == b"ok"
    assert seen["key"] == "sk-local"
    assert get_openai_api_key_env() == "sk-env"
