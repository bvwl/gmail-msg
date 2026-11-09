from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient


class FakeDependency:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.check = AsyncMock(side_effect=None if available else RuntimeError("connection failed"))
        self.dispose = AsyncMock()
        self.close = AsyncMock()


def test_health_ready_returns_200_when_dependencies_are_healthy() -> None:
    database = FakeDependency()
    redis = FakeDependency()
    app = create_app(Settings(_env_file=None), database=database, redis=redis)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"config": "ok", "postgres": "ok", "redis": "ok"},
    }
    database.dispose.assert_awaited_once()
    redis.close.assert_awaited_once()


def test_health_ready_returns_503_without_leaking_exception_details() -> None:
    database = FakeDependency(available=False)
    redis = FakeDependency(available=True)
    app = create_app(Settings(_env_file=None), database=database, redis=redis)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"] == {
        "config": "ok",
        "postgres": "unavailable",
        "redis": "ok",
    }
    assert "connection failed" not in response.text


def test_health_ready_applies_timeout() -> None:
    database = FakeDependency()

    async def slow_check() -> None:
        await asyncio.sleep(0.05)

    database.check = AsyncMock(side_effect=slow_check)
    redis = FakeDependency()
    settings = Settings(_env_file=None, health_check_timeout_seconds=0.001)
    app = create_app(settings, database=database, redis=redis)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["postgres"] == "timeout"
