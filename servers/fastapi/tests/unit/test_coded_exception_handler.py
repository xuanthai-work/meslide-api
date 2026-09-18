"""Phase 5: ``CodedHTTPException`` response body shape.

- Coded errors serialize as ``{"detail", "error_code", "error_params"}``
  (params omitted when None; detail always present, verbatim).
- Plain ``HTTPException`` keeps FastAPI's default ``{"detail": ...}`` body.
"""
import asyncio

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from utils.coded_exception import CodedHTTPException, register_coded_exception_handler


def _build_app() -> FastAPI:
    app = FastAPI()
    register_coded_exception_handler(app)

    @app.get("/coded")
    async def coded():
        raise CodedHTTPException(
            400,
            "Number of slides cannot be greater than 20",
            error_code="error.slides_too_many",
            error_params={"max": 20},
        )

    @app.get("/coded-without-params")
    async def coded_without_params():
        raise CodedHTTPException(
            404,
            "Presentation not found",
            error_code="error.presentation_not_found",
        )

    @app.get("/plain")
    async def plain():
        raise HTTPException(status_code=418, detail="teapot")

    return app


def _get(path: str):
    async def runner():
        transport = ASGITransport(app=_build_app())
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path)

    return asyncio.run(runner())


def test_coded_exception_body_includes_code_and_params():
    response = _get("/coded")
    assert response.status_code == 400
    assert response.json() == {
        "detail": "Number of slides cannot be greater than 20",
        "error_code": "error.slides_too_many",
        "error_params": {"max": 20},
    }


def test_coded_exception_body_omits_params_when_none():
    response = _get("/coded-without-params")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Presentation not found",
        "error_code": "error.presentation_not_found",
    }


def test_plain_http_exception_body_is_fastapi_default():
    response = _get("/plain")
    assert response.status_code == 418
    assert response.json() == {"detail": "teapot"}