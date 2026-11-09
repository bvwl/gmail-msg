"""Outbox 过期领取者 fencing 验收。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from app.config import Settings
from app.db.base import GmailMessage, Mailbox, NotificationDelivery, NotificationEndpoint
from app.notifications.delivery import DeliveryResult, NotificationDeliveryService, WebhookSender
from app.workers.notification_worker import NotificationWorker
from sqlalchemy import text
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
        pytest.skip("未设置 TEST_DATABASE_URL，Outbox fencing 验收未执行")
    if not value.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL 必须使用 postgresql+asyncpg://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产数据库，Outbox fencing 只接受本机数据库")
    return value


class BlockingSender(WebhookSender):
    def __init__(self, started: asyncio.Event, release: asyncio.Event, settings: Settings) -> None:
        super().__init__(settings)
        self.started = started
        self.release = release

    async def send(
        self,
        _payload: Mapping[str, Any],
        *,
        url: str,
        secret: str,
        event_id: str,
        idempotency_key: str,
        event_type: str = "gmail.message.received",
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> DeliveryResult:
        _ = (url, secret, event_id, idempotency_key, event_type, timeout, max_retries)
        self.started.set()
        await self.release.wait()
        return DeliveryResult(False, True, 503, "late_worker")


class AcceptSender(WebhookSender):
    async def send(
        self,
        _payload: Mapping[str, Any],
        *,
        url: str,
        secret: str,
        event_id: str,
        idempotency_key: str,
        event_type: str = "gmail.message.received",
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> DeliveryResult:
        _ = (url, secret, event_id, idempotency_key, event_type, timeout, max_retries)
        return DeliveryResult(True, False, 200)


@pytest_asyncio.fixture
async def outbox_context() -> tuple[async_sessionmaker[AsyncSession], str, AsyncEngine]:
    url = _database_url()
    engine = create_async_engine(url, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    mailbox_id = uuid4()
    message_id = uuid4()
    endpoint_id = uuid4()
    delivery_id = uuid4()
    async with factory() as session:
        async with session.begin():
            session.add(Mailbox(id=mailbox_id, email=f"fence-{mailbox_id.hex}@example.invalid"))
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
                    idempotency_key=f"fence-{delivery_id.hex}",
                    state="pending",
                    payload_json={"event_id": str(delivery_id)},
                )
            )
    return factory, str(delivery_id), engine


@pytest.mark.asyncio
async def test_late_worker_cannot_overwrite_new_delivery(
    outbox_context: tuple[async_sessionmaker[AsyncSession], str, AsyncEngine],
) -> None:
    factory, delivery_id, engine = outbox_context
    settings = Settings(_env_file=None, app_env="test", webhook_max_retries=0)
    started = asyncio.Event()
    release = asyncio.Event()
    first = NotificationDeliveryService(
        factory, settings, sender=BlockingSender(started, release, settings)
    )
    second = NotificationDeliveryService(factory, settings, sender=AcceptSender())
    worker = NotificationWorker(second, factory, settings)
    delivery_uuid = UUID(delivery_id)
    first_task = asyncio.create_task(first.deliver(delivery_uuid))
    try:
        await started.wait()
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE notification_deliveries "
                    "SET updated_at = now() - interval '120 seconds' "
                    "WHERE id = :delivery_id"
                ),
                {"delivery_id": delivery_id},
            )
        await worker.recover_stale_sending()
        await second.deliver(delivery_uuid)
        release.set()
        await first_task
        async with factory() as session:
            delivery = await session.get(NotificationDelivery, delivery_uuid)
            assert delivery is not None and delivery.state == "delivered"
    finally:
        release.set()
        if not first_task.done():
            await first_task
        await engine.dispose()
