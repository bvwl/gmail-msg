from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from app.db.base import GmailMessage, GmailSyncState, Mailbox, NotificationDelivery
from app.state_machine import (
    GmailMessageState,
    InvalidStateTransitionError,
    MailboxStatus,
    NotificationState,
    SyncMode,
    transition_mailbox,
    transition_message,
    transition_notification,
    transition_sync_state,
)
from sqlalchemy.ext.asyncio import AsyncSession


class FakeSession:
    def __init__(self) -> None:
        self.flush = AsyncMock()


@pytest.mark.asyncio
async def test_valid_state_transitions_are_recorded() -> None:
    session = FakeSession()
    mailbox = SimpleNamespace(id=uuid4(), status=MailboxStatus.PENDING_AUTH)
    sync_state = SimpleNamespace(mailbox_id=mailbox.id, mode=SyncMode.BASELINE_REQUIRED)
    message = SimpleNamespace(id=uuid4(), state=GmailMessageState.DISCOVERED)
    delivery = SimpleNamespace(id=uuid4(), state=NotificationState.PENDING)

    typed_session = cast(AsyncSession, session)
    mailbox_change = await transition_mailbox(
        typed_session, cast(Mailbox, mailbox), MailboxStatus.AUTHORIZED
    )
    sync_change = await transition_sync_state(
        typed_session, cast(GmailSyncState, sync_state), SyncMode.INCREMENTAL
    )
    message_change = await transition_message(
        typed_session, cast(GmailMessage, message), GmailMessageState.FETCHING
    )
    delivery_change = await transition_notification(
        typed_session, cast(NotificationDelivery, delivery), NotificationState.SENDING
    )

    assert (mailbox_change.previous, mailbox_change.current) == ("pending_auth", "authorized")
    assert (sync_change.previous, sync_change.current) == ("baseline_required", "incremental")
    assert (message_change.previous, message_change.current) == ("discovered", "fetching")
    assert (delivery_change.previous, delivery_change.current) == ("pending", "sending")
    assert session.flush.await_count == 4


@pytest.mark.asyncio
async def test_invalid_state_transition_is_rejected_without_flush() -> None:
    session = FakeSession()
    mailbox = SimpleNamespace(id=uuid4(), status=MailboxStatus.PENDING_AUTH)

    with pytest.raises(InvalidStateTransitionError, match="pending_auth.*watch_active"):
        await transition_mailbox(
            cast(AsyncSession, session), cast(Mailbox, mailbox), MailboxStatus.WATCH_ACTIVE
        )

    assert mailbox.status == MailboxStatus.PENDING_AUTH
    session.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_state_cannot_be_reopened() -> None:
    session = FakeSession()
    delivery = SimpleNamespace(id=uuid4(), state=NotificationState.DELIVERED)

    with pytest.raises(InvalidStateTransitionError):
        await transition_notification(
            cast(AsyncSession, session),
            cast(NotificationDelivery, delivery),
            NotificationState.RETRY,
        )


@pytest.mark.asyncio
async def test_same_state_transition_is_idempotent() -> None:
    session = FakeSession()
    sync_state = SimpleNamespace(mailbox_id=uuid4(), mode=SyncMode.INCREMENTAL)

    change = await transition_sync_state(
        cast(AsyncSession, session), cast(GmailSyncState, sync_state), SyncMode.INCREMENTAL
    )

    assert change.previous == change.current == "incremental"
    session.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_parse_failed_message_can_be_reprocessed() -> None:
    session = FakeSession()
    message = SimpleNamespace(id=uuid4(), state=GmailMessageState.PARSE_FAILED)
    change = await transition_message(
        cast(AsyncSession, session), cast(GmailMessage, message), GmailMessageState.FETCHING
    )
    assert change.current == GmailMessageState.FETCHING
