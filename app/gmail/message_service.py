"""邮件完整内容获取、MIME 解析和事务内 Outbox 创建。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import GmailMessage, Mailbox, NotificationEndpoint
from app.gmail.client import GmailApiClient
from app.gmail.mime import AttachmentMetadata, ParsedEmail, parse_gmail_full
from app.notifications.delivery import NotificationOutboxService
from app.state_machine import GmailMessageState

logger = logging.getLogger(__name__)


class AttachmentStorage(Protocol):
    """兼容 S3/对象存储的最小异步接口；默认流程不启用二进制存储。"""

    async def put(self, key: str, data: bytes, *, content_type: str) -> str: ...


class MessageContentService:
    def __init__(
        self,
        client: GmailApiClient,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
    ) -> None:
        self.client = client
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.outbox = NotificationOutboxService(session_factory, self.settings)

    async def process_message_id(self, mailbox_id: UUID, message_id: str) -> ParsedEmail | None:
        async with self.session_factory() as session:
            async with session.begin():
                message = await session.scalar(
                    select(GmailMessage)
                    .where(
                        GmailMessage.mailbox_id == mailbox_id,
                        GmailMessage.gmail_message_id == message_id,
                    )
                    .with_for_update()
                )
                if message is None:
                    return None
                if message.state in {
                    GmailMessageState.FETCHING,
                    GmailMessageState.DELIVERED,
                    GmailMessageState.NOTIFICATION_PENDING,
                }:
                    return None
                message.state = GmailMessageState.FETCHING
                message.parse_attempt_count += 1
        scopes = {
            item.strip() for item in self.settings.google_oauth_scopes.split() if item.strip()
        }
        metadata_scope = "https://www.googleapis.com/auth/gmail.metadata"
        read_scope = "https://www.googleapis.com/auth/gmail.readonly"
        if metadata_scope in scopes and read_scope not in scopes:
            await self._mark_parse_failed(mailbox_id, message_id, "insufficient_gmail_scope")
            return None
        try:
            raw = await self.client.get_message(mailbox_id, message_id, format="full")
            parsed = parse_gmail_full(
                raw,
                max_body_chars=self.settings.message_body_max_chars,
                max_attachment_bytes=self.settings.attachment_max_bytes,
            )
        except Exception as exc:
            logger.warning(
                "gmail message parse failed mailbox=%s message_id=%s error=%s",
                mailbox_id,
                message_id,
                type(exc).__name__,
            )
            await self._mark_parse_failed(mailbox_id, message_id, type(exc).__name__)
            return None

        async with self.session_factory() as session:
            async with session.begin():
                message = await session.scalar(
                    select(GmailMessage)
                    .where(
                        GmailMessage.mailbox_id == mailbox_id,
                        GmailMessage.gmail_message_id == message_id,
                    )
                    .with_for_update()
                )
                mailbox = await session.get(Mailbox, mailbox_id)
                if message is None or mailbox is None:
                    return parsed
                self._apply_parsed(message, parsed)
                endpoint = await session.scalar(
                    select(NotificationEndpoint)
                    .where(NotificationEndpoint.enabled.is_(True))
                    .order_by(NotificationEndpoint.created_at)
                    .limit(1)
                )
                if (
                    endpoint is None
                    and self.settings.webhook_enabled
                    and self.settings.webhook_url
                    and self.settings.webhook_secret
                ):
                    endpoint = NotificationEndpoint(
                        name="configured-webhook",
                        url=self.settings.webhook_url,
                        # 实际 secret 只从 Settings 注入；此占位值避免把明文密钥写入数据库。
                        secret_ciphertext="CONFIGURED_SECRET",
                        enabled=True,
                        timeout_ms=int(self.settings.webhook_timeout * 1000),
                        max_attempts=self.settings.webhook_max_retries + 1,
                    )
                    session.add(endpoint)
                    await session.flush()
                if endpoint is not None and self.settings.webhook_enabled:
                    await self.outbox.create_for_message(session, message, mailbox, endpoint)
                else:
                    message.state = GmailMessageState.PARSED
        return parsed

    async def _mark_parse_failed(self, mailbox_id: UUID, message_id: str, error_code: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                message = await session.scalar(
                    select(GmailMessage)
                    .where(
                        GmailMessage.mailbox_id == mailbox_id,
                        GmailMessage.gmail_message_id == message_id,
                    )
                    .with_for_update()
                )
                if message is not None:
                    message.state = GmailMessageState.PARSE_FAILED
                    message.parse_error = error_code[:1000]
                    message.last_error_code = error_code[:64]

    def _apply_parsed(self, message: GmailMessage, parsed: ParsedEmail) -> None:
        message.thread_id = parsed.thread_id
        message.from_address = parsed.sender[0].email if parsed.sender else None
        message.to_addresses = [item.email for item in parsed.to] or None
        message.cc_addresses = [item.email for item in parsed.cc] or None
        message.reply_to_addresses = [item.email for item in parsed.reply_to] or None
        message.subject = parsed.subject
        message.body_text = parsed.text_body
        message.body_html = parsed.html_body
        message.internal_date = parsed.internal_date
        message.received_at = parsed.internal_date or parsed.date
        message.attachments = [self._attachment_dict(item) for item in parsed.attachments]
        message.attachment_count = len(parsed.attachments)
        message.parsed_at = datetime.now(UTC)
        message.parse_error = "body_truncated" if parsed.body_truncated else None
        message.last_error_code = None
        digest_input = "\x00".join(
            (parsed.subject or "", parsed.text_body or "", parsed.html_body or "")
        ).encode("utf-8", "replace")
        message.content_hash = hashlib.sha256(digest_input).hexdigest()
        message.state = GmailMessageState.PARSED

    @staticmethod
    def _attachment_dict(item: AttachmentMetadata) -> dict[str, Any]:
        return {
            "filename": item.filename,
            "mime_type": item.mime_type,
            "size": item.size,
            "attachment_id": item.attachment_id,
            "inline": item.inline,
            "content_id": item.content_id,
            "too_large": item.too_large,
            "download_state": "pending"
            if item.attachment_id and not item.too_large
            else "metadata_only",
        }

    async def download_attachment(
        self,
        mailbox_id: UUID,
        gmail_message_id: str,
        metadata: Mapping[str, Any],
        *,
        destination_dir: str,
    ) -> bytes | None:
        """按配置下载单个附件；不解压、不执行、不接受路径穿越。"""

        if not self.settings.attachment_download_enabled:
            return None
        attachment_id = str(metadata.get("attachment_id", "")).strip()
        if not attachment_id or bool(metadata.get("too_large")):
            return None
        filename = _safe_filename(str(metadata.get("filename", "attachment")))
        if not filename:
            filename = "attachment"
        response = await self.client.get_attachment(mailbox_id, gmail_message_id, attachment_id)
        encoded = response.get("data") if isinstance(response, dict) else None
        if not encoded:
            return b""
        from app.gmail.mime import decode_base64url

        data = decode_base64url(str(encoded))
        if len(data) > self.settings.attachment_max_bytes:
            return None
        # The caller decides whether and how to persist these bytes. We deliberately
        # return bytes only; this service never executes or extracts an attachment.
        _ = PurePosixPath(destination_dir) / filename
        return data


def _safe_filename(value: str) -> str:
    value = value.replace("\\", "/")
    value = PurePosixPath(value).name
    value = re.sub(r"[^\w. ()\-\u4e00-\u9fff]", "_", value, flags=re.UNICODE)
    return value[:255]


__all__ = ["AttachmentStorage", "MessageContentService"]
