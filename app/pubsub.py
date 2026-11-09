"""Google Pub/Sub Push 验证、解析和可靠入队。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, cast
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import InboundEvent, Mailbox, SyncRequest
from app.redis_client import RedisManager


class PubSubPayloadError(ValueError):
    """Push 消息结构或业务数据无效。"""


class UnknownMailboxError(PubSubPayloadError):
    """推送邮箱尚未接入。"""


class PubSubAuthenticationError(PubSubPayloadError):
    """OIDC 身份验证失败。"""


@dataclass(frozen=True)
class PubSubNotification:
    message_id: str
    subscription: str
    email_address: str
    history_id: str
    publish_time: datetime | None
    payload_hash: str


def verify_google_oidc_token(
    token: str,
    *,
    audience: str,
    service_account: str,
) -> Mapping[str, Any]:
    """通过 Google 公钥验证 OIDC JWT，并校验发行者、受众和服务账号。"""

    try:
        google_auth_requests = cast(Any, import_module("google.auth.transport.requests"))
        id_token = cast(Any, import_module("google.oauth2.id_token"))
        google_auth_request = google_auth_requests.Request()

        claims = cast(
            Mapping[str, Any],
            id_token.verify_oauth2_token(token, google_auth_request, audience),
        )
    except Exception as exc:
        raise PubSubAuthenticationError("invalid OIDC token") from exc
    issuer = str(claims.get("iss", ""))
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        raise PubSubAuthenticationError("invalid OIDC issuer")
    if str(claims.get("aud", "")) != audience:
        raise PubSubAuthenticationError("invalid OIDC audience")
    exp = claims.get("exp")
    if not isinstance(exp, int | float) or exp <= time.time():
        raise PubSubAuthenticationError("OIDC token is expired")
    if str(claims.get("email", "")) != service_account:
        raise PubSubAuthenticationError("invalid OIDC service account")
    if claims.get("email_verified") is False:
        raise PubSubAuthenticationError("OIDC service account is not verified")
    return claims


def parse_pubsub_push(payload: Mapping[str, Any]) -> PubSubNotification:
    message = payload.get("message")
    subscription = payload.get("subscription")
    if not isinstance(message, Mapping) or not isinstance(subscription, str) or not subscription:
        raise PubSubPayloadError("invalid Pub/Sub envelope")
    message_id = str(message.get("messageId", "")).strip()
    encoded = message.get("data")
    if not message_id or not isinstance(encoded, str):
        raise PubSubPayloadError("Pub/Sub messageId or data is missing")
    try:
        raw = base64.b64decode(encoded, validate=True)
        decoded = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PubSubPayloadError("invalid Pub/Sub data encoding") from exc
    if not isinstance(decoded, Mapping):
        raise PubSubPayloadError("Pub/Sub data must be a JSON object")
    email = str(decoded.get("emailAddress", "")).strip().lower()
    history_id = str(decoded.get("historyId", "")).strip()
    if not email or "@" not in email or not history_id:
        raise PubSubPayloadError("emailAddress and historyId are required")
    published_at: datetime | None = None
    raw_time = message.get("publishTime")
    if isinstance(raw_time, str) and raw_time:
        try:
            published_at = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=UTC)
        except ValueError:
            raise PubSubPayloadError("invalid publishTime") from None
    return PubSubNotification(
        message_id=message_id,
        subscription=subscription,
        email_address=email,
        history_id=history_id,
        publish_time=published_at,
        payload_hash=hashlib.sha256(raw).hexdigest(),
    )


class PubSubIngestService:
    """先将可恢复记录提交到 PostgreSQL，再投递 Redis Streams。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        redis: RedisManager,
        settings: Settings,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.settings = settings

    async def accept(self, notification: PubSubNotification) -> str:
        request_id: UUID | None = None
        event_id: UUID | None = None
        async with self.session_factory() as session:
            async with session.begin():
                mailbox = await session.scalar(
                    select(Mailbox)
                    .where(Mailbox.email == notification.email_address)
                    .with_for_update()
                )
                if mailbox is None:
                    raise UnknownMailboxError("mailbox is not registered")
                event = await session.scalar(
                    select(InboundEvent).where(
                        InboundEvent.provider == "google_pubsub",
                        InboundEvent.pubsub_message_id == notification.message_id,
                    )
                )
                if event is None:
                    event = InboundEvent(
                        provider="google_pubsub",
                        pubsub_message_id=notification.message_id,
                        subscription=notification.subscription,
                        mailbox_id=mailbox.id,
                        observed_history_id=notification.history_id,
                        published_at=notification.publish_time,
                        received_at=datetime.now(UTC),
                        payload_hash=notification.payload_hash,
                        status="persisted",
                    )
                    session.add(event)
                    await session.flush()
                event_id = event.id
                request = await session.scalar(
                    select(SyncRequest)
                    .where(
                        SyncRequest.mailbox_id == mailbox.id,
                        SyncRequest.observed_history_id == notification.history_id,
                        SyncRequest.reason == "pubsub",
                    )
                    .with_for_update()
                )
                if request is None:
                    request = SyncRequest(
                        mailbox_id=mailbox.id,
                        observed_history_id=notification.history_id,
                        reason="pubsub",
                        status="queued",
                    )
                    session.add(request)
                    await session.flush()
                request_id = request.id
                mailbox.last_push_at = datetime.now(UTC)
                if str(event.status) != "enqueued":
                    event.status = "persisted"
        assert request_id is not None and event_id is not None
        try:
            await self.redis.enqueue_sync(
                self.settings.redis_stream_name,
                mailbox_id=str(mailbox.id),
                observed_history_id=notification.history_id,
                request_id=str(request_id),
                reason="pubsub",
                maxlen=self.settings.redis_stream_maxlen,
            )
        except Exception:
            # PostgreSQL 记录会被补偿 Scheduler 重新投递；本次返回 503 促使 Pub/Sub 重试。
            raise
        async with self.session_factory() as session:
            async with session.begin():
                event = await session.get(InboundEvent, event_id, with_for_update=True)
                if event is not None:
                    event.status = "enqueued"
                request = await session.get(SyncRequest, request_id, with_for_update=True)
                if request is not None and request.status not in {"completed", "dead_letter"}:
                    request.status = "enqueued"
        return str(request_id)


async def read_request_json(request: Request, *, max_bytes: int) -> Mapping[str, Any]:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(status_code=413, detail="request body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid content length") from exc
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(status_code=413, detail="request body too large")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    if not isinstance(payload, Mapping):
        raise HTTPException(status_code=400, detail="JSON object required")
    return payload


__all__ = [
    "PubSubAuthenticationError",
    "PubSubIngestService",
    "PubSubNotification",
    "PubSubPayloadError",
    "UnknownMailboxError",
    "parse_pubsub_push",
    "read_request_json",
    "verify_google_oidc_token",
]
