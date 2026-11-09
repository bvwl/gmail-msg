"""数据库实体状态机和并发安全的状态更新辅助函数。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import GmailMessage, GmailSyncState, Mailbox, NotificationDelivery

logger = logging.getLogger(__name__)


class MailboxStatus(StrEnum):
    PENDING_AUTH = "pending_auth"
    AUTHORIZED = "authorized"
    WATCH_ACTIVE = "watch_active"
    DEGRADED = "degraded"
    REAUTH_REQUIRED = "reauth_required"
    DISABLED = "disabled"


class SyncMode(StrEnum):
    BASELINE_REQUIRED = "baseline_required"
    INCREMENTAL = "incremental"
    FULL_RECOVERY_REQUIRED = "full_recovery_required"
    PAUSED = "paused"


class GmailMessageState(StrEnum):
    DISCOVERED = "discovered"
    FETCHING = "fetching"
    PARSED = "parsed"
    FETCH_RETRY = "fetch_retry"
    FETCH_FAILED = "fetch_failed"
    PARSE_FAILED = "parse_failed"
    NOTIFICATION_PENDING = "notification_pending"
    DELIVERED = "delivered"
    NOTIFICATION_RETRY = "notification_retry"
    NOTIFICATION_DEAD_LETTER = "notification_dead_letter"


class NotificationState(StrEnum):
    PENDING = "pending"
    SENDING = "sending"
    RETRY = "retry"
    DELIVERED = "delivered"
    DEAD_LETTER = "dead_letter"


MAILBOX_TRANSITIONS: dict[str, frozenset[str]] = {
    MailboxStatus.PENDING_AUTH: frozenset({MailboxStatus.AUTHORIZED}),
    MailboxStatus.AUTHORIZED: frozenset({MailboxStatus.WATCH_ACTIVE, MailboxStatus.DEGRADED}),
    MailboxStatus.WATCH_ACTIVE: frozenset(
        {MailboxStatus.AUTHORIZED, MailboxStatus.DEGRADED, MailboxStatus.REAUTH_REQUIRED}
    ),
    MailboxStatus.DEGRADED: frozenset(
        {MailboxStatus.AUTHORIZED, MailboxStatus.WATCH_ACTIVE, MailboxStatus.REAUTH_REQUIRED}
    ),
    MailboxStatus.REAUTH_REQUIRED: frozenset(
        {MailboxStatus.PENDING_AUTH, MailboxStatus.AUTHORIZED, MailboxStatus.DISABLED}
    ),
    MailboxStatus.DISABLED: frozenset(),
}

SYNC_TRANSITIONS: dict[str, frozenset[str]] = {
    SyncMode.BASELINE_REQUIRED: frozenset({SyncMode.INCREMENTAL, SyncMode.PAUSED}),
    SyncMode.INCREMENTAL: frozenset({SyncMode.FULL_RECOVERY_REQUIRED, SyncMode.PAUSED}),
    SyncMode.FULL_RECOVERY_REQUIRED: frozenset({SyncMode.INCREMENTAL, SyncMode.PAUSED}),
    SyncMode.PAUSED: frozenset({SyncMode.BASELINE_REQUIRED, SyncMode.INCREMENTAL}),
}

MESSAGE_TRANSITIONS: dict[str, frozenset[str]] = {
    GmailMessageState.DISCOVERED: frozenset({GmailMessageState.FETCHING}),
    GmailMessageState.FETCHING: frozenset(
        {
            GmailMessageState.PARSED,
            GmailMessageState.FETCH_RETRY,
            GmailMessageState.FETCH_FAILED,
            GmailMessageState.PARSE_FAILED,
        }
    ),
    GmailMessageState.FETCH_RETRY: frozenset(
        {GmailMessageState.FETCHING, GmailMessageState.FETCH_FAILED}
    ),
    GmailMessageState.PARSED: frozenset({GmailMessageState.NOTIFICATION_PENDING}),
    GmailMessageState.NOTIFICATION_PENDING: frozenset(
        {GmailMessageState.DELIVERED, GmailMessageState.NOTIFICATION_RETRY}
    ),
    GmailMessageState.NOTIFICATION_RETRY: frozenset(
        {GmailMessageState.NOTIFICATION_PENDING, GmailMessageState.NOTIFICATION_DEAD_LETTER}
    ),
    GmailMessageState.FETCH_FAILED: frozenset(),
    GmailMessageState.PARSE_FAILED: frozenset({GmailMessageState.FETCHING}),
    GmailMessageState.DELIVERED: frozenset(),
    GmailMessageState.NOTIFICATION_DEAD_LETTER: frozenset(),
}

NOTIFICATION_TRANSITIONS: dict[str, frozenset[str]] = {
    NotificationState.PENDING: frozenset({NotificationState.SENDING}),
    NotificationState.SENDING: frozenset(
        {NotificationState.DELIVERED, NotificationState.RETRY, NotificationState.DEAD_LETTER}
    ),
    NotificationState.RETRY: frozenset({NotificationState.SENDING, NotificationState.DEAD_LETTER}),
    NotificationState.DELIVERED: frozenset(),
    NotificationState.DEAD_LETTER: frozenset(),
}


class StateTransitionError(ValueError):
    """状态机输入或状态转移不合法。"""


class InvalidStateTransitionError(StateTransitionError):
    """当前状态不允许转移到目标状态。"""


class StateEntityNotFoundError(StateTransitionError):
    """按主键查找状态实体失败。"""


@dataclass(frozen=True)
class StateChange:
    entity: str
    entity_id: str
    previous: str
    current: str


def validate_transition(
    entity: str,
    current: str,
    target: str,
    transitions: Mapping[str, frozenset[str]],
) -> None:
    """验证状态转移；重复写入同一状态是幂等操作。"""

    if current == target:
        return
    allowed = transitions.get(current)
    if allowed is None or target not in allowed:
        raise InvalidStateTransitionError(
            f"invalid {entity} state transition: {current!r} -> {target!r}"
        )


EntityT = TypeVar("EntityT")


async def _apply_transition(
    session: AsyncSession,
    entity: EntityT,
    *,
    entity_name: str,
    entity_id: UUID,
    target: str,
    transitions: Mapping[str, frozenset[str]],
    state_attribute: str = "state",
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    current = str(getattr(entity, state_attribute))
    validate_transition(entity_name, current, target, transitions)
    setattr(entity, state_attribute, target)
    await session.flush()
    change = StateChange(entity_name, str(entity_id), current, target)
    logger.info(
        "state_transition entity=%s entity_id=%s previous=%s current=%s metadata_keys=%s",
        change.entity,
        change.entity_id,
        change.previous,
        change.current,
        sorted(metadata.keys()) if metadata else [],
    )
    return change


async def transition_mailbox(
    session: AsyncSession,
    mailbox: Mailbox,
    target: MailboxStatus | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    return await _apply_transition(
        session,
        mailbox,
        entity_name="mailbox",
        entity_id=mailbox.id,
        target=str(target),
        transitions=MAILBOX_TRANSITIONS,
        state_attribute="status",
        metadata=metadata,
    )


async def transition_sync_state(
    session: AsyncSession,
    sync_state: GmailSyncState,
    target: SyncMode | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    return await _apply_transition(
        session,
        sync_state,
        entity_name="gmail_sync_state",
        entity_id=sync_state.mailbox_id,
        target=str(target),
        transitions=SYNC_TRANSITIONS,
        state_attribute="mode",
        metadata=metadata,
    )


async def transition_message(
    session: AsyncSession,
    message: GmailMessage,
    target: GmailMessageState | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    return await _apply_transition(
        session,
        message,
        entity_name="gmail_message",
        entity_id=message.id,
        target=str(target),
        transitions=MESSAGE_TRANSITIONS,
        metadata=metadata,
    )


async def transition_notification(
    session: AsyncSession,
    delivery: NotificationDelivery,
    target: NotificationState | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    return await _apply_transition(
        session,
        delivery,
        entity_name="notification_delivery",
        entity_id=delivery.id,
        target=str(target),
        transitions=NOTIFICATION_TRANSITIONS,
        metadata=metadata,
    )


async def transition_mailbox_by_id(
    session: AsyncSession,
    mailbox_id: UUID,
    target: MailboxStatus | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    result = await session.execute(
        select(Mailbox).where(Mailbox.id == mailbox_id).with_for_update()
    )
    mailbox = result.scalar_one_or_none()
    if mailbox is None:
        raise StateEntityNotFoundError(f"mailbox not found: {mailbox_id}")
    return await transition_mailbox(session, mailbox, target, metadata=metadata)


async def transition_sync_state_by_id(
    session: AsyncSession,
    mailbox_id: UUID,
    target: SyncMode | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    result = await session.execute(
        select(GmailSyncState).where(GmailSyncState.mailbox_id == mailbox_id).with_for_update()
    )
    sync_state = result.scalar_one_or_none()
    if sync_state is None:
        raise StateEntityNotFoundError(f"gmail sync state not found: {mailbox_id}")
    return await transition_sync_state(session, sync_state, target, metadata=metadata)


async def transition_message_by_id(
    session: AsyncSession,
    message_id: UUID,
    target: GmailMessageState | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    result = await session.execute(
        select(GmailMessage).where(GmailMessage.id == message_id).with_for_update()
    )
    message = result.scalar_one_or_none()
    if message is None:
        raise StateEntityNotFoundError(f"gmail message not found: {message_id}")
    return await transition_message(session, message, target, metadata=metadata)


async def transition_notification_by_id(
    session: AsyncSession,
    delivery_id: UUID,
    target: NotificationState | str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> StateChange:
    result = await session.execute(
        select(NotificationDelivery).where(NotificationDelivery.id == delivery_id).with_for_update()
    )
    delivery = result.scalar_one_or_none()
    if delivery is None:
        raise StateEntityNotFoundError(f"notification delivery not found: {delivery_id}")
    return await transition_notification(session, delivery, target, metadata=metadata)


__all__ = [
    "GmailMessageState",
    "InvalidStateTransitionError",
    "MAILBOX_TRANSITIONS",
    "MESSAGE_TRANSITIONS",
    "MailboxStatus",
    "NOTIFICATION_TRANSITIONS",
    "NotificationState",
    "StateChange",
    "StateEntityNotFoundError",
    "StateTransitionError",
    "SYNC_TRANSITIONS",
    "SyncMode",
    "transition_mailbox",
    "transition_mailbox_by_id",
    "transition_message",
    "transition_message_by_id",
    "transition_notification",
    "transition_notification_by_id",
    "transition_sync_state",
    "transition_sync_state_by_id",
    "validate_transition",
]
