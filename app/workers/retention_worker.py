from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db.session import build_database
from app.services.retention import RetentionService


async def run_retention_forever(
    service: RetentionService, interval_seconds: int, stop_event: asyncio.Event
) -> None:
    while not stop_event.is_set():
        await service.run_once()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


async def run() -> None:
    settings = get_settings()
    database = build_database(settings)
    stop_event = asyncio.Event()
    try:
        await run_retention_forever(
            RetentionService(database.session_factory, days=settings.message_retention_days),
            settings.cleanup_interval_seconds,
            stop_event,
        )
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
