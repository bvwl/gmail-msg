"""数据库事务边界和邮箱级 PostgreSQL advisory lock。"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

MAILBOX_ADVISORY_LOCK_SQL: TextClause = text("SELECT pg_advisory_xact_lock(:lock_key)")


def mailbox_lock_key(mailbox_id: UUID) -> int:
    """将邮箱 UUID 稳定映射为 PostgreSQL advisory lock 的有符号 bigint。"""

    digest = hashlib.blake2b(mailbox_id.bytes, digest_size=8).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)


async def acquire_mailbox_advisory_lock(session: AsyncSession, mailbox_id: UUID) -> None:
    """在当前事务中获取邮箱锁。

    PostgreSQL 的 ``pg_advisory_xact_lock`` 会在事务提交或回滚时自动释放。
    如果调用方没有先开启事务，直接获取锁会让锁的生命周期脱离游标更新，
    因此这里明确拒绝该用法。
    """

    if not session.in_transaction():
        raise RuntimeError("mailbox advisory lock requires an active transaction")
    await session.execute(
        MAILBOX_ADVISORY_LOCK_SQL,
        {"lock_key": mailbox_lock_key(mailbox_id)},
    )


@asynccontextmanager
async def transaction(
    session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """创建 Session 并提供显式的提交/回滚边界。"""

    session = session_factory()
    try:
        async with session.begin():
            yield session
    finally:
        await session.close()


@asynccontextmanager
async def mailbox_transaction(
    session_factory: async_sessionmaker[AsyncSession] | Callable[[], AsyncSession],
    mailbox_id: UUID,
) -> AsyncIterator[AsyncSession]:
    """在一个事务内获取邮箱锁，供游标推进等串行操作使用。"""

    async with transaction(session_factory) as session:
        await acquire_mailbox_advisory_lock(session, mailbox_id)
        yield session


__all__ = [
    "MAILBOX_ADVISORY_LOCK_SQL",
    "acquire_mailbox_advisory_lock",
    "mailbox_lock_key",
    "mailbox_transaction",
    "transaction",
]
