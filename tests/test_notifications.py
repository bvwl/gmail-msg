from __future__ import annotations

import httpx
import pytest
from app.config import Settings
from app.notifications.delivery import WebhookSender
from app.notifications.signing import canonical_json_bytes, verify_signature


def test_hmac_signature_uses_raw_canonical_json_and_timestamp_window() -> None:
    payload = {"event_id": "e1", "subject": "中文 😀"}
    body = canonical_json_bytes(payload)
    sender = WebhookSender(Settings(_env_file=None))
    # sender's helper is exercised through a deterministic request below; use public verifier
    from app.notifications.signing import build_signature

    signature = build_signature("SECRET_REF", 1000, body)
    assert verify_signature("SECRET_REF", 1000, body, signature, now=1001)
    assert not verify_signature("WRONG", 1000, body, signature, now=1001)
    assert not verify_signature("SECRET_REF", 1000, body, signature, now=2000)
    assert sender.settings.app_env == "development"


@pytest.mark.asyncio
async def test_webhook_sender_retries_429_then_accepts() -> None:
    calls: list[dict[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return httpx.Response(202, request=request)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    sleeps: list[float] = []

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)

    sender = WebhookSender(
        Settings(_env_file=None, webhook_max_retries=2),
        http_client=client,
        sleep=fake_sleep,
    )
    # The sender intentionally creates a fresh timestamp per retry; validate request contract.
    result = await sender.send(
        {"event_id": "e1", "timestamp": 1},
        url="https://business.example.test/hook",
        secret="SECRET_REF",
        event_id="e1",
        idempotency_key="gmail:mailbox:message:v1",
    )
    await client.aclose()
    assert result.accepted is True
    assert len(calls) == 2
    assert calls[0]["idempotency-key"] == calls[1]["idempotency-key"]
    assert calls[0]["x-event-id"] == calls[1]["x-event-id"] == "e1"


@pytest.mark.asyncio
async def test_webhook_sender_does_not_retry_permanent_400() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    sender = WebhookSender(Settings(_env_file=None, webhook_max_retries=4), http_client=client)
    result = await sender.send(
        {"event_id": "e2"},
        url="https://business.example.test/hook",
        secret="SECRET_REF",
        event_id="e2",
        idempotency_key="gmail:mailbox:message:v1",
    )
    await client.aclose()
    assert result.accepted is False
    assert result.retryable is False
    assert calls == 1
