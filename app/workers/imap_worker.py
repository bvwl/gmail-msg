"""独立运行所有已启用邮箱的 IMAP Worker。"""

from __future__ import annotations

import asyncio

from app.config import get_settings
from app.db.session import build_database
from app.workers.imap_manager import ImapWorkerManager


async def run() -> None:
    database = build_database(get_settings())
    manager = ImapWorkerManager(database.session_factory)
    try:
        while True:
            await manager.refresh()
            await asyncio.sleep(2)
    finally:
        await manager.stop()
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(run())
