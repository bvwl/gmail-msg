"""Gmail history 增量同步、基线和游标恢复。"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import GmailMessage, GmailSyncState, Mailbox
from app.db.transaction import mailbox_transaction
from app.gmail.client import GmailApiClient, GmailApiNotFoundError
from app.state_machine import GmailMessageState, SyncMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncResult:
    mailbox_id: UUID
    mode: str
    fetched_history_pages: int
    discovered_messages: int
    saved_messages: int
    skipped_messages: int
    cursor_history_id: str | None
    recovery: bool = False


class GmailSyncService:
    """在邮箱级事务锁内完成 API 读取、消息幂等保存和游标推进。"""

    def __init__(
        self,
        client: GmailApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def sync_mailbox(
        self,
        mailbox_id: UUID,
        *,
        observed_history_id: str | None = None,
        force_recovery: bool = False,
    ) -> SyncResult:
        async with mailbox_transaction(self.session_factory, mailbox_id) as session:
            mailbox = await session.scalar(
                select(Mailbox).where(Mailbox.id == mailbox_id).with_for_update()
            )
            if mailbox is None:
                raise ValueError(f"mailbox not found: {mailbox_id}")
            state = await session.scalar(
                select(GmailSyncState)
                .where(GmailSyncState.mailbox_id == mailbox_id)
                .with_for_update()
            )
            if state is None:
                state = GmailSyncState(mailbox_id=mailbox_id, mode=SyncMode.BASELINE_REQUIRED)
                session.add(state)
                await session.flush()

            if observed_history_id and self._is_newer(
                observed_history_id, state.last_observed_history_id
            ):
                state.last_observed_history_id = observed_history_id

            if force_recovery or str(state.mode) == SyncMode.FULL_RECOVERY_REQUIRED:
                return await self._full_recovery(session, mailbox, state)
            if str(state.mode) == SyncMode.BASELINE_REQUIRED or not state.cursor_history_id:
                if self.settings.gmail_sync_import_history_on_baseline:
                    return await self._full_recovery(session, mailbox, state, baseline=True)
                baseline = state.last_observed_history_id or observed_history_id
                if baseline is None:
                    profile = await self.client.get_profile(mailbox_id)
                    baseline = str(profile.get("historyId", "")).strip() or None
                if baseline is None:
                    raise RuntimeError("Gmail profile did not return historyId for baseline")
                state.baseline_history_id = baseline
                state.cursor_history_id = baseline
                state.mode = SyncMode.INCREMENTAL
                state.recovery_completed_at = None
                return SyncResult(mailbox_id, "baseline", 0, 0, 0, 0, baseline)

            start = state.cursor_history_id
            pending_saved = await self._retry_pending_messages(session, mailbox_id)
            try:
                pages, message_ids, final_history_id = await self._collect_history(
                    mailbox_id, start
                )
            except GmailApiNotFoundError:
                state.mode = SyncMode.FULL_RECOVERY_REQUIRED
                state.recovery_started_at = datetime.now(UTC)
                return await self._full_recovery(session, mailbox, state)

            saved, skipped = await self._save_message_ids(session, mailbox_id, message_ids)
            saved += pending_saved
            if final_history_id and self._is_newer(final_history_id, state.cursor_history_id):
                state.cursor_history_id = final_history_id
            state.mode = SyncMode.INCREMENTAL
            state.consecutive_failures = 0
            mailbox.last_sync_at = datetime.now(UTC)
            return SyncResult(
                mailbox_id,
                "incremental",
                pages,
                len(message_ids),
                saved,
                skipped,
                state.cursor_history_id,
            )

    async def _retry_pending_messages(self, session: AsyncSession, mailbox_id: UUID) -> int:
        """在补偿同步中重试此前已发现但 messages.get 暂时失败的邮件。"""

        rows = await session.scalars(
            select(GmailMessage)
            .where(
                GmailMessage.mailbox_id == mailbox_id,
                GmailMessage.state.in_(
                    [
                        GmailMessageState.DISCOVERED,
                        GmailMessageState.FETCH_RETRY,
                    ]
                ),
            )
            .order_by(GmailMessage.created_at)
            .limit(self.settings.gmail_sync_recovery_page_size)
            .with_for_update()
        )
        all_rows = rows.all() if hasattr(rows, "all") else list(rows)
        saved = 0
        for entity in all_rows:
            try:
                payload = await self.client.get_message(
                    mailbox_id, entity.gmail_message_id, format="metadata"
                )
            except Exception:
                entity.fetch_attempt_count += 1
                continue
            self._apply_message_payload(entity, payload)
            saved += 1
        await session.flush()
        return saved

    async def _collect_history(
        self, mailbox_id: UUID, start_history_id: str
    ) -> tuple[int, list[str], str | None]:
        token: str | None = None
        pages = 0
        final_history_id: str | None = None
        message_ids: list[str] = []
        seen: set[str] = set()
        while True:
            page = await self.client.list_history(
                mailbox_id,
                start_history_id=start_history_id,
                page_token=token,
                history_types=["messageAdded"],
                max_results=self.settings.gmail_sync_recovery_page_size,
            )
            pages += 1
            raw = page.raw or {}
            candidate = raw.get("historyId")
            if candidate:
                final_history_id = str(candidate)
            for item in page.items:
                for event in item.get("messagesAdded", []) or []:
                    message = event.get("message", {}) if isinstance(event, dict) else {}
                    message_id = (
                        str(message.get("id", "")).strip() if isinstance(message, dict) else ""
                    )
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        message_ids.append(message_id)
            token = page.next_page_token
            if not token:
                break
        return pages, message_ids, final_history_id

    async def _save_message_ids(
        self, session: AsyncSession, mailbox_id: UUID, message_ids: Iterable[str]
    ) -> tuple[int, int]:
        saved = 0
        skipped = 0
        for message_id in message_ids:
            existing = await session.scalar(
                select(GmailMessage)
                .where(
                    GmailMessage.mailbox_id == mailbox_id,
                    GmailMessage.gmail_message_id == message_id,
                )
                .with_for_update()
            )
            if existing is not None and str(existing.state) in {
                GmailMessageState.PARSED,
                GmailMessageState.NOTIFICATION_PENDING,
                GmailMessageState.DELIVERED,
            }:
                skipped += 1
                continue
            try:
                payload = await self.client.get_message(mailbox_id, message_id, format="metadata")
            except GmailApiNotFoundError:
                skipped += 1
                continue
            except Exception as exc:
                logger.warning(
                    "gmail message fetch failed mailbox=%s message_id=%s",
                    mailbox_id,
                    message_id,
                )
                if existing is None:
                    session.add(
                        GmailMessage(
                            mailbox_id=mailbox_id,
                            gmail_message_id=message_id,
                            state=GmailMessageState.DISCOVERED,
                            last_error_code=self._error_code(exc),
                        )
                    )
                else:
                    existing.state = GmailMessageState.FETCH_RETRY
                    existing.last_error_code = self._error_code(exc)
                skipped += 1
                continue
            if existing is None:
                existing = GmailMessage(
                    mailbox_id=mailbox_id,
                    gmail_message_id=message_id,
                )
                session.add(existing)
            self._apply_message_payload(existing, payload)
            saved += 1
        await session.flush()
        return saved, skipped

    async def _full_recovery(
        self,
        session: AsyncSession,
        mailbox: Mailbox,
        state: GmailSyncState,
        *,
        baseline: bool = False,
    ) -> SyncResult:
        state.mode = SyncMode.FULL_RECOVERY_REQUIRED
        if state.recovery_started_at is None:
            state.recovery_started_at = datetime.now(UTC)
        items = await self.client.list_all_messages(
            mailbox.id,
            max_results=self.settings.gmail_sync_recovery_page_size,
        )
        ids = []
        seen: set[str] = set()
        for item in items:
            message_id = str(item.get("id", "")).strip()
            if message_id and message_id not in seen:
                seen.add(message_id)
                ids.append(message_id)
        saved, skipped = await self._save_message_ids(session, mailbox.id, ids)
        profile = await self.client.get_profile(mailbox.id)
        cursor = str(profile.get("historyId", "")).strip() or state.last_observed_history_id
        if cursor:
            state.cursor_history_id = cursor
            state.last_observed_history_id = cursor
            if state.baseline_history_id is None:
                state.baseline_history_id = cursor
        state.mode = SyncMode.INCREMENTAL
        state.recovery_completed_at = datetime.now(UTC)
        state.consecutive_failures = 0
        mailbox.last_sync_at = datetime.now(UTC)
        return SyncResult(
            mailbox.id,
            "baseline" if baseline else "full_recovery",
            0,
            len(ids),
            saved,
            skipped,
            state.cursor_history_id,
            recovery=not baseline,
        )

    @staticmethod
    def _apply_message_payload(entity: GmailMessage, payload: dict[str, Any]) -> None:
        entity.thread_id = str(payload.get("threadId", "")).strip() or None
        entity.history_id = str(payload.get("historyId", "")).strip() or None
        labels = payload.get("labelIds")
        entity.label_ids = [str(item) for item in labels] if isinstance(labels, list) else None
        headers: dict[str, str] = {}
        payload_part = payload.get("payload")
        headers_list = payload_part.get("headers", []) if isinstance(payload_part, dict) else []
        for header in headers_list:
            if isinstance(header, dict) and header.get("name"):
                headers[str(header["name"]).lower()] = str(header.get("value", ""))
        entity.raw_headers = headers or None
        entity.from_address = headers.get("from")
        entity.to_addresses = [
            part.strip() for part in headers.get("to", "").split(",") if part.strip()
        ] or None
        entity.cc_addresses = [
            part.strip() for part in headers.get("cc", "").split(",") if part.strip()
        ] or None
        entity.reply_to_addresses = [
            part.strip() for part in headers.get("reply-to", "").split(",") if part.strip()
        ] or None
        entity.subject = headers.get("subject")
        internal_date = payload.get("internalDate")
        if internal_date:
            try:
                entity.received_at = datetime.fromtimestamp(int(internal_date) / 1000, tz=UTC)
                entity.internal_date = entity.received_at
            except (TypeError, ValueError, OSError):
                pass
        # metadata 只证明 Message ID 和头字段已落库，正文仍需由 Message
        # Worker 以 format=full 获取和解析；因此不能在此处提前标记 parsed。
        if str(entity.state) not in {
            GmailMessageState.PARSED,
            GmailMessageState.NOTIFICATION_PENDING,
            GmailMessageState.DELIVERED,
        }:
            entity.state = GmailMessageState.DISCOVERED
        entity.last_error_code = None

    @staticmethod
    def _is_newer(candidate: str | None, current: str | None) -> bool:
        if not candidate:
            return False
        if not current:
            return True
        try:
            return int(candidate) > int(current)
        except ValueError:
            return candidate != current

    @staticmethod
    def _error_code(exc: Exception) -> str:
        return f"{type(exc).__name__}"[:64]


__all__ = ["GmailSyncService", "SyncResult"]
