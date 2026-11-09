"""业务 Webhook 投递和 PostgreSQL Transactional Outbox 辅助。"""

from __future__ import annotations

import asyncio
import hashlib
import random
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings, get_settings
from app.db.base import GmailMessage, Mailbox, NotificationDelivery, NotificationEndpoint
from app.notifications.signing import sign_payload
from app.state_machine import GmailMessageState, NotificationState


@dataclass(frozen=True)
class DeliveryResult:
    accepted: bool
    retryable: bool
    status_code: int | None
    error_code: str | None = None
    retry_after_seconds: float | None = None
    response_hash: str | None = None


class WebhookSender:
    """不保存响应正文的异步 HTTP 投递器。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        http_client: Any | None = None,
        sleep: Any = asyncio.sleep,
        random_fn: Any = random.random,
    ) -> None:
        self.settings = settings or get_settings()
        self.http_client = http_client
        self.sleep = sleep
        self.random_fn = random_fn

    async def send(
        self,
        payload: Mapping[str, Any],
        *,
        url: str,
        secret: str,
        event_id: str,
        idempotency_key: str,
        event_type: str = "gmail.message.received",
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> DeliveryResult:
        retries = self.settings.webhook_max_retries if max_retries is None else max_retries
        timeout_seconds = self.settings.webhook_timeout if timeout is None else timeout
        own_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=timeout_seconds)
        try:
            for attempt in range(retries + 1):
                body, timestamp, signature = sign_payload(secret, payload)
                headers = {
                    "Content-Type": "application/json",
                    "X-Event-Type": event_type,
                    "X-Event-Id": event_id,
                    "Idempotency-Key": idempotency_key,
                    "X-Webhook-Timestamp": str(timestamp),
                    "X-Webhook-Signature": signature,
                    "X-Request-ID": event_id,
                }
                try:
                    response = await client.post(url, content=body, headers=headers)
                except (
                    TimeoutError,
                    httpx.TimeoutException,
                    httpx.NetworkError,
                    httpx.RemoteProtocolError,
                ):
                    if attempt >= retries:
                        return DeliveryResult(False, True, None, "network_error")
                    await self._backoff(attempt)
                    continue
                status = int(getattr(response, "status_code", 0))
                if 200 <= status < 300 or status == 409:
                    return DeliveryResult(
                        True, False, status, response_hash=_response_hash(response)
                    )
                retryable = status in {408, 425, 429} or 500 <= status <= 599
                if not retryable:
                    return DeliveryResult(
                        False,
                        False,
                        status,
                        f"http_{status}",
                        response_hash=_response_hash(response),
                    )
                retry_after = _retry_after(response)
                if attempt >= retries:
                    return DeliveryResult(
                        False, True, status, f"http_{status}", retry_after, _response_hash(response)
                    )
                if retry_after is None:
                    await self._backoff(attempt)
                else:
                    await self.sleep(min(retry_after, self.settings.webhook_retry_base_delay * 64))
            return DeliveryResult(False, True, None, "retry_exhausted")
        finally:
            if own_client:
                await client.aclose()

    async def _backoff(self, attempt: int) -> None:
        base = self.settings.webhook_retry_base_delay
        delay = min(base * (2**attempt), base * 64)
        await self.sleep(delay * (0.5 + float(self.random_fn())))


def _retry_after(response: Any) -> float | None:
    value = None
    headers = getattr(response, "headers", None)
    if headers is not None:
        value = headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return max(0.0, (parsed - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


def _response_hash(response: Any) -> str | None:
    try:
        content = getattr(response, "content", b"")
        if isinstance(content, str):
            content = content.encode("utf-8", "replace")
        if not isinstance(content, bytes):
            content = b""
        return hashlib.sha256(content[:4096]).hexdigest()
    except Exception:
        return None


def _address_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def build_notification_payload(
    message: GmailMessage,
    mailbox: Mailbox,
    delivery: NotificationDelivery,
) -> dict[str, Any]:
    """构造持久化 Outbox 使用的协议 payload。"""

    received = message.received_at or datetime.now(UTC)
    payload: dict[str, Any] = {
        "event_id": str(delivery.id),
        "event_type": delivery.event_type,
        "schema_version": delivery.schema_version,
        "idempotency_key": delivery.idempotency_key,
        "mailbox": mailbox.email,
        "gmail_account": mailbox.email,
        "message_id": message.gmail_message_id,
        "gmail_message_id": message.gmail_message_id,
        "thread_id": message.thread_id,
        "from": message.from_address,
        "to": _address_values(message.to_addresses),
        "cc": _address_values(message.cc_addresses),
        "reply_to": _address_values(message.reply_to_addresses),
        "subject": message.subject,
        "text_body": message.body_text,
        "html_body": message.body_html,
        "has_attachments": bool(message.attachment_count or message.attachments),
        "attachments": message.attachments or [],
        "received_at": received.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "timestamp": int(received.timestamp()),
    }
    return payload


class NotificationOutboxService:
    """在邮件解析事务中创建稳定、可恢复的通知事件。"""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], settings: Settings | None = None
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()

    async def create_for_message(
        self,
        session: AsyncSession,
        message: GmailMessage,
        mailbox: Mailbox,
        endpoint: NotificationEndpoint,
        *,
        event_type: str = "gmail.message.received",
    ) -> NotificationDelivery:
        key = f"gmail:{message.mailbox_id}:{message.gmail_message_id}:v1"
        existing = await session.scalar(
            select(NotificationDelivery)
            .where(NotificationDelivery.idempotency_key == key)
            .with_for_update()
        )
        if existing is not None:
            return existing
        delivery = NotificationDelivery(
            message_id=message.id,
            endpoint_id=endpoint.id,
            event_type=event_type,
            idempotency_key=key,
            state=NotificationState.PENDING,
        )
        session.add(delivery)
        await session.flush()
        delivery.payload_json = build_notification_payload(message, mailbox, delivery)
        message.state = GmailMessageState.NOTIFICATION_PENDING
        await session.flush()
        return delivery


class NotificationDeliveryService:
    """从 PostgreSQL 扫描并投递到期 Outbox 任务。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings | None = None,
        *,
        sender: WebhookSender | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.sender = sender or WebhookSender(self.settings)

    async def deliver(self, delivery_id: UUID) -> DeliveryResult:
        async with self.session_factory() as session:
            async with session.begin():
                delivery = await session.scalar(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update()
                )
                if delivery is None:
                    return DeliveryResult(False, False, None, "delivery_not_found")
                message = await session.get(GmailMessage, delivery.message_id)
                endpoint = await session.get(NotificationEndpoint, delivery.endpoint_id)
                mailbox = (
                    await session.scalar(select(Mailbox).where(Mailbox.id == message.mailbox_id))
                    if message
                    else None
                )
                if message is None or endpoint is None or mailbox is None:
                    delivery.state = NotificationState.DEAD_LETTER
                    delivery.last_error_code = "missing_reference"
                    return DeliveryResult(False, False, None, "missing_reference")
                if delivery.state in {NotificationState.DELIVERED, NotificationState.DEAD_LETTER}:
                    return DeliveryResult(
                        delivery.state == NotificationState.DELIVERED,
                        False,
                        delivery.last_http_status,
                    )
                if delivery.state == NotificationState.SENDING:
                    return DeliveryResult(False, True, None, "already_sending")
                delivery.state = NotificationState.SENDING
                delivery.attempt_count += 1
                attempt_number = delivery.attempt_count
                payload = delivery.payload_json or build_notification_payload(
                    message, mailbox, delivery
                )
                url = endpoint.url
                secret = (
                    self.settings.webhook_secret.get_secret_value()
                    if self.settings.webhook_secret
                    else endpoint.secret_ciphertext
                )
                event_type = delivery.event_type
                key = delivery.idempotency_key
                event_id = str(delivery.id)
                max_attempts = endpoint.max_attempts
            try:
                result = await self.sender.send(
                    payload,
                    url=url,
                    secret=secret,
                    event_id=event_id,
                    idempotency_key=key,
                    event_type=event_type,
                    timeout=endpoint.timeout_ms / 1000,
                    max_retries=max(0, endpoint.max_attempts - 1),
                )
            except Exception:
                # 外部调用或 Worker 被取消时，第二次扫描仍能接管 sending 任务。
                result = DeliveryResult(False, True, None, "worker_delivery_error")
        async with self.session_factory() as session:
            async with session.begin():
                delivery = await session.scalar(
                    select(NotificationDelivery)
                    .where(
                        NotificationDelivery.id == delivery_id,
                        NotificationDelivery.state == NotificationState.SENDING,
                        NotificationDelivery.attempt_count == attempt_number,
                    )
                    .with_for_update()
                )
                if delivery is None:
                    return result
                delivery.last_http_status = result.status_code
                delivery.last_error_code = result.error_code
                delivery.last_response_hash = result.response_hash
                if result.accepted:
                    delivery.state = NotificationState.DELIVERED
                    delivery.delivered_at = datetime.now(UTC)
                    message = await session.get(
                        GmailMessage, delivery.message_id, with_for_update=True
                    )
                    if message is not None:
                        message.state = GmailMessageState.DELIVERED
                    delivery.next_attempt_at = None
                elif result.retryable and delivery.attempt_count < max(1, max_attempts):
                    delivery.state = NotificationState.RETRY
                    delay = result.retry_after_seconds or min(
                        self.settings.webhook_retry_base_delay
                        * (2 ** max(0, delivery.attempt_count - 1)),
                        3600,
                    )
                    delivery.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
                else:
                    delivery.state = NotificationState.DEAD_LETTER
                    delivery.next_attempt_at = None
        return result

    async def redeliver(self, delivery_id: UUID) -> bool:
        """人工将 dead_letter 任务重新放回待发送队列。"""

        async with self.session_factory() as session:
            async with session.begin():
                delivery = await session.scalar(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery_id)
                    .with_for_update()
                )
                if delivery is None:
                    return False
                if delivery.state != NotificationState.DEAD_LETTER:
                    return False
                delivery.state = NotificationState.RETRY
                delivery.next_attempt_at = datetime.now(UTC)
                delivery.last_error_code = None
                return True

    async def backlog_count(self) -> int:
        """返回尚未完成的通知数量，供健康/运维接口使用。"""

        from sqlalchemy import func

        async with self.session_factory() as session:
            value = await session.scalar(
                select(func.count(NotificationDelivery.id)).where(
                    NotificationDelivery.state.in_(
                        [
                            NotificationState.PENDING,
                            NotificationState.SENDING,
                            NotificationState.RETRY,
                        ]
                    )
                )
            )
            return int(value or 0)


__all__ = [
    "DeliveryResult",
    "NotificationDeliveryService",
    "NotificationOutboxService",
    "WebhookSender",
    "build_notification_payload",
    "NotificationSender",
]

NotificationSender = WebhookSender
