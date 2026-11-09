from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import uuid4

import pytest
from app.auth.oauth import OAuthToken
from app.config import Settings
from app.gmail.client import (
    GmailApiClient,
    GmailApiNotFoundError,
    GmailApiPermissionError,
    GmailApiRateLimitError,
    GmailApiServerError,
    OAuthServiceProtocol,
)
from app.gmail.quota import InMemoryQuotaLimiter


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("get", url, kwargs))
        return self.responses.pop(0)

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("post", url, kwargs))
        return self.responses.pop(0)


@dataclass
class FakeOAuth:
    token: OAuthToken = OAuthToken("ACCESS", 9_999_999_999)
    refreshes: int = 0
    invalidations: int = 0

    async def refresh_access_token(self, _mailbox_id: object) -> OAuthToken:
        self.refreshes += 1
        return self.token

    def invalidate_access_token(self, _mailbox_id: object) -> None:
        self.invalidations += 1


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        gmail_max_retries=2,
        gmail_retry_base_seconds=0.001,
        gmail_retry_max_seconds=0.01,
    )


@pytest.mark.asyncio
async def test_profile_and_message_requests_use_access_token_and_pagination() -> None:
    http = FakeHttpClient(
        [
            FakeResponse(200, {"emailAddress": "user@example.com"}),
            FakeResponse(200, {"messages": [{"id": "m1"}], "nextPageToken": "next"}),
            FakeResponse(200, {"messages": [{"id": "m2"}]}),
        ]
    )
    oauth = FakeOAuth()
    client = GmailApiClient(cast(OAuthServiceProtocol, oauth), settings(), http_client=http)
    mailbox_id = uuid4()

    profile = await client.get_profile(mailbox_id)
    messages = await client.list_all_messages(mailbox_id)

    assert profile["emailAddress"] == "user@example.com"
    assert [item["id"] for item in messages] == ["m1", "m2"]
    headers = cast(dict[str, str], http.calls[0][2]["headers"])
    assert headers["Authorization"] == "Bearer ACCESS"


@pytest.mark.asyncio
async def test_401_invalidates_token_and_retries_once() -> None:
    http = FakeHttpClient(
        [FakeResponse(401, {"error": {"message": "expired"}}), FakeResponse(200, {"id": "m1"})]
    )
    oauth = FakeOAuth()
    client = GmailApiClient(cast(OAuthServiceProtocol, oauth), settings(), http_client=http)

    result = await client.get_message(uuid4(), "m1")

    assert result["id"] == "m1"
    assert oauth.invalidations == 1
    assert oauth.refreshes == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "error_type"),
    [
        (403, {"error": {"reason": "forbidden"}}, GmailApiPermissionError),
        (404, {"error": {"message": "missing"}}, GmailApiNotFoundError),
        (429, {"error": {"reason": "rateLimitExceeded"}}, GmailApiRateLimitError),
        (500, {"error": {"message": "server"}}, GmailApiServerError),
    ],
)
async def test_error_classification(
    status: int, payload: dict[str, object], error_type: type[Exception]
) -> None:
    http = FakeHttpClient(
        [
            FakeResponse(status, payload),
            FakeResponse(status, payload),
            FakeResponse(status, payload),
        ]
    )
    client = GmailApiClient(
        cast(OAuthServiceProtocol, FakeOAuth()),
        settings(),
        http_client=http,
        sleep=lambda _seconds: _completed(),
        random_fn=lambda: 0.0,
    )

    with pytest.raises(error_type):
        await client.get_profile(uuid4())


async def _completed() -> None:
    return None


@pytest.mark.asyncio
async def test_history_pagination_and_quota_consumption() -> None:
    http = FakeHttpClient(
        [
            FakeResponse(200, {"history": [{"id": "10"}], "nextPageToken": "p2"}),
            FakeResponse(200, {"history": [{"id": "11"}]}),
        ]
    )
    mailbox_id = uuid4()
    quota = InMemoryQuotaLimiter()
    client = GmailApiClient(
        cast(OAuthServiceProtocol, FakeOAuth()), settings(), http_client=http, quota_limiter=quota
    )

    items = [item async for item in client.iter_history(mailbox_id, start_history_id="9")]

    assert [item["id"] for item in items] == ["10", "11"]
    assert quota.consumed[mailbox_id] == 4


@pytest.mark.asyncio
async def test_attachment_request_uses_attachment_endpoint_and_quota() -> None:
    http = FakeHttpClient([FakeResponse(200, {"data": "YWJj"})])
    mailbox_id = uuid4()
    quota = InMemoryQuotaLimiter()
    client = GmailApiClient(
        cast(OAuthServiceProtocol, FakeOAuth()), settings(), http_client=http, quota_limiter=quota
    )
    result = await client.get_attachment(mailbox_id, "m1", "a1")
    assert result["data"] == "YWJj"
    assert "/messages/m1/attachments/a1" in http.calls[0][1]
    assert quota.consumed[mailbox_id] == 5
