"""补偿同步 Scheduler。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import SyncRequest
from app.redis_client import RedisManager

logger = logging.getLogger(__name__)


class GmailCompensationScheduler:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: RedisManager,
        settings: Settings | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()

    async def run_once(self, *, limit: int = 100) -> int:
        token = uuid.uuid4().hex
        lock_key = f"{self.settings.redis_stream_name}:compensation-lock"
        acquire_lock = getattr(self.redis, "acquire_lock", None)
        release_lock = getattr(self.redis, "release_lock", None)
        if acquire_lock is not None and not await acquire_lock(
            lock_key,
            token,
            ttl_seconds=self.settings.gmail_sync_compensation_lock_ttl_seconds,
        ):
            return 0
        now = datetime.now(UTC)
        try:
            async with self.session_factory() as session:
                retryable = SyncRequest.status.in_(["queued", "retry"]) & (
                    (SyncRequest.next_attempt_at.is_(None)) | (SyncRequest.next_attempt_at <= now)
                )
                stale_enqueued = (SyncRequest.status == "enqueued") & (
                    SyncRequest.updated_at
                    <= now
                    - timedelta(seconds=self.settings.gmail_sync_compensation_interval_seconds)
                )
                rows = await session.scalars(
                    select(SyncRequest)
                    .where(retryable | stale_enqueued)
                    .order_by(SyncRequest.created_at)
                    .limit(limit)
                )
                requests = list(rows.all()) if hasattr(rows, "all") else list(rows)
            queued = 0
            for request in requests:
                try:
                    await self.redis.enqueue_sync(
                        self.settings.redis_stream_name,
                        mailbox_id=str(request.mailbox_id),
                        observed_history_id=request.observed_history_id,
                        request_id=str(request.id),
                        reason="compensation",
                        maxlen=self.settings.redis_stream_maxlen,
                    )
                except Exception:
                    continue
                try:
                    await self._mark_enqueued(request.id)
                except Exception:
                    # Redis 已经持久化了请求；数据库状态保留为 queued，下一轮
                    # 补偿会再次投递同一个 request_id，Worker 依靠幂等约束收敛。
                    logger.warning("failed to mark sync request enqueued", exc_info=True)
                    continue
                queued += 1
            return queued
        finally:
            if release_lock is not None:
                await release_lock(lock_key, token)

    async def _mark_enqueued(self, request_id: uuid.UUID) -> None:
        """在 Redis 入队成功后记录数据库状态。

        入队与数据库提交之间没有跨系统事务；若进程在此处崩溃，补偿扫描会
        依据原状态再次入队，而 Worker 通过 request_id 和邮件唯一键保持幂等。
        """

        async with self.session_factory() as session:
            async with session.begin():
                request = await session.scalar(
                    select(SyncRequest).where(SyncRequest.id == request_id).with_for_update()
                )
                if request is not None and request.status in {"queued", "retry"}:
                    request.status = "enqueued"
                    request.last_error_code = None

    async def run(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.gmail_sync_compensation_interval_seconds,
                )
            except TimeoutError:
                continue

    def stop(self) -> None:
        self._stop.set()


async def run_scheduler() -> None:
    from app.config import get_settings
    from app.main import create_app

    application = create_app(get_settings())
    database = getattr(application.state, "database", None)
    redis = getattr(application.state, "redis", None)
    if database is None or redis is None:
        raise RuntimeError("scheduler requires database and Redis configuration")
    scheduler = GmailCompensationScheduler(
        database.session_factory,
        redis,
        application.state.settings,
    )
    try:
        await scheduler.run()
    finally:
        await database.dispose()
        await redis.close()


if __name__ == "__main__":
    asyncio.run(run_scheduler())
