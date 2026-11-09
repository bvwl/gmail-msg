"""IMAP UID 同步服务：消息实体和标签关系在同一事务内幂等写入。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.db.base import MailboxFolder, Message, MessageLabel, VerificationCode
from app.services.codes import extract_codes
from app.services.mail_parser import parse_rfc822
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


class ImapSyncService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        max_body_chars: int = 1_000_000,
    ) -> None:
        self.session_factory = session_factory
        self.max_body_chars = max_body_chars

    async def store_message(
        self,
        mailbox_id: UUID,
        folder_id: UUID,
        uid: int,
        uidvalidity: int,
        raw: bytes,
        *,
        x_gm_msgid: str | None = None,
        x_gm_thrid: str | None = None,
    ) -> Message:
        parsed = parse_rfc822(
            raw,
            x_gm_msgid=x_gm_msgid,
            x_gm_thrid=x_gm_thrid,
            max_body_chars=self.max_body_chars,
        )
        now = datetime.now(UTC)
        async with self.session_factory() as session:
            async with session.begin():
                message = (
                    await session.execute(
                        select(Message)
                        .where(
                            Message.mailbox_id == mailbox_id,
                            Message.stable_message_key == parsed.stable_key,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if message is None:
                    message = Message(mailbox_id=mailbox_id, stable_message_key=parsed.stable_key)
                    session.add(message)
                message.message_id_header = parsed.message_id
                message.thread_key = parsed.thread_key
                message.subject = parsed.subject
                message.from_address = parsed.from_address
                message.to_addresses = parsed.to_addresses
                message.cc_addresses = parsed.cc_addresses
                message.received_at = parsed.received_at
                message.text_body = parsed.text_body
                message.html_body_sanitized = parsed.html_body
                message.has_attachments = parsed.has_attachments
                message.body_hash = parsed.body_hash
                message.parser_state = "parsed"
                message.updated_at = now
                label = (
                    await session.execute(
                        select(MessageLabel)
                        .where(
                            MessageLabel.message_id == message.id,
                            MessageLabel.mailbox_folder_id == folder_id,
                        )
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if label is None:
                    label = MessageLabel(
                        message_id=message.id,
                        mailbox_folder_id=folder_id,
                        folder_uid=uid,
                        uidvalidity_snapshot=uidvalidity,
                    )
                    session.add(label)
                else:
                    label.folder_uid = uid
                    label.uidvalidity_snapshot = uidvalidity
                    label.last_seen_at = now
                    label.is_present = True
                existing_codes = {
                    row[0]
                    for row in (
                        await session.execute(
                            select(VerificationCode.code_value).where(
                                VerificationCode.message_id == message.id
                            )
                        )
                    ).all()
                }
                for candidate in extract_codes(parsed.subject, parsed.text_body, parsed.html_body):
                    if candidate.value not in existing_codes:
                        session.add(
                            VerificationCode(
                                message_id=message.id,
                                code_value=candidate.value,
                                code_type=candidate.code_type,
                                confidence=candidate.confidence,
                            )
                        )
                return message

    async def advance_cursor(
        self, folder_id: UUID, uidvalidity: int, uidnext: int, last_seen_uid: int
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                folder = await session.get(MailboxFolder, folder_id, with_for_update=True)
                if folder is None:
                    return
                folder.uidvalidity = uidvalidity
                folder.uidnext = uidnext
                folder.last_seen_uid = last_seen_uid
                folder.sync_state = "idle"
                folder.last_success_at = datetime.now(UTC)
                folder.worker_heartbeat_at = datetime.now(UTC)
