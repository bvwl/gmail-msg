from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from app.config import Settings
from app.db.session import DatabaseManager
from app.redis_client import RedisManager
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine


@pytest.mark.asyncio
async def test_database_check_and_dispose_use_engine() -> None:
    connection = SimpleNamespace(execute=AsyncMock())
    engine = SimpleNamespace(
        connect=AsyncMock(),
        dispose=AsyncMock(),
    )

    class ConnectionContext:
        async def __aenter__(self) -> SimpleNamespace:
            return connection

        async def __aexit__(self, *_args: object) -> bool:
            return False

    engine.connect = lambda: ConnectionContext()
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.engine = cast(AsyncEngine, engine)

    await manager.check()
    await manager.dispose()

    connection.execute.assert_awaited_once()
    engine.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_redis_check_and_close_use_async_client() -> None:
    client = SimpleNamespace(ping=AsyncMock(), aclose=AsyncMock())
    manager = RedisManager.__new__(RedisManager)
    manager.client = cast(Redis, client)

    await manager.check()
    await manager.close()

    client.ping.assert_awaited_once()
    client.aclose.assert_awaited_once()


def test_managers_build_from_settings() -> None:
    settings = Settings(_env_file=None)
    database = DatabaseManager(settings)
    redis = RedisManager(settings)

    assert database.engine.url.drivername == "postgresql+asyncpg"
    assert redis.client.connection_pool.connection_kwargs["host"] == "localhost"
