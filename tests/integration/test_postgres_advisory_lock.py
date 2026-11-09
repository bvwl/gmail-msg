from __future__ import annotations

import asyncio
import os
import time
from uuid import UUID, uuid4

import pytest
from app.db.transaction import mailbox_transaction
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


def _test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_DATABASE_URL")
        pytest.skip("未设置 TEST_DATABASE_URL，真实 PostgreSQL 并发验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    return value


@pytest.mark.asyncio
async def test_same_mailbox_advisory_lock_serializes_workers() -> None:
    engine = create_async_engine(_test_database_url(), pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first_worker() -> None:
        async with mailbox_transaction(factory, mailbox_id):
            first_entered.set()
            await release_first.wait()

    async def second_worker() -> float:
        await first_entered.wait()
        started = time.monotonic()
        async with mailbox_transaction(factory, mailbox_id):
            second_entered.set()
        return time.monotonic() - started

    first_task = asyncio.create_task(first_worker())
    await first_entered.wait()
    second_task = asyncio.create_task(second_worker())
    await asyncio.sleep(0.1)
    assert not second_entered.is_set()
    release_first.set()
    elapsed = await second_task
    await first_task

    assert second_entered.is_set()
    assert elapsed >= 0.08
    await engine.dispose()


@pytest.mark.asyncio
async def test_different_mailboxes_can_progress_in_parallel() -> None:
    engine = create_async_engine(_test_database_url(), pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entered = asyncio.Event()
    mailbox_ids = (uuid4(), uuid4())

    async def worker(mailbox_id: UUID) -> None:
        async with mailbox_transaction(factory, mailbox_id):
            entered.set()
            await asyncio.sleep(0.05)

    await asyncio.gather(*(worker(mailbox_id) for mailbox_id in mailbox_ids))
    assert entered.is_set()
    await engine.dispose()


@pytest.mark.asyncio
async def test_exception_rolls_back_and_releases_advisory_lock() -> None:
    engine = create_async_engine(_test_database_url(), pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()

    with pytest.raises(RuntimeError, match="rollback probe"):
        async with mailbox_transaction(factory, mailbox_id):
            raise RuntimeError("rollback probe")

    # 第二次获取成功证明异常回滚后 PostgreSQL 已释放事务级 advisory lock。
    async with mailbox_transaction(factory, mailbox_id):
        pass
    await engine.dispose()
