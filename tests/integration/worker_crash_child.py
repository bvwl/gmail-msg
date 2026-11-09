"""由进程级故障测试启动的隔离 Worker 子进程。"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Mapping
from typing import Any, cast
from uuid import UUID

from app.config import Settings
from app.db.base import GmailMessage
from app.db.session import DatabaseManager
from app.gmail.sync import GmailSyncService
from app.notifications.delivery import DeliveryResult, NotificationDeliveryService, WebhookSender
from app.redis_client import RedisManager, StreamMessage
from app.workers.gmail_sync_worker import GmailSyncWorker
from app.workers.message_worker import MessageWorker
from app.workers.notification_worker import NotificationWorker
from app.workers.scheduler import GmailCompensationScheduler
from sqlalchemy import select


def _kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


class CrashSyncService:
    def __init__(
        self, database: DatabaseManager, mailbox_id: UUID, message_id: str, phase: str
    ) -> None:
        self.database = database
        self.mailbox_id = mailbox_id
        self.message_id = message_id
        self.phase = phase

    async def sync_mailbox(self, _mailbox_id: UUID, **_kwargs: Any) -> None:
        async with self.database.session_factory() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(GmailMessage).where(
                        GmailMessage.mailbox_id == self.mailbox_id,
                        GmailMessage.gmail_message_id == self.message_id,
                    )
                )
                if existing is None:
                    session.add(
                        GmailMessage(
                            mailbox_id=self.mailbox_id,
                            gmail_message_id=self.message_id,
                        )
                    )
                await session.flush()
                if self.phase == "before_commit":
                    _kill_self()


class CrashSyncWorker(GmailSyncWorker):
    def __init__(self, *args: Any, crash_after_completed: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.crash_after_completed = crash_after_completed

    async def _mark_completed(self, message: StreamMessage) -> None:
        await super()._mark_completed(message)
        if self.crash_after_completed:
            _kill_self()


class CrashScheduler(GmailCompensationScheduler):
    async def _mark_enqueued(self, _request_id: UUID) -> None:
        _kill_self()


class CrashMessageService:
    def __init__(self, database: DatabaseManager, phase: str) -> None:
        self.database = database
        self.phase = phase

    async def process_message_id(self, mailbox_id: UUID, message_id: str) -> None:
        async with self.database.session_factory() as session:
            async with session.begin():
                message = await session.scalar(
                    select(GmailMessage).where(
                        GmailMessage.mailbox_id == mailbox_id,
                        GmailMessage.gmail_message_id == message_id,
                    )
                )
                if message is None:
                    return
                message.state = "parsed"
                await session.flush()
                if self.phase == "before_commit":
                    _kill_self()


class CrashSender(WebhookSender):
    def __init__(self, settings: Settings, *, phase: str, received_key: str) -> None:
        super().__init__(settings)
        self.phase = phase
        self.received_key = received_key

    async def send(
        self,
        payload: Mapping[str, Any],
        *,
        url: str,
        secret: str,
        event_id: str,
        idempotency_key: str,
        event_type: str = "gmail.message.received",
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> DeliveryResult:
        _ = (payload, url, secret, idempotency_key, event_type, timeout, max_retries)
        event_id = str(event_id)
        if self.phase == "after_http":
            redis_url = os.environ["TEST_REDIS_URL"]
            redis = RedisManager(Settings(_env_file=None, app_env="test", redis_url=redis_url))
            try:
                await redis.client.set(self.received_key, event_id)
            finally:
                await redis.close()
            _kill_self()
        return DeliveryResult(True, False, 200)


async def _run_sync(mode: str) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    redis_url = os.environ["TEST_REDIS_URL"]
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        redis_url=redis_url,
        redis_stream_name=os.environ["TEST_STREAM"],
        redis_stream_group=os.environ["TEST_GROUP"],
        redis_stream_block_ms=100,
        gmail_sync_worker_concurrency=1,
    )
    database = DatabaseManager(settings)
    redis = RedisManager(settings)
    mailbox_id = UUID(os.environ["TEST_MAILBOX_ID"])
    await redis.ensure_consumer_group(settings.redis_stream_name, settings.redis_stream_group)
    if mode == "resume":
        message = None
    else:
        messages = await redis.read_group(
            settings.redis_stream_name,
            settings.redis_stream_group,
            "crash-child",
            count=1,
            block_ms=100,
        )
        if not messages:
            raise RuntimeError("crash test stream message was not available")
        message = messages[0]
    service = CrashSyncService(database, mailbox_id, os.environ["TEST_GMAIL_MESSAGE_ID"], mode)
    worker = CrashSyncWorker(
        cast(GmailSyncService, service),
        redis,
        database.session_factory,
        settings,
        consumer_name="crash-child",
        crash_after_completed=mode == "after_commit",
    )
    try:
        if mode == "resume":
            await worker.reclaim(min_idle_ms=0)
        else:
            assert message is not None
            await worker.process_message(message)
    finally:
        await database.dispose()
        await redis.close()


async def _run_notification(mode: str) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    redis_url = os.environ["TEST_REDIS_URL"]
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        redis_url=redis_url,
        webhook_enabled=True,
        webhook_secret="TEST_SECRET",
        webhook_timeout=0.1,
        webhook_max_retries=0,
        webhook_retry_base_delay=0.01,
        webhook_concurrency=1,
    )
    database = DatabaseManager(settings)
    received_key = os.environ["TEST_RECEIVED_KEY"]
    sender = CrashSender(
        settings,
        phase="after_http" if mode == "after_http" else "resume",
        received_key=received_key,
    )
    service = NotificationDeliveryService(database.session_factory, settings, sender=sender)
    worker = NotificationWorker(service, database.session_factory, settings)
    delivery_id = UUID(os.environ["TEST_DELIVERY_ID"])
    try:
        if mode == "resume":
            await worker.recover_stale_sending()
        if mode == "after_http":
            await service.deliver(delivery_id)
        else:
            await service.deliver(delivery_id)
    finally:
        await database.dispose()


async def _run_scheduler(mode: str) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ["TEST_DATABASE_URL"],
        redis_url=os.environ["TEST_REDIS_URL"],
        redis_stream_name=os.environ["TEST_STREAM"],
        gmail_sync_compensation_lock_ttl_seconds=1,
    )
    database = DatabaseManager(settings)
    redis = RedisManager(settings)
    scheduler: GmailCompensationScheduler = (
        CrashScheduler(database.session_factory, redis, settings)
        if mode == "crash"
        else GmailCompensationScheduler(database.session_factory, redis, settings)
    )
    try:
        await scheduler.run_once(limit=1)
    finally:
        await database.dispose()
        await redis.close()


async def _run_message(mode: str) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ["TEST_DATABASE_URL"],
        redis_url=os.environ["TEST_REDIS_URL"],
        gmail_sync_worker_concurrency=1,
    )
    database = DatabaseManager(settings)
    service = CrashMessageService(
        database, "before_commit" if mode == "message_crash" else "resume"
    )
    worker = MessageWorker(cast(Any, service), database.session_factory, settings)
    try:
        await worker.run_once(limit=1)
    finally:
        await database.dispose()


async def main() -> None:
    mode = os.environ["TEST_MODE"]
    if mode in {"before_commit", "after_commit", "resume"}:
        await _run_sync(mode)
    elif mode in {"after_http", "resume_notification"}:
        await _run_notification("after_http" if mode == "after_http" else "resume")
    elif mode in {"scheduler_crash", "scheduler_resume"}:
        await _run_scheduler("crash" if mode == "scheduler_crash" else "resume")
    elif mode in {"message_crash", "message_resume"}:
        await _run_message(mode)
    else:
        raise SystemExit(f"unknown TEST_MODE: {mode}")


if __name__ == "__main__":
    asyncio.run(main())
