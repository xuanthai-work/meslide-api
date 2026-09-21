"""Health probes: liveness stays cheap; readiness checks DB, migrations, storage."""

import asyncio
import os
import tempfile

os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.health import HEALTH_ROUTER, get_live


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(HEALTH_ROUTER)
    return TestClient(app)


def test_live_returns_ok_without_touching_dependencies(monkeypatch):
    async def fail_database():
        raise AssertionError("liveness must not query the database")

    monkeypatch.setattr("api.health.check_database", fail_database)
    monkeypatch.setattr("api.health.check_migration", fail_database)
    monkeypatch.setattr("api.health.check_storage", fail_database)

    assert asyncio.run(get_live()) == {"status": "ok"}
    response = _client().get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_ok_when_database_migration_and_storage_pass(monkeypatch):
    async def ok(_name="ok"):
        return "ok"

    monkeypatch.setattr("api.health.check_database", lambda: ok())
    monkeypatch.setattr("api.health.check_migration", lambda: ok())
    monkeypatch.setattr("api.health.check_storage", lambda: ok())

    response = _client().get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"database": "ok", "migration": "ok", "storage": "ok"},
    }


def test_ready_returns_503_when_database_is_down(monkeypatch):
    async def unavailable():
        return "unavailable"

    async def ok():
        return "ok"

    monkeypatch.setattr("api.health.check_database", unavailable)
    monkeypatch.setattr("api.health.check_migration", ok)
    monkeypatch.setattr("api.health.check_storage", ok)

    response = _client().get("/health/ready")
    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["database"] == "unavailable"
    assert payload["checks"]["migration"] == "ok"
    assert payload["checks"]["storage"] == "ok"


def test_ready_returns_503_when_migrations_are_pending(monkeypatch):
    async def pending():
        return "pending"

    async def ok():
        return "ok"

    monkeypatch.setattr("api.health.check_database", ok)
    monkeypatch.setattr("api.health.check_migration", pending)
    monkeypatch.setattr("api.health.check_storage", ok)

    response = _client().get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["migration"] == "pending"


def test_ready_returns_503_when_storage_is_unavailable(monkeypatch):
    async def unavailable():
        return "unavailable"

    async def ok():
        return "ok"

    monkeypatch.setattr("api.health.check_database", ok)
    monkeypatch.setattr("api.health.check_migration", ok)
    monkeypatch.setattr("api.health.check_storage", unavailable)

    response = _client().get("/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["storage"] == "unavailable"
