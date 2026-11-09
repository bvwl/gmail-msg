from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.base import Message, RetentionRun
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class RetentionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], days: int = 30) -> None:
        self.session_factory, self.days = session_factory, days

    async def run_once(self) -> RetentionRun:
        now = datetime.now(UTC)
        run = RetentionRun(started_at=now, cutoff_at=now - timedelta(days=self.days))
        async with self.session_factory() as session:
            session.add(run)
            await session.flush()
            try:
                result = await session.execute(
                    select(Message.id).where(Message.stored_at < run.cutoff_at)
                )
                ids = [row[0] for row in result]
                run.scanned_count = len(ids)
                if ids:
                    await session.execute(delete(Message).where(Message.id.in_(ids)))
                run.deleted_count = len(ids)
                run.status = "completed"
            except Exception:
                run.status = "failed"
                run.failed_count = 1
                run.error_code = "retention_failed"
            run.finished_at = datetime.now(UTC)
            await session.commit()
        return run
