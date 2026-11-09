"""SQLAlchemy 异步 PostgreSQL Session。"""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings


class DatabaseManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        connect_args: dict[str, object] = {"timeout": settings.database_connect_timeout_seconds}
        if settings.database_ssl_mode == "require":
            connect_args["ssl"] = "require"
        elif settings.database_ssl_mode == "verify-full":
            if not settings.database_ssl_ca:
                raise ValueError("DATABASE_SSL_CA is required for verify-full")
            context = ssl.create_default_context(cafile=settings.database_ssl_ca)
            context.check_hostname = True
            connect_args["ssl"] = context
        self.engine: AsyncEngine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(self.engine, expire_on_commit=False)

    async def check(self) -> None:
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        session = self.session_factory()
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def dispose(self) -> None:
        await self.engine.dispose()


def build_database(settings: Settings | None = None) -> DatabaseManager:
    return DatabaseManager(settings or get_settings())


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.database.session() as session:
        yield session
