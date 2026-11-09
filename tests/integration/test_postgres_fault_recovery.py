"""隔离 PostgreSQL 断连、事务回滚和连接池恢复验收。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from app.db.base import (
    GmailMessage,
    GmailSyncState,
    Mailbox,
    NotificationDelivery,
    NotificationEndpoint,
)
from app.db.transaction import mailbox_transaction
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, InterfaceError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


def _test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_DATABASE_URL")
        pytest.skip("未设置 TEST_DATABASE_URL，真实 PostgreSQL 故障验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产数据库，故障验收只接受本机数据库")
    return value


async def _run_alembic(url: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(f"alembic upgrade head 失败:\n{result.stdout}\n{result.stderr}")


@pytest_asyncio.fixture
async def database_url() -> str:
    url = _test_database_url()
    await _run_alembic(url)
    return url


async def _terminate_backend(url: str, pid: int) -> None:
    killer_engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        async with killer_engine.begin() as connection:
            await connection.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
    finally:
        await killer_engine.dispose()


async def _backend_pid(session: AsyncSession) -> int:
    value = await session.scalar(text("SELECT pg_backend_pid()"))
    assert value is not None
    return int(value)


@pytest.mark.asyncio
async def test_transaction_disconnect_rolls_back_and_pool_recovers(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    failed_id = uuid4()
    recovered_id = uuid4()

    session = factory()
    try:
        await session.begin()
        session.add(Mailbox(id=failed_id, email=f"failed-{failed_id.hex}@example.invalid"))
        await session.flush()
        pid = await _backend_pid(session)
        await _terminate_backend(database_url, pid)
        with pytest.raises((DBAPIError, InterfaceError, ConnectionError)):
            await session.commit()
    finally:
        await session.close()

    async with factory() as recovered:
        async with recovered.begin():
            recovered.add(
                Mailbox(id=recovered_id, email=f"recovered-{recovered_id.hex}@example.invalid")
            )

    async with factory() as verify:
        assert await verify.get(Mailbox, failed_id) is None
        assert await verify.get(Mailbox, recovered_id) is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_cursor_is_not_advanced_when_commit_connection_is_lost(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    async with factory() as setup:
        async with setup.begin():
            setup.add(Mailbox(id=mailbox_id, email=f"cursor-{mailbox_id.hex}@example.invalid"))
            await setup.flush()
            setup.add(
                GmailSyncState(mailbox_id=mailbox_id, cursor_history_id="100", mode="incremental")
            )

    session = factory()
    try:
        await session.begin()
        state = await session.get(GmailSyncState, mailbox_id, with_for_update=True)
        assert state is not None
        state.cursor_history_id = "200"
        await session.flush()
        pid = await _backend_pid(session)
        await _terminate_backend(database_url, pid)
        with pytest.raises((DBAPIError, InterfaceError, ConnectionError)):
            await session.commit()
    finally:
        await session.close()

    async with factory() as verify:
        state = await verify.get(GmailSyncState, mailbox_id)
        assert state is not None and state.cursor_history_id == "100"
    await engine.dispose()


@pytest.mark.asyncio
async def test_advisory_lock_connection_loss_does_not_stick_lock(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    first = factory()
    try:
        await first.begin()
        from app.db.transaction import acquire_mailbox_advisory_lock

        await acquire_mailbox_advisory_lock(first, mailbox_id)
        pid = await _backend_pid(first)
        await _terminate_backend(database_url, pid)
        with pytest.raises((DBAPIError, InterfaceError, ConnectionError)):
            await first.commit()
    finally:
        await first.close()

    async with mailbox_transaction(factory, mailbox_id):
        pass
    await engine.dispose()


@pytest.mark.asyncio
async def test_outbox_state_update_connection_loss_is_retryable(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox = Mailbox(id=uuid4(), email=f"outbox-{uuid4().hex}@example.invalid")
    message = GmailMessage(id=uuid4(), mailbox_id=mailbox.id, gmail_message_id=f"m-{uuid4().hex}")
    endpoint = NotificationEndpoint(
        id=uuid4(),
        name=f"endpoint-{uuid4().hex}",
        url="http://127.0.0.1:1",
        secret_ciphertext="TEST_SECRET",
    )
    delivery = NotificationDelivery(
        id=uuid4(),
        message_id=message.id,
        endpoint_id=endpoint.id,
        idempotency_key=f"key-{uuid4().hex}",
        state="pending",
    )
    async with factory() as setup:
        async with setup.begin():
            setup.add_all([mailbox, endpoint])
            await setup.flush()
            setup.add_all([message, delivery])

    session = factory()
    try:
        await session.begin()
        row = await session.get(NotificationDelivery, delivery.id, with_for_update=True)
        assert row is not None
        row.state = "delivered"
        await session.flush()
        pid = await _backend_pid(session)
        await _terminate_backend(database_url, pid)
        with pytest.raises((DBAPIError, InterfaceError, ConnectionError)):
            await session.commit()
    finally:
        await session.close()

    async with factory() as verify:
        row = await verify.get(NotificationDelivery, delivery.id)
        assert row is not None and row.state == "pending"
        assert (
            await verify.scalars(select(GmailMessage).where(GmailMessage.id == message.id))
        ).one()
    await engine.dispose()


__all__ = ["test_transaction_disconnect_rolls_back_and_pool_recovers"]
