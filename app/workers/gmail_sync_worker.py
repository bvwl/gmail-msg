"""Gmail 同步 Consumer Group Worker。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import SyncRequest
from app.gmail.sync import GmailSyncService
from app.redis_client import RedisManager, StreamMessage, new_consumer_name

logger = logging.getLogger(__name__)


class GmailSyncWorker:
    def __init__(
        self,
        sync_service: GmailSyncService,
        redis: RedisManager,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
        *,
        consumer_name: str | None = None,
    ) -> None:
        self.sync_service = sync_service
        self.redis = redis
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.consumer_name = (
            consumer_name or self.settings.redis_stream_consumer or new_consumer_name()
        )
        self._stop = asyncio.Event()

    async def process_message(self, message: StreamMessage) -> bool:
        try:
            mailbox_id = UUID(message.mailbox_id)
        except ValueError:
            await self._dead_letter(message, "invalid_mailbox_id", 1)
            return False
        try:
            await asyncio.wait_for(
                self.sync_service.sync_mailbox(
                    mailbox_id,
                    observed_history_id=message.observed_history_id,
                ),
                timeout=self.settings.gmail_sync_task_timeout_seconds,
            )
        except Exception as exc:
            try:
                attempts = await self._record_failure(message, type(exc).__name__)
            except Exception:
                await self._dead_letter(message, "request_state_unavailable", 1)
                return False
            if attempts >= self.settings.redis_stream_max_retries:
                await self._dead_letter(message, type(exc).__name__, attempts)
                return False
            return False
        await self._mark_completed(message)
        await self.redis.ack(
            self.settings.redis_stream_name,
            self.settings.redis_stream_group,
            message.id,
        )
        return True

    async def run_once(self, *, count: int | None = None) -> int:
        messages = await self.redis.read_group(
            self.settings.redis_stream_name,
            self.settings.redis_stream_group,
            self.consumer_name,
            count=count or self.settings.gmail_sync_worker_concurrency,
            block_ms=self.settings.redis_stream_block_ms,
        )
        if not messages:
            return 0
        semaphore = asyncio.Semaphore(self.settings.gmail_sync_worker_concurrency)

        async def process(message: StreamMessage) -> None:
            async with semaphore:
                await self.process_message(message)

        await asyncio.gather(*(process(message) for message in messages))
        return len(messages)

    async def reclaim(self, *, min_idle_ms: int = 60_000) -> int:
        _next, messages, _deleted = await self.redis.autoclaim(
            self.settings.redis_stream_name,
            self.settings.redis_stream_group,
            self.consumer_name,
            min_idle_ms=min_idle_ms,
            count=self.settings.gmail_sync_worker_concurrency,
        )
        for message in messages:
            await self.process_message(message)
        return len(messages)

    async def run(self) -> None:
        await self.redis.ensure_consumer_group(
            self.settings.redis_stream_name,
            self.settings.redis_stream_group,
        )
        while not self._stop.is_set():
            await self.reclaim()
            await self.run_once()

    def stop(self) -> None:
        self._stop.set()

    async def _record_failure(self, message: StreamMessage, error_code: str) -> int:
        async with self.session_factory() as session:
            async with session.begin():
                request = await session.get(
                    SyncRequest, UUID(message.request_id), with_for_update=True
                )
                if request is None:
                    return 1
                request.attempt_count += 1
                request.status = "retry"
                request.last_error_code = error_code[:64]
                request.next_attempt_at = datetime.now(UTC)
                return request.attempt_count

    async def _mark_completed(self, message: StreamMessage) -> None:
        try:
            request_id = UUID(message.request_id)
        except ValueError:
            return
        async with self.session_factory() as session:
            async with session.begin():
                request = await session.get(SyncRequest, request_id, with_for_update=True)
                if request is not None:
                    request.status = "completed"
                    request.next_attempt_at = None
                    request.last_error_code = None

    async def _dead_letter(self, message: StreamMessage, error_code: str, attempts: int) -> None:
        await self.redis.move_to_dead_letter(
            self.settings.redis_stream_dead_letter_name,
            message,
            error_code=error_code,
            attempts=attempts,
            maxlen=self.settings.redis_stream_maxlen,
        )
        try:
            request_id = UUID(message.request_id)
        except ValueError:
            request_id = None
        if request_id is not None:
            async with self.session_factory() as session:
                async with session.begin():
                    request = await session.get(SyncRequest, request_id, with_for_update=True)
                    if request is not None:
                        request.status = "dead_letter"
                        request.last_error_code = error_code[:64]
        await self.redis.ack(
            self.settings.redis_stream_name,
            self.settings.redis_stream_group,
            message.id,
        )


async def run_worker() -> None:
    """使用应用同一套配置独立启动 Worker。"""

    from app.config import get_settings
    from app.gmail.sync import GmailSyncService
    from app.main import create_app

    application = create_app(get_settings())
    client = getattr(application.state, "gmail_client", None)
    database = getattr(application.state, "database", None)
    redis = getattr(application.state, "redis", None)
    if client is None or database is None or redis is None:
        raise RuntimeError("worker requires OAuth, database and Redis configuration")
    worker = GmailSyncWorker(
        GmailSyncService(client, database.session_factory, application.state.settings),
        redis,
        database.session_factory,
        application.state.settings,
    )
    try:
        await worker.run()
    finally:
        await database.dispose()
        await redis.close()


if __name__ == "__main__":
    asyncio.run(run_worker())
