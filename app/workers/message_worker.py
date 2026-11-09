"""获取并解析 Gmail 邮件的独立 Worker。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.config import Settings, get_settings
from app.db.base import GmailMessage
from app.gmail.message_service import MessageContentService
from app.state_machine import GmailMessageState


class MessageWorker:
    def __init__(
        self, service: MessageContentService, session_factory: Any, settings: Settings | None = None
    ) -> None:
        self.service = service
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self._stop = asyncio.Event()

    async def run_once(self, *, limit: int | None = None) -> int:
        max_items = limit or self.settings.gmail_sync_worker_concurrency
        await self.recover_stale_fetching()
        async with self.session_factory() as session:
            result = await session.execute(
                select(GmailMessage.mailbox_id, GmailMessage.gmail_message_id)
                .where(
                    GmailMessage.state.in_(
                        [
                            GmailMessageState.DISCOVERED,
                            GmailMessageState.FETCH_RETRY,
                            GmailMessageState.PARSE_FAILED,
                        ]
                    )
                )
                .order_by(GmailMessage.created_at)
                .limit(max_items)
            )
            pairs = list(result.all())
        semaphore = asyncio.Semaphore(self.settings.gmail_sync_worker_concurrency)

        async def process(pair: Any) -> None:
            async with semaphore:
                mailbox_id, gmail_message_id = pair
                await self.service.process_message_id(mailbox_id, gmail_message_id)

        await asyncio.gather(*(process(pair) for pair in pairs))
        return len(pairs)

    async def recover_stale_fetching(self) -> int:
        now = datetime.now(UTC)
        cutoff = now.timestamp() - max(60.0, self.settings.gmail_http_timeout_seconds * 2)
        changed = 0
        async with self.session_factory() as session:
            async with session.begin():
                rows = await session.scalars(
                    select(GmailMessage).where(GmailMessage.state == GmailMessageState.FETCHING)
                )
                for message in list(rows.all() if hasattr(rows, "all") else rows):
                    if message.updated_at is not None and message.updated_at.timestamp() <= cutoff:
                        message.state = GmailMessageState.DISCOVERED
                        message.last_error_code = "stale_fetching_recovered"
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
    client = getattr(application.state, "gmail_client", None)
    if database is None or client is None:
        raise RuntimeError("message worker requires database and Gmail client")
    service = MessageContentService(client, database.session_factory, settings)
    worker = MessageWorker(service, database.session_factory, settings)
    try:
        await worker.run()
    finally:
        await database.dispose()
        redis = getattr(application.state, "redis", None)
        if redis is not None:
            await redis.close()


if __name__ == "__main__":
    asyncio.run(run_worker())


__all__ = ["MessageWorker", "run_worker"]
