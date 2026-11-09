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
from app.notifications.signing import verify_signature
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration


def _database_url() -> str:
    value = os.getenv("TEST_DATABASE_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_DATABASE_URL")
        pytest.skip("未设置 TEST_DATABASE_URL，真实 Outbox HTTP 验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产数据库，默认只接受本机 TEST_DATABASE_URL")
    return value


async def _serve_http(
    statuses: list[int],
    requests: list[dict[str, object]],
) -> tuple[asyncio.AbstractServer, str]:
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_headers = await reader.readuntil(b"\r\n\r\n")
            header_lines = raw_headers.decode("latin-1").split("\r\n")
            headers: dict[str, str] = {}
            for line in header_lines[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.lower()] = value.strip()
            length = int(headers.get("content-length", "0"))
            body = await reader.readexactly(length)
            requests.append({"headers": headers, "body": body})
            status = statuses.pop(0) if statuses else 200
            phrase = {200: "OK", 201: "Created", 429: "Too Many Requests", 500: "Server Error"}.get(
                status, "Error"
            )
            response = (
                f"HTTP/1.1 {status} {phrase}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            writer.write(response.encode("ascii"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, f"http://127.0.0.1:{port}/notify"


@pytest.mark.asyncio
async def test_outbox_http_success_retry_hmac_and_idempotency() -> None:
    engine = create_async_engine(_database_url(), pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    received: list[dict[str, object]] = []
    server, url = await _serve_http([429, 500, 200], received)
    secret = "TEST_WEBHOOK_SECRET"
    try:
        mailbox = Mailbox(id=uuid4(), email=f"http-{uuid4().hex}@example.invalid")
        endpoint = NotificationEndpoint(
            id=uuid4(),
            name=f"http-{uuid4().hex}",
            url=url,
            secret_ciphertext=secret,
            max_attempts=4,
        )
        message = GmailMessage(
            id=uuid4(),
            mailbox_id=mailbox.id,
            gmail_message_id=f"http-{uuid4().hex}",
            subject="integration",
            received_at=datetime.now(UTC),
            body_text="body",
        )
        async with factory() as session:
            async with session.begin():
                session.add_all([mailbox, endpoint])
        async with factory() as session:
            async with session.begin():
                session.add(message)
        async with factory() as session:
            async with session.begin():
                loaded_message = await session.get(GmailMessage, message.id)
                loaded_mailbox = await session.get(Mailbox, mailbox.id)
                loaded_endpoint = await session.get(NotificationEndpoint, endpoint.id)
                assert loaded_message and loaded_mailbox and loaded_endpoint
                delivery = await NotificationOutboxService(factory).create_for_message(
                    session, loaded_message, loaded_mailbox, loaded_endpoint
                )
                delivery_id = delivery.id
        settings = Settings(_env_file=None, webhook_retry_base_delay=0.01, webhook_max_retries=3)
        result = await NotificationDeliveryService(factory, settings).deliver(delivery_id)
        assert result.accepted and result.status_code == 200
        assert len(received) == 3
        first = received[0]
        headers = first["headers"]
        body = first["body"]
        assert isinstance(headers, dict) and isinstance(body, bytes)
        assert verify_signature(
            secret,
            headers["x-webhook-timestamp"],
            body,
            headers["x-webhook-signature"],
            tolerance_seconds=300,
        )
        assert headers["idempotency-key"] == headers["x-request-id"] or headers["idempotency-key"]
        second_result = await NotificationDeliveryService(factory, settings).deliver(delivery_id)
        assert second_result.accepted and len(received) == 3
        async with factory() as session:
            row = await session.scalar(
                select(NotificationDelivery).where(NotificationDelivery.id == delivery_id)
            )
            assert row is not None and row.state == "delivered" and row.attempt_count == 1
    finally:
        server.close()
        await server.wait_closed()
        await engine.dispose()
