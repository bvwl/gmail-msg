from __future__ import annotations

import base64
import json
import sys
import time
import types

import pytest
from app.pubsub import (
    PubSubAuthenticationError,
    PubSubPayloadError,
    parse_pubsub_push,
    verify_google_oidc_token,
)


def _payload(data: object) -> dict[str, object]:
    encoded = base64.b64encode(json.dumps(data).encode()).decode()
    return {
        "message": {
            "messageId": "pubsub-1",
            "data": encoded,
            "publishTime": "2026-11-05T00:00:00Z",
        },
        "subscription": "projects/PROJECT_ID/subscriptions/SUBSCRIPTION",
    }


def test_parse_pubsub_push_decodes_gmail_notification() -> None:
    notification = parse_pubsub_push(
        _payload({"emailAddress": "User@Example.com", "historyId": "123"})
    )

    assert notification.email_address == "user@example.com"
    assert notification.history_id == "123"
    assert notification.publish_time is not None
    assert len(notification.payload_hash) == 64


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"message": {"messageId": "x", "data": "%%%"}, "subscription": "sub"},
        _payload({"emailAddress": "user@example.com"}),
    ],
)
def test_parse_pubsub_push_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(PubSubPayloadError):
        parse_pubsub_push(payload)


def test_oidc_claims_require_audience_and_service_account(monkeypatch: pytest.MonkeyPatch) -> None:
    google = types.ModuleType("google")
    auth = types.ModuleType("google.auth")
    transport = types.ModuleType("google.auth.transport")
    requests = types.ModuleType("google.auth.transport.requests")
    oauth2 = types.ModuleType("google.oauth2")
    id_token = types.ModuleType("google.oauth2.id_token")
    google.__path__ = []
    auth.__path__ = []
    transport.__path__ = []
    oauth2.__path__ = []

    class Request:  # noqa: D101
        pass

    requests.Request = Request  # type: ignore[attr-defined]
    id_token.verify_oauth2_token = lambda *_args: {  # type: ignore[attr-defined]
        "iss": "https://accounts.google.com",
        "aud": "AUDIENCE",
        "email": "push@example.iam.gserviceaccount.com",
        "email_verified": True,
        "exp": time.time() + 60,
    }
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.auth", auth)
    monkeypatch.setitem(sys.modules, "google.auth.transport", transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", requests)
    monkeypatch.setitem(sys.modules, "google.oauth2", oauth2)
    monkeypatch.setitem(sys.modules, "google.oauth2.id_token", id_token)

    with pytest.raises(PubSubAuthenticationError, match="audience"):
        verify_google_oidc_token(
            "token",
            audience="OTHER",
            service_account="push@example.iam.gserviceaccount.com",
        )
