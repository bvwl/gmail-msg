"""独立业务通知 Worker。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select

from app.config import Settings, get_settings
from app.db.base import NotificationDelivery
from app.notifications.delivery import NotificationDeliveryService
from app.state_machine import NotificationState


class NotificationWorker:
    def __init__(
        self,
        service: NotificationDeliveryService,
        session_factory: Any,
        settings: Settings | None = None,
    ) -> None:
        self.service = service
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()

    async def run_once(self, *, limit: int | None = None) -> int:
        max_items = limit or self.settings.webhook_concurrency
        now = datetime.now(UTC)
        await self.recover_stale_sending(now=now)
        async with self.session_factory() as session:
            rows = await session.scalars(
                select(NotificationDelivery.id)
                .where(
                    NotificationDelivery.state.in_(
                        [NotificationState.PENDING, NotificationState.RETRY]
                    ),
                    or_(
                        NotificationDelivery.next_attempt_at.is_(None),
                        NotificationDelivery.next_attempt_at <= now,
                    ),
                )
                .order_by(NotificationDelivery.created_at)
                .limit(max_items)
            )
            ids = list(rows.all() if hasattr(rows, "all") else rows)
        semaphore = asyncio.Semaphore(self.settings.webhook_concurrency)

        async def process(item_id: Any) -> None:
            async with semaphore:
                await self.service.deliver(item_id)

        await asyncio.gather(*(process(item_id) for item_id in ids))
        return len(ids)

    async def recover_stale_sending(self, *, now: datetime | None = None) -> int:
        """进程在 HTTP 调用期间崩溃后，把旧 sending 任务交还重试队列。"""

        current = now or datetime.now(UTC)
        cutoff = current.timestamp() - max(60.0, self.settings.webhook_timeout * 2)
        changed = 0
        async with self.session_factory() as session:
            async with session.begin():
                rows = await session.scalars(
                    select(NotificationDelivery).where(
                        NotificationDelivery.state == NotificationState.SENDING
                    )
                )
                for delivery in list(rows.all() if hasattr(rows, "all") else rows):
                    updated = delivery.updated_at
                    if updated is not None and updated.timestamp() <= cutoff:
                        delivery.state = NotificationState.RETRY
                        delivery.next_attempt_at = current
                        delivery.last_error_code = "stale_sending_recovered"
                        changed += 1
        return changed

    async def run(self, *, poll_seconds: float = 1.0) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
            except TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()


async def run_worker() -> None:
    from app.main import create_app

    settings = get_settings()
    application = create_app(settings)
    database = getattr(application.state, "database", None)
    if database is None:
        raise RuntimeError("notification worker requires database")
    service = NotificationDeliveryService(database.session_factory, settings)
    worker = NotificationWorker(service, database.session_factory, settings)
    try:
        await worker.run()
    finally:
        await database.dispose()
        redis = getattr(application.state, "redis", None)
        if redis is not None:
            await redis.close()


if __name__ == "__main__":
    asyncio.run(run_worker())


__all__ = ["NotificationWorker", "run_worker"]
