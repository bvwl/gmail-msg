from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from urllib.parse import urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from app.db.base import Base, GmailMessage, Mailbox, NotificationDelivery, NotificationEndpoint
from app.notifications.delivery import NotificationOutboxService
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


def _test_database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_DATABASE_URL")
        pytest.skip("未设置 TEST_DATABASE_URL，真实 PostgreSQL 验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    host = urlparse(value).hostname
    if (
        host not in {"localhost", "127.0.0.1", "::1"}
        and os.getenv("ALLOW_REMOTE_TEST_INFRA") != "1"
    ):
        pytest.skip("为避免误连生产数据库，默认只接受本机 TEST_DATABASE_URL")
    return value


async def _run_alembic(url: str, *args: str) -> None:
    env = os.environ.copy()
    env["DATABASE_URL"] = url
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.fail(f"alembic {' '.join(args)} 失败:\n{result.stdout}\n{result.stderr}")


@pytest_asyncio.fixture
async def database_url() -> str:
    url = _test_database_url()
    await _run_alembic(url, "upgrade", "head")
    return url


@pytest.mark.asyncio
async def test_alembic_upgrade_creates_model_schema_and_is_idempotent(database_url: str) -> None:
    await _run_alembic(database_url, "upgrade", "head")
    engine = create_async_engine(database_url)
    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda conn: set(inspect(conn).get_table_names()))
        assert set(Base.metadata.tables).issubset(table_names)
        fk_count = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE constraint_type='FOREIGN KEY'"
            )
        )
        assert int(fk_count or 0) >= 8
        unique_count = await connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.table_constraints "
                "WHERE constraint_type='UNIQUE'"
            )
        )
        assert int(unique_count or 0) >= 6
    await engine.dispose()


@pytest.mark.asyncio
async def test_alembic_downgrade_to_0005_and_upgrade_is_safe(database_url: str) -> None:
    await _run_alembic(database_url, "downgrade", "0005_stage5_email_content_outbox")
    await _run_alembic(database_url, "upgrade", "head")


@pytest.mark.asyncio
async def test_unique_message_and_outbox_are_transactional(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox = Mailbox(id=uuid4(), email=f"test-{uuid4().hex}@example.invalid")
    endpoint = NotificationEndpoint(
        id=uuid4(),
        name=f"endpoint-{uuid4().hex}",
        url="http://127.0.0.1:1",
        secret_ciphertext="TEST_SECRET",
    )
    message = GmailMessage(id=uuid4(), mailbox_id=mailbox.id, gmail_message_id=f"m-{uuid4().hex}")
    async with factory() as session:
        async with session.begin():
            session.add_all([mailbox, endpoint])
    async with factory() as session:
        async with session.begin():
            session.add(message)
    service = NotificationOutboxService(factory)
    async with factory() as session:
        async with session.begin():
            loaded_message = await session.get(GmailMessage, message.id, with_for_update=True)
            loaded_mailbox = await session.get(Mailbox, mailbox.id)
            loaded_endpoint = await session.get(NotificationEndpoint, endpoint.id)
            assert loaded_message and loaded_mailbox and loaded_endpoint
            first = await service.create_for_message(
                session, loaded_message, loaded_mailbox, loaded_endpoint
            )
            second = await service.create_for_message(
                session, loaded_message, loaded_mailbox, loaded_endpoint
            )
            assert first.id == second.id
    async with factory() as session:
        rows = list(
            (
                await session.scalars(
                    select(NotificationDelivery).where(
                        NotificationDelivery.message_id == message.id
                    )
                )
            ).all()
        )
        assert len(rows) == 1
        assert rows[0].payload_json is not None
    async with factory() as session:
        async with session.begin():
            duplicate = GmailMessage(
                id=uuid4(), mailbox_id=mailbox.id, gmail_message_id=message.gmail_message_id
            )
            async with session.begin_nested():
                session.add(duplicate)
                with pytest.raises(IntegrityError):
                    await session.flush()
    await engine.dispose()


@pytest.mark.asyncio
async def test_row_lock_serializes_workers_and_rollback_preserves_state(database_url: str) -> None:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox = Mailbox(id=uuid4(), email=f"lock-{uuid4().hex}@example.invalid")
    async with factory() as session:
        async with session.begin():
            session.add(mailbox)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def first() -> None:
        async with factory() as session:
            async with session.begin():
                row = await session.get(Mailbox, mailbox.id, with_for_update=True)
                assert row is not None
                row.status = "authorized"
                entered.set()
                await release.wait()

    async def second() -> float:
        await entered.wait()
        started = time.monotonic()
        async with factory() as session:
            async with session.begin():
                row = await session.get(Mailbox, mailbox.id, with_for_update=True)
                assert row is not None
                row.status = "watch_active"
        return time.monotonic() - started

    first_task = asyncio.create_task(first())
    await entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert not second_task.done()
    release.set()
    elapsed = await second_task
    await first_task
    assert elapsed >= 0.04
    async with factory() as session:
        row = await session.get(Mailbox, mailbox.id)
        assert row is not None and row.status == "watch_active"
    await engine.dispose()
