from __future__ import annotations

from collections.abc import Callable
from typing import cast
from uuid import UUID, uuid4

import pytest
from app.db.transaction import (
    MAILBOX_ADVISORY_LOCK_SQL,
    acquire_mailbox_advisory_lock,
    mailbox_lock_key,
    mailbox_transaction,
    transaction,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class FakeTransaction:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    async def __aenter__(self) -> FakeSession:
        self.session.active = True
        self.session.events.append("begin")
        return self.session

    async def __aexit__(self, exc_type: object, _exc: object, _tb: object) -> bool:
        self.session.active = False
        self.session.events.append("rollback" if exc_type else "commit")
        return False


class FakeSession:
    def __init__(self) -> None:
        self.active = False
        self.closed = False
        self.events: list[str] = []
        self.executed: list[tuple[object, dict[str, int]]] = []

    def begin(self) -> FakeTransaction:
        return FakeTransaction(self)

    def in_transaction(self) -> bool:
        return self.active

    async def execute(self, statement: object, params: dict[str, int]) -> None:
        self.executed.append((statement, params))
        self.events.append("lock")

    async def close(self) -> None:
        self.closed = True
        self.events.append("close")


def test_mailbox_lock_key_is_stable_and_uses_signed_bigint() -> None:
    mailbox_id = UUID("00000000-0000-0000-0000-000000000001")
    other_mailbox_id = UUID("00000000-0000-0000-0000-000000000002")

    assert mailbox_lock_key(mailbox_id) == mailbox_lock_key(mailbox_id)
    assert -(2**63) <= mailbox_lock_key(mailbox_id) < 2**63
    assert mailbox_lock_key(mailbox_id) != mailbox_lock_key(other_mailbox_id)


@pytest.mark.asyncio
async def test_advisory_lock_requires_active_transaction() -> None:
    session = FakeSession()

    with pytest.raises(RuntimeError, match="active transaction"):
        await acquire_mailbox_advisory_lock(cast(AsyncSession, session), uuid4())

    assert session.executed == []


@pytest.mark.asyncio
async def test_transaction_commits_and_closes_session() -> None:
    session = FakeSession()

    factory = cast(Callable[[], AsyncSession], lambda: session)
    expected_session = cast(AsyncSession, session)
    async with transaction(factory) as active_session:
        assert active_session is expected_session
        assert session.in_transaction()

    assert session.events == ["begin", "commit", "close"]
    assert session.closed


@pytest.mark.asyncio
async def test_transaction_rolls_back_on_exception() -> None:
    session = FakeSession()

    with pytest.raises(ValueError, match="boom"):
        factory = cast(Callable[[], AsyncSession], lambda: session)
        async with transaction(factory):
            raise ValueError("boom")

    assert session.events == ["begin", "rollback", "close"]
    assert session.closed


@pytest.mark.asyncio
async def test_mailbox_transaction_locks_before_user_work() -> None:
    session = FakeSession()
    mailbox_id = UUID("00000000-0000-0000-0000-000000000001")

    factory = cast(Callable[[], AsyncSession], lambda: session)
    expected_session = cast(AsyncSession, session)
    async with mailbox_transaction(factory, mailbox_id) as active_session:
        assert active_session is expected_session
        assert session.events == ["begin", "lock"]
        statement, params = session.executed[0]
        assert statement is MAILBOX_ADVISORY_LOCK_SQL
        assert params == {"lock_key": mailbox_lock_key(mailbox_id)}
        session.events.append("work")

    assert session.events == ["begin", "lock", "work", "commit", "close"]


@pytest.mark.asyncio
async def test_database_manager_exposes_transaction_helpers() -> None:
    from app.db.session import DatabaseManager

    session = FakeSession()
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.session_factory = cast(async_sessionmaker[AsyncSession], lambda: session)
    expected_session = cast(AsyncSession, session)

    async with manager.transaction() as active_session:
        assert active_session is expected_session

    assert session.events == ["begin", "commit", "close"]


@pytest.mark.asyncio
async def test_database_manager_exposes_mailbox_transaction_helper() -> None:
    from app.db.session import DatabaseManager

    session = FakeSession()
    manager = DatabaseManager.__new__(DatabaseManager)
    manager.session_factory = cast(async_sessionmaker[AsyncSession], lambda: session)
    mailbox_id = UUID("00000000-0000-0000-0000-000000000001")
    expected_session = cast(AsyncSession, session)

    async with manager.mailbox_transaction(mailbox_id) as active_session:
        assert active_session is expected_session

    assert session.events == ["begin", "lock", "commit", "close"]
