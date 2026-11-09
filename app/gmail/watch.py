"""Gmail Watch 注册、基线和续订管理。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import GmailSyncState, Mailbox
from app.db.transaction import acquire_mailbox_advisory_lock
from app.gmail.client import GmailApiError
from app.state_machine import MailboxStatus, SyncMode, transition_mailbox, transition_sync_state


class GmailWatchClientProtocol(Protocol):
    async def watch(
        self,
        mailbox_id: UUID,
        *,
        topic_name: str,
        user_id: str = "me",
        label_ids: list[str] | None = None,
        label_filter_behavior: str | None = None,
    ) -> dict[str, Any]: ...

    async def stop(self, mailbox_id: UUID, *, user_id: str = "me") -> dict[str, Any]: ...


@dataclass(frozen=True)
class WatchRegistration:
    history_id: str
    expiration_at: datetime
    topic_name: str
    label_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class WatchStatus:
    mailbox_id: UUID
    topic_name: str | None
    expiration_at: datetime | None
    last_watch_at: datetime | None
    mailbox_status: str
    baseline_history_id: str | None
    cursor_history_id: str | None
    sync_mode: str | None


class GmailWatchManager:
    def __init__(
        self,
        client: GmailWatchClientProtocol,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep

    def _topic(self, topic_name: str | None) -> str:
        topic = topic_name or self.settings.gmail_watch_topic
        if not topic:
            raise ValueError("Gmail Watch topic is not configured")
        if not topic.startswith("projects/") or "/topics/" not in topic:
            raise ValueError(
                "Gmail Watch topic must be a full projects/{project}/topics/{topic} name"
            )
        return topic

    def _labels(self, label_ids: list[str] | None) -> list[str] | None:
        if label_ids is not None:
            return label_ids or None
        values = [
            value.strip()
            for value in self.settings.gmail_watch_label_ids.split(",")
            if value.strip()
        ]
        return values or None

    async def register(
        self,
        mailbox_id: UUID,
        *,
        topic_name: str | None = None,
        label_ids: list[str] | None = None,
        label_filter_behavior: str | None = None,
    ) -> WatchRegistration:
        topic = self._topic(topic_name)
        response = await self._register_with_retry(
            mailbox_id,
            topic_name=topic,
            label_ids=self._labels(label_ids),
            label_filter_behavior=label_filter_behavior,
        )
        registration = self._parse_registration(response, topic, self._labels(label_ids))
        async with self.session_factory() as session:
            async with session.begin():
                mailbox = await session.scalar(
                    select(Mailbox).where(Mailbox.id == mailbox_id).with_for_update()
                )
                if mailbox is None:
                    raise ValueError(f"mailbox not found: {mailbox_id}")
                await self._persist_registration(session, mailbox, registration)
        return registration

    async def renew(self, mailbox_id: UUID, **kwargs: Any) -> WatchRegistration:
        """在邮箱事务锁内重新检查到期状态，防止多个实例重复续订。"""

        topic = self._topic(kwargs.pop("topic_name", None))
        label_ids = self._labels(kwargs.pop("label_ids", None))
        async with self.session_factory() as session:
            async with session.begin():
                await acquire_mailbox_advisory_lock(session, mailbox_id)
                mailbox = await session.scalar(
                    select(Mailbox).where(Mailbox.id == mailbox_id).with_for_update()
                )
                if mailbox is None:
                    raise ValueError(f"mailbox not found: {mailbox_id}")
                now = self.clock()
                if (
                    mailbox.watch_expiration_at is not None
                    and mailbox.watch_expiration_at
                    > now + timedelta(seconds=self.settings.gmail_watch_renew_before_seconds)
                ):
                    return self._status_registration(mailbox, topic, label_ids)
                response = await self._register_with_retry(
                    mailbox_id,
                    topic_name=topic,
                    label_ids=label_ids,
                    label_filter_behavior=kwargs.pop("label_filter_behavior", None),
                )
                registration = self._parse_registration(response, topic, label_ids)
                await self._persist_registration(session, mailbox, registration)
                return registration

    async def renew_due(self, *, limit: int = 100, topic_name: str | None = None) -> int:
        now = self.clock()
        threshold = now + timedelta(seconds=self.settings.gmail_watch_renew_before_seconds)
        async with self.session_factory() as session:
            result = await session.execute(
                select(Mailbox.id)
                .where(
                    Mailbox.watch_expiration_at.is_(None)
                    | (Mailbox.watch_expiration_at <= threshold)
                )
                .where(
                    Mailbox.status.in_(
                        [
                            MailboxStatus.AUTHORIZED,
                            MailboxStatus.WATCH_ACTIVE,
                            MailboxStatus.DEGRADED,
                        ]
                    )
                )
                .limit(limit)
            )
            mailbox_ids = list(result.scalars())
        renewed = 0
        for mailbox_id in mailbox_ids:
            try:
                await self.renew(mailbox_id, topic_name=topic_name)
            except Exception:
                continue
            renewed += 1
        return renewed

    async def stop(self, mailbox_id: UUID) -> None:
        await self.client.stop(mailbox_id)
        async with self.session_factory() as session:
            async with session.begin():
                mailbox = await session.scalar(
                    select(Mailbox).where(Mailbox.id == mailbox_id).with_for_update()
                )
                if mailbox is None:
                    raise ValueError(f"mailbox not found: {mailbox_id}")
                mailbox.watch_expiration_at = None
                if mailbox.status == MailboxStatus.WATCH_ACTIVE:
                    await transition_mailbox(session, mailbox, MailboxStatus.AUTHORIZED)

    async def get_status(self, mailbox_id: UUID) -> WatchStatus:
        async with self.session_factory() as session:
            mailbox = await session.scalar(select(Mailbox).where(Mailbox.id == mailbox_id))
            if mailbox is None:
                raise ValueError(f"mailbox not found: {mailbox_id}")
            sync_state = await session.scalar(
                select(GmailSyncState).where(GmailSyncState.mailbox_id == mailbox_id)
            )
            return WatchStatus(
                mailbox_id=mailbox_id,
                topic_name=mailbox.watch_topic,
                expiration_at=mailbox.watch_expiration_at,
                last_watch_at=mailbox.last_watch_at,
                mailbox_status=str(mailbox.status),
                baseline_history_id=sync_state.baseline_history_id if sync_state else None,
                cursor_history_id=sync_state.cursor_history_id if sync_state else None,
                sync_mode=str(sync_state.mode) if sync_state else None,
            )

    async def _register_with_retry(self, mailbox_id: UUID, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(self.settings.gmail_watch_retry_max_attempts):
            try:
                return await self.client.watch(mailbox_id, **kwargs)
            except Exception as exc:
                last_error = exc
                if isinstance(exc, GmailApiError) and not exc.retryable:
                    raise
                if attempt + 1 >= self.settings.gmail_watch_retry_max_attempts:
                    break
                await self.sleep(
                    min(
                        self.settings.gmail_retry_max_seconds,
                        self.settings.gmail_retry_base_seconds * (2**attempt),
                    )
                )
        assert last_error is not None
        raise last_error

    @staticmethod
    def _parse_registration(
        payload: dict[str, Any], topic: str, label_ids: list[str] | None
    ) -> WatchRegistration:
        history_id = str(payload.get("historyId", "")).strip()
        if not history_id:
            raise ValueError("Gmail Watch response missing historyId")
        raw_expiration = payload.get("expiration")
        if raw_expiration is None:
            raise ValueError("Gmail Watch response missing expiration")
        try:
            expiration_at = datetime.fromtimestamp(int(raw_expiration) / 1000, tz=UTC)
        except (TypeError, ValueError, OverflowError, OSError) as exc:
            raise ValueError("Gmail Watch response has invalid expiration") from exc
        return WatchRegistration(
            history_id=history_id,
            expiration_at=expiration_at,
            topic_name=topic,
            label_ids=tuple(label_ids or ()),
        )

    async def _persist_registration(
        self, session: AsyncSession, mailbox: Mailbox, registration: WatchRegistration
    ) -> None:
        now = self.clock()
        mailbox.watch_topic = registration.topic_name
        mailbox.watch_subscription = self.settings.pubsub_subscription
        mailbox.watch_expiration_at = registration.expiration_at
        mailbox.last_watch_at = now
        mailbox.last_error_code = None
        if str(mailbox.status) in {MailboxStatus.AUTHORIZED, MailboxStatus.DEGRADED}:
            await transition_mailbox(session, mailbox, MailboxStatus.WATCH_ACTIVE)
        sync_state = await session.scalar(
            select(GmailSyncState).where(GmailSyncState.mailbox_id == mailbox.id).with_for_update()
        )
        if sync_state is None:
            sync_state = GmailSyncState(
                mailbox_id=mailbox.id,
                cursor_history_id=registration.history_id,
                baseline_history_id=registration.history_id,
                last_observed_history_id=registration.history_id,
                mode=SyncMode.INCREMENTAL,
            )
            session.add(sync_state)
        else:
            if self._history_is_newer(registration.history_id, sync_state.last_observed_history_id):
                sync_state.last_observed_history_id = registration.history_id
            if sync_state.cursor_history_id is None and sync_state.baseline_history_id is None:
                sync_state.cursor_history_id = registration.history_id
                sync_state.baseline_history_id = registration.history_id
                if str(sync_state.mode) == SyncMode.BASELINE_REQUIRED:
                    await transition_sync_state(session, sync_state, SyncMode.INCREMENTAL)

    @staticmethod
    def _history_is_newer(candidate: str, current: str | None) -> bool:
        if current is None:
            return True
        try:
            return int(candidate) > int(current)
        except ValueError:
            return candidate != current

    @staticmethod
    def _status_registration(
        mailbox: Mailbox, topic: str, label_ids: list[str] | None
    ) -> WatchRegistration:
        if mailbox.watch_expiration_at is None:
            raise ValueError("watch expiration is missing")
        return WatchRegistration(
            history_id="",
            expiration_at=mailbox.watch_expiration_at,
            topic_name=topic,
            label_ids=tuple(label_ids or ()),
        )


__all__ = ["GmailWatchManager", "WatchRegistration", "WatchStatus"]
