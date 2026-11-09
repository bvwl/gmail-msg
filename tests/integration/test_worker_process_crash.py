"""真实子进程 Worker 崩溃窗口验收。"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.db.base import (
    GmailMessage,
    GmailSyncState,
    Mailbox,
    NotificationDelivery,
    NotificationEndpoint,
    SyncRequest,
)
from app.redis_client import RedisManager
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = pytest.mark.integration
CHILD = Path(__file__).with_name("worker_crash_child.py")


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_DATABASE_URL")
        pytest.skip("未设置 TEST_DATABASE_URL，Worker 进程验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产数据库，Worker 进程验收只接受本机数据库")
    return value


def _redis_url() -> str:
    value = os.getenv("TEST_REDIS_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_REDIS_URL")
        pytest.skip("未设置 TEST_REDIS_URL，Worker 进程验收未执行")
    if not value.startswith("redis://"):
        pytest.fail("TEST_REDIS_URL 必须使用 redis://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产 Redis，Worker 进程验收只接受本机 Redis")
    return value


@pytest_asyncio.fixture
async def infra() -> tuple[str, str]:
    return _database_url(), _redis_url()


async def _run_child(
    mode: str, *, database_url: str, redis_url: str, values: dict[str, str]
) -> int:
    env = os.environ.copy()
    env.update(
        {
            "TEST_MODE": mode,
            "TEST_DATABASE_URL": database_url,
            "TEST_REDIS_URL": redis_url,
            "PYTHONPATH": str(CHILD.parents[2]),
            **values,
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(CHILD),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
        if process.returncode not in {0, -signal.SIGKILL}:
            raise AssertionError(
                f"子进程 PID {process.pid} 返回 {process.returncode}: "
                f"stdout={stdout.decode(errors='replace')} stderr={stderr.decode(errors='replace')}"
            )
        return int(process.returncode or 0)
    except TimeoutError:
        os.kill(process.pid, signal.SIGKILL)
        await process.wait()
        raise AssertionError(f"子进程 PID {process.pid} 超时") from None


async def _setup_sync(
    database_url: str, redis_url: str
) -> tuple[dict[str, str], async_sessionmaker[AsyncSession], AsyncEngine, RedisManager]:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    request_id = uuid4()
    message_id = f"crash-{uuid4().hex}"
    stream = f"test:crash:{uuid4().hex}"
    group = f"g-{uuid4().hex}"
    async with factory() as session:
        async with session.begin():
            mailbox = Mailbox(id=mailbox_id, email=f"crash-{mailbox_id.hex}@example.invalid")
            session.add(mailbox)
            await session.flush()
            session.add(
                GmailSyncState(mailbox_id=mailbox_id, mode="incremental", cursor_history_id="1")
            )
            session.add(
                SyncRequest(
                    id=request_id,
                    mailbox_id=mailbox_id,
                    observed_history_id="1",
                    reason="crash-test",
                    status="queued",
                )
            )
    redis = RedisManager.__new__(RedisManager)
    redis.settings = type("TestSettings", (), {"redis_url": redis_url})()
    from redis.asyncio import Redis

    redis.client = Redis.from_url(redis_url, decode_responses=True)
    stream_id = await redis.enqueue_sync(
        stream,
        mailbox_id=str(mailbox_id),
        observed_history_id=None,
        request_id=str(request_id),
        reason="crash-test",
    )
    values = {
        "TEST_STREAM": stream,
        "TEST_GROUP": group,
        "TEST_STREAM_ID": stream_id,
        "TEST_MAILBOX_ID": str(mailbox_id),
        "TEST_REQUEST_ID": str(request_id),
        "TEST_GMAIL_MESSAGE_ID": message_id,
    }
    return values, factory, engine, redis


@pytest.mark.asyncio
async def test_gmail_sync_worker_recovers_before_commit_crash(infra: tuple[str, str]) -> None:
    database_url, redis_url = infra
    values, factory, engine, redis = await _setup_sync(database_url, redis_url)
    try:
        assert (
            await _run_child(
                "before_commit", database_url=database_url, redis_url=redis_url, values=values
            )
        ) < 0
        assert (
            await _run_child(
                "resume", database_url=database_url, redis_url=redis_url, values=values
            )
        ) == 0
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(GmailMessage).where(
                        GmailMessage.gmail_message_id == values["TEST_GMAIL_MESSAGE_ID"]
                    )
                )
            ).all()
            assert len(rows) == 1
    finally:
        await redis.client.delete(values["TEST_STREAM"])
        await redis.close()
        await engine.dispose()


@pytest.mark.asyncio
async def test_gmail_sync_worker_replays_after_commit_before_ack(infra: tuple[str, str]) -> None:
    database_url, redis_url = infra
    values, factory, engine, redis = await _setup_sync(database_url, redis_url)
    try:
        assert (
            await _run_child(
                "after_commit", database_url=database_url, redis_url=redis_url, values=values
            )
        ) < 0
        assert (
            await _run_child(
                "resume", database_url=database_url, redis_url=redis_url, values=values
            )
        ) == 0
        async with factory() as session:
            rows = (
                await session.scalars(
                    select(GmailMessage).where(
                        GmailMessage.gmail_message_id == values["TEST_GMAIL_MESSAGE_ID"]
                    )
                )
            ).all()
            assert len(rows) == 1
        pending = await redis.pending(values["TEST_STREAM"], values["TEST_GROUP"])
        assert int(pending.get("pending", 0)) == 0
    finally:
        await redis.client.delete(values["TEST_STREAM"])
        await redis.close()
        await engine.dispose()


async def _setup_delivery(
    database_url: str,
) -> tuple[dict[str, str], async_sessionmaker[AsyncSession], AsyncEngine]:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    message_id = uuid4()
    endpoint_id = uuid4()
    delivery_id = uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(Mailbox(id=mailbox_id, email=f"notify-{mailbox_id.hex}@example.invalid"))
            await session.flush()
            session.add(
                GmailMessage(
                    id=message_id, mailbox_id=mailbox_id, gmail_message_id=f"m-{message_id.hex}"
                )
            )
            session.add(
                NotificationEndpoint(
                    id=endpoint_id,
                    name=f"endpoint-{endpoint_id.hex}",
                    url="http://127.0.0.1:1",
                    secret_ciphertext="TEST_SECRET",
                )
            )
            await session.flush()
            session.add(
                NotificationDelivery(
                    id=delivery_id,
                    message_id=message_id,
                    endpoint_id=endpoint_id,
                    idempotency_key=f"key-{delivery_id.hex}",
                    state="pending",
                    payload_json={"event_id": str(delivery_id)},
                )
            )
    return (
        {
            "TEST_DELIVERY_ID": str(delivery_id),
            "TEST_RECEIVED_KEY": f"test:received:{delivery_id.hex}",
        },
        factory,
        engine,
    )


@pytest.mark.asyncio
async def test_notification_worker_reclaims_after_http_success_crash(
    infra: tuple[str, str],
) -> None:
    database_url, redis_url = infra
    values, factory, engine = await _setup_delivery(database_url)
    redis = RedisManager.__new__(RedisManager)
    redis.settings = type("TestSettings", (), {"redis_url": redis_url})()
    from redis.asyncio import Redis

    redis.client = Redis.from_url(redis_url, decode_responses=True)
    try:
        assert (
            await _run_child(
                "after_http", database_url=database_url, redis_url=redis_url, values=values
            )
        ) < 0
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE notification_deliveries "
                    "SET updated_at = now() - interval '120 seconds' "
                    "WHERE id = :delivery_id"
                ),
                {"delivery_id": values["TEST_DELIVERY_ID"]},
            )
        assert (
            await _run_child(
                "resume_notification", database_url=database_url, redis_url=redis_url, values=values
            )
        ) == 0
        async with factory() as session:
            delivery = await session.get(NotificationDelivery, UUID(values["TEST_DELIVERY_ID"]))
            assert delivery is not None and delivery.state == "delivered", (
                delivery.state if delivery is not None else None,
                delivery.last_error_code if delivery is not None else None,
                delivery.last_http_status if delivery is not None else None,
                delivery.attempt_count if delivery is not None else None,
                delivery.next_attempt_at if delivery is not None else None,
            )
        assert await redis.client.get(values["TEST_RECEIVED_KEY"]) == values["TEST_DELIVERY_ID"]
    finally:
        await redis.client.delete(values["TEST_RECEIVED_KEY"])
        await redis.close()
        await engine.dispose()


async def _setup_scheduler(
    database_url: str, redis_url: str
) -> tuple[dict[str, str], async_sessionmaker[AsyncSession], AsyncEngine, RedisManager]:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    request_id = uuid4()
    stream = f"test:scheduler-crash:{uuid4().hex}"
    async with factory() as session:
        async with session.begin():
            await session.execute(text("UPDATE sync_requests SET status = 'enqueued'"))
            session.add(
                Mailbox(id=mailbox_id, email=f"scheduler-crash-{mailbox_id.hex}@example.invalid")
            )
            await session.flush()
            session.add(
                SyncRequest(
                    id=request_id,
                    mailbox_id=mailbox_id,
                    observed_history_id="99",
                    reason="scheduler-crash",
                    status="queued",
                )
            )
    redis = RedisManager.__new__(RedisManager)
    redis.settings = type("TestSettings", (), {"redis_url": redis_url})()
    from redis.asyncio import Redis

    redis.client = Redis.from_url(redis_url, decode_responses=True)
    return (
        {
            "TEST_STREAM": stream,
            "TEST_REQUEST_ID": str(request_id),
            "TEST_MAILBOX_ID": str(mailbox_id),
        },
        factory,
        engine,
        redis,
    )


@pytest.mark.asyncio
async def test_scheduler_restarts_after_lock_holder_crash(infra: tuple[str, str]) -> None:
    database_url, redis_url = infra
    values, factory, engine, redis = await _setup_scheduler(database_url, redis_url)
    try:
        assert (
            await _run_child(
                "scheduler_crash", database_url=database_url, redis_url=redis_url, values=values
            )
        ) < 0
        await asyncio.sleep(2.5)
        assert (
            await _run_child(
                "scheduler_resume", database_url=database_url, redis_url=redis_url, values=values
            )
        ) == 0
        async with factory() as session:
            request = await session.get(SyncRequest, UUID(values["TEST_REQUEST_ID"]))
            stream_length = int(await redis.client.xlen(values["TEST_STREAM"]))
            assert request is not None
            assert await redis.client.get(f"{values['TEST_STREAM']}:compensation-lock") is None
        assert stream_length == 2
    finally:
        await redis.client.delete(values["TEST_STREAM"])
        await redis.close()
        await engine.dispose()


async def _setup_message(
    database_url: str,
) -> tuple[dict[str, str], async_sessionmaker[AsyncSession], AsyncEngine]:
    engine = create_async_engine(database_url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    message_id = f"message-crash-{uuid4().hex}"
    async with factory() as session:
        async with session.begin():
            await session.execute(update(GmailMessage).values(state="delivered"))
            session.add(
                Mailbox(id=mailbox_id, email=f"message-crash-{mailbox_id.hex}@example.invalid")
            )
            await session.flush()
            session.add(
                GmailMessage(
                    mailbox_id=mailbox_id,
                    gmail_message_id=message_id,
                    state="discovered",
                )
            )
    return (
        {
            "TEST_MAILBOX_ID": str(mailbox_id),
            "TEST_GMAIL_MESSAGE_ID": message_id,
        },
        factory,
        engine,
    )


@pytest.mark.asyncio
async def test_message_worker_recovers_after_transaction_crash(infra: tuple[str, str]) -> None:
    database_url, redis_url = infra
    values, factory, engine = await _setup_message(database_url)
    try:
        assert (
            await _run_child(
                "message_crash", database_url=database_url, redis_url=redis_url, values=values
            )
        ) < 0
        assert (
            await _run_child(
                "message_resume", database_url=database_url, redis_url=redis_url, values=values
            )
        ) == 0
        async with factory() as session:
            message = await session.scalar(
                select(GmailMessage).where(
                    GmailMessage.mailbox_id == UUID(values["TEST_MAILBOX_ID"]),
                    GmailMessage.gmail_message_id == values["TEST_GMAIL_MESSAGE_ID"],
                )
            )
            assert message is not None and message.state == "parsed"
    finally:
        await engine.dispose()
