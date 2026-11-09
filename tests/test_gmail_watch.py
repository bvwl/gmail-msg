from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from app.config import Settings
from app.db.base import GmailSyncState, Mailbox
from app.gmail.watch import GmailWatchClientProtocol, GmailWatchManager
from app.state_machine import MailboxStatus, SyncMode


async def _completed() -> None:
    return None


class FakeWatchClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = responses
        self.watch_calls: list[dict[str, object]] = []
        self.stop_calls = 0

    async def watch(self, mailbox_id: object, **kwargs: object) -> dict[str, object]:
        self.watch_calls.append({"mailbox_id": mailbox_id, **kwargs})
        return self.responses.pop(0)

    async def stop(self, _mailbox_id: object) -> dict[str, object]:
        self.stop_calls += 1
        return {}


class FlakyWatchClient(FakeWatchClient):
    def __init__(self, responses: list[dict[str, object]], failures: int) -> None:
        super().__init__(responses)
        self.failures = failures

    async def watch(self, mailbox_id: object, **kwargs: object) -> dict[str, object]:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("temporary watch failure")
        return await super().watch(mailbox_id, **kwargs)


class FakeSession:
    def __init__(self, mailbox: Mailbox, sync_state: GmailSyncState | None = None) -> None:
        self.mailbox = mailbox
        self.sync_state = sync_state
        self.scalar_calls = 0
        self.added: list[object] = []

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    async def scalar(self, _statement: object) -> object:
        self.scalar_calls += 1
        if self.scalar_calls == 1:
            return self.mailbox
        return self.sync_state

    async def flush(self) -> None:
        return None

    async def execute(self, _statement: object, _params: object = None) -> object:
        return object()

    def in_transaction(self) -> bool:
        return True

    def add(self, value: object) -> None:
        self.added.append(value)
        if isinstance(value, GmailSyncState):
            self.sync_state = value


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        gmail_watch_topic="projects/PROJECT_ID/topics/gmail-events",
        gmail_watch_retry_max_attempts=2,
    )


@pytest.mark.asyncio
async def test_watch_registration_records_expiration_and_baseline() -> None:
    mailbox_id = uuid4()
    mailbox = Mailbox(id=mailbox_id, email="user@example.com", status=MailboxStatus.AUTHORIZED)
    session = FakeSession(mailbox)
    client = FakeWatchClient([{"historyId": "123", "expiration": "1890000000000"}])
    manager = GmailWatchManager(client, lambda: session, settings())  # type: ignore[arg-type]

    registration = await manager.register(mailbox_id)

    assert registration.history_id == "123"
    assert mailbox.watch_topic == "projects/PROJECT_ID/topics/gmail-events"
    assert mailbox.watch_expiration_at is not None
    assert mailbox.status == MailboxStatus.WATCH_ACTIVE
    assert session.sync_state is not None
    assert session.sync_state.baseline_history_id == "123"
    assert session.sync_state.cursor_history_id == "123"
    assert session.sync_state.mode == SyncMode.INCREMENTAL


@pytest.mark.asyncio
async def test_watch_registration_does_not_overwrite_existing_cursor() -> None:
    mailbox_id = uuid4()
    mailbox = Mailbox(id=mailbox_id, email="user@example.com", status=MailboxStatus.WATCH_ACTIVE)
    sync_state = GmailSyncState(
        mailbox_id=mailbox_id,
        baseline_history_id="100",
        cursor_history_id="110",
        last_observed_history_id="110",
        mode=SyncMode.INCREMENTAL,
    )
    session = FakeSession(mailbox, sync_state)
    client = FakeWatchClient([{"historyId": "120", "expiration": "1890000000000"}])
    manager = GmailWatchManager(client, lambda: session, settings())  # type: ignore[arg-type]

    await manager.register(mailbox_id)

    assert sync_state.cursor_history_id == "110"
    assert sync_state.baseline_history_id == "100"
    assert sync_state.last_observed_history_id == "120"


@pytest.mark.asyncio
async def test_watch_stop_clears_expiration_and_status() -> None:
    mailbox = Mailbox(id=uuid4(), email="user@example.com", status=MailboxStatus.WATCH_ACTIVE)
    mailbox.watch_expiration_at = datetime.now(UTC)
    session = FakeSession(mailbox)
    client = FakeWatchClient([])
    manager = GmailWatchManager(client, lambda: session, settings())  # type: ignore[arg-type]

    await manager.stop(mailbox.id)

    assert client.stop_calls == 1
    assert mailbox.watch_expiration_at is None
    assert mailbox.status == MailboxStatus.AUTHORIZED


@pytest.mark.asyncio
async def test_watch_registration_retries_temporary_failure() -> None:
    mailbox_id = uuid4()
    mailbox = Mailbox(id=mailbox_id, email="user@example.com", status=MailboxStatus.AUTHORIZED)
    session = FakeSession(mailbox)
    client = FlakyWatchClient([{"historyId": "123", "expiration": "1890000000000"}], failures=1)
    manager = GmailWatchManager(
        cast(GmailWatchClientProtocol, client),
        lambda: session,  # type: ignore[arg-type]
        settings(),
        sleep=lambda _seconds: _completed(),
    )

    await manager.register(mailbox_id)

    assert len(client.watch_calls) == 1


@pytest.mark.asyncio
async def test_expired_watch_is_renewed_under_mailbox_lock() -> None:
    mailbox_id = uuid4()
    mailbox = Mailbox(id=mailbox_id, email="user@example.com", status=MailboxStatus.WATCH_ACTIVE)
    mailbox.watch_expiration_at = datetime(2026, 11, 5, tzinfo=UTC)
    session = FakeSession(mailbox)
    client = FakeWatchClient([{"historyId": "124", "expiration": "1890000000000"}])
    manager = GmailWatchManager(
        cast(GmailWatchClientProtocol, client),
        lambda: session,  # type: ignore[arg-type]
        settings(),
        clock=lambda: datetime(2026, 11, 5, 12, tzinfo=UTC),
    )

    registration = await manager.renew(mailbox_id)

    assert registration.history_id == "124"
    assert len(client.watch_calls) == 1
