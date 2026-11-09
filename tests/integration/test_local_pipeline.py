from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from app.config import Settings
from app.db.base import GmailMessage, Mailbox, NotificationDelivery, NotificationEndpoint
from app.notifications.delivery import NotificationDeliveryService, NotificationOutboxService
from app.redis_client import RedisManager
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


def _url(name: str, prefix: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail(f"REQUIRE_REAL_INFRA=1 但未设置 {name}")
        pytest.skip(f"未设置 {name}，本地全链路验收未执行")
    if not value.startswith(prefix):
        pytest.fail(f"{name} 格式错误")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip(f"为避免误连生产服务，默认只接受本机 {name}")
    return value


@pytest.mark.asyncio
async def test_pubsub_like_stream_to_postgres_outbox_and_http() -> None:
    database_url = _url("TEST_DATABASE_URL", "postgresql+asyncpg://")
    redis_url = _url("TEST_REDIS_URL", "redis://")
    engine = create_async_engine(database_url, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = RedisManager.__new__(RedisManager)
    redis.settings = Settings(_env_file=None, redis_url=redis_url)
    from redis.asyncio import Redis

    redis.client = Redis.from_url(redis_url, decode_responses=True)
    stream = f"test:full:{uuid4().hex}"
    group = f"g-{uuid4().hex}"
    mailbox = Mailbox(id=uuid4(), email=f"pipeline-{uuid4().hex}@example.invalid")
    endpoint = NotificationEndpoint(
        id=uuid4(),
        name=f"pipeline-{uuid4().hex}",
        url="http://127.0.0.1:1",
        secret_ciphertext="PIPELINE_SECRET",
    )
    message = GmailMessage(
        id=uuid4(),
        mailbox_id=mailbox.id,
        gmail_message_id=f"pipeline-{uuid4().hex}",
        subject="pipeline",
        received_at=datetime.now(UTC),
        body_text="hello",
    )
    received: list[bytes] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        raw = await reader.readuntil(b"\r\n\r\n")
        headers = {
            line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip()
            for line in raw.decode("latin-1").split("\r\n")
            if ":" in line
        }
        received.append(await reader.readexactly(int(headers.get("content-length", "0"))))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    endpoint.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/notify"
    try:
        async with factory() as session:
            async with session.begin():
                session.add_all([mailbox, endpoint])
        await redis.xadd(
            stream, {"mailbox_id": str(mailbox.id), "gmail_message_id": message.gmail_message_id}
        )
        items = await redis.read_group(stream, group, "pipeline-worker", block_ms=100, count=1)
        assert len(items) == 1
        # 模拟 Gmail Sync/Message Worker：DB 事务提交后才 ACK Redis。
        async with factory() as session:
            async with session.begin():
                session.add(message)
                loaded_endpoint = await session.get(NotificationEndpoint, endpoint.id)
                loaded_mailbox = await session.get(Mailbox, mailbox.id)
                assert loaded_endpoint and loaded_mailbox
                delivery = await NotificationOutboxService(factory).create_for_message(
                    session, message, loaded_mailbox, loaded_endpoint
                )
                delivery_id = delivery.id
        assert await redis.ack(stream, group, items[0].id) == 1
        result = await NotificationDeliveryService(
            factory, Settings(_env_file=None, webhook_secret="PIPELINE_SECRET")
        ).deliver(delivery_id)
        assert result.accepted
        assert received
        async with factory() as session:
            stored = await session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
            )
            assert stored is not None and stored.state == "delivered"
    finally:
        server.close()
        await server.wait_closed()
        try:
            await redis.client.delete(stream)
        except Exception:
            pass
        await redis.close()
        await engine.dispose()
