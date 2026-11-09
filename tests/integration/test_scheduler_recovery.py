"""Scheduler 入队状态与 Redis 锁恢复验收。"""

from __future__ import annotations

import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from app.config import Settings
from app.db.base import Mailbox, SyncRequest
from app.redis_client import RedisManager
from app.workers.scheduler import GmailCompensationScheduler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_DATABASE_URL")
        pytest.skip("未设置 TEST_DATABASE_URL，Scheduler 真实验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产数据库，Scheduler 验收只接受本机数据库")
    return value


def _redis_url() -> str:
    value = os.getenv("TEST_REDIS_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_REDIS_URL")
        pytest.skip("未设置 TEST_REDIS_URL，Scheduler 真实验收未执行")
    if not value.startswith("redis://"):
        pytest.fail("TEST_REDIS_URL 必须使用 redis://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产 Redis，Scheduler 验收只接受本机 Redis")
    return value


@pytest_asyncio.fixture
async def scheduler_context() -> tuple[
    async_sessionmaker[AsyncSession], RedisManager, str, AsyncEngine
]:
    database_url = _database_url()
    redis_url = _redis_url()
    stream = f"test:scheduler:{uuid4().hex}"
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        redis_url=redis_url,
        redis_stream_name=stream,
        gmail_sync_compensation_interval_seconds=300,
    )
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = RedisManager(settings)
    return factory, redis, stream, engine


@pytest.mark.asyncio
async def test_scheduler_marks_enqueued_after_redis_write(
    scheduler_context: tuple[async_sessionmaker[AsyncSession], RedisManager, str, AsyncEngine],
) -> None:
    factory, redis, stream, engine = scheduler_context
    mailbox_id = uuid4()
    request_id = uuid4()
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(update(SyncRequest).values(status="enqueued"))
                session.add(
                    Mailbox(id=mailbox_id, email=f"scheduler-{mailbox_id.hex}@example.invalid")
                )
                await session.flush()
                session.add(
                    SyncRequest(
                        id=request_id,
                        mailbox_id=mailbox_id,
                        observed_history_id="12",
                        reason="compensation",
                        status="queued",
                    )
                )
        scheduler = GmailCompensationScheduler(factory, redis, redis.settings)
        assert await scheduler.run_once(limit=10) == 1
        async with factory() as session:
            request = await session.scalar(select(SyncRequest).where(SyncRequest.id == request_id))
            assert request is not None and request.status == "enqueued"
        assert await scheduler.run_once(limit=10) == 0
        assert int(await redis.client.xlen(stream)) == 1
    finally:
        await redis.client.delete(stream)
        await redis.close()
        await engine.dispose()
