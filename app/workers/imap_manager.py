"""邮箱 Worker 管理器：每个启用邮箱独立一个 asyncio Task。"""

from __future__ import annotations

import asyncio
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.base import Mailbox
from app.imap.client import ImapConnectionFactory
from app.imap.worker import MailboxWorker
from app.services.sync import ImapSyncService


class ImapWorkerManager:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        connection_factory: ImapConnectionFactory | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.connection_factory = connection_factory or ImapConnectionFactory()
        self.tasks: dict[UUID, tuple[asyncio.Task[None], MailboxWorker]] = {}

    async def refresh(self) -> None:
        async with self.session_factory() as session:
            ids = list(
                (
                    await session.execute(select(Mailbox.id).where(Mailbox.enabled.is_(True)))
                ).scalars()
            )
        for mailbox_id in ids:
            if mailbox_id not in self.tasks:
                worker = MailboxWorker(
                    mailbox_id,
                    self.session_factory,
                    self.connection_factory,
                    ImapSyncService(
                        self.session_factory,
                        max_body_chars=get_settings().message_body_max_chars,
                    ),
                )
                self.tasks[mailbox_id] = (
                    asyncio.create_task(worker.run_forever(), name=f"imap-worker-{mailbox_id}"),
                    worker,
                )
        for mailbox_id in list(self.tasks):
            if mailbox_id not in ids:
                task, worker = self.tasks.pop(mailbox_id)
                worker.stop()
                task.cancel()

    async def stop(self) -> None:
        for task, worker in self.tasks.values():
            worker.stop()
            task.cancel()
        if self.tasks:
            await asyncio.gather(*(task for task, _ in self.tasks.values()), return_exceptions=True)
        self.tasks.clear()
