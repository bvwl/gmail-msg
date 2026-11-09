from __future__ import annotations

import asyncio
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from app.auth.oauth import (
    GOOGLE_TOKEN_ENDPOINT,
    FernetRefreshTokenCipher,
    GoogleOAuthService,
    InvalidOAuthStateError,
    OAuthAuthorizationDeniedError,
    OAuthReauthorizationRequired,
    OAuthTokenExchangeError,
    RefreshTokenMissingError,
)
from app.config import Settings


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeResponse], *, delay: float = 0.0) -> None:
        self.responses = responses
        self.delay = delay
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.calls.append(("post", url, kwargs))
        return self.responses.pop(0)

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("get", url, kwargs))
        return self.responses.pop(0)


@dataclass
class FakeCredential:
    refresh_token: str
    scopes: tuple[str, ...] = ("https://www.googleapis.com/auth/gmail.readonly",)


class FakeRefreshStore:
    def __init__(self, credential: FakeCredential | None = None) -> None:
        self.credential = credential
        self.saved: list[dict[str, object]] = []
        self.reauth: list[tuple[object, str]] = []
        self.invalidated: list[tuple[object, str]] = []
        self.get_calls = 0

    async def get(self, _mailbox_id: object) -> FakeCredential | None:
        self.get_calls += 1
        return self.credential

    async def save(self, mailbox_id: object, **kwargs: object) -> None:
        self.saved.append({"mailbox_id": mailbox_id, **kwargs})
        self.credential = FakeCredential(str(kwargs["refresh_token"]))

    async def mark_reauth_required(self, mailbox_id: object, error_code: str) -> None:
        self.reauth.append((mailbox_id, error_code))

    async def invalidate(self, mailbox_id: object, error_code: str) -> None:
        self.invalidated.append((mailbox_id, error_code))

    async def mark_refreshed(self, _mailbox_id: object) -> None:
        return None


def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        google_client_id="CLIENT_ID",
        google_client_secret="CLIENT_SECRET",
        google_redirect_uri="https://example.test/oauth/google/callback",
    )


def test_refresh_token_cipher_does_not_store_plaintext() -> None:
    cipher = FernetRefreshTokenCipher("TEST_SECRET_REF")

    ciphertext = cipher.encrypt("REFRESH_TOKEN")

    assert ciphertext != "REFRESH_TOKEN"
    assert cipher.decrypt(ciphertext) == "REFRESH_TOKEN"


def test_authorization_url_contains_offline_minimal_scope_and_state() -> None:
    service = GoogleOAuthService(settings())

    request = asyncio.run(
        service.start_authorization(mailbox_hint="user@example.com", return_to="/done")
    )
    query = parse_qs(urlparse(request.authorization_url).query)

    assert query["client_id"] == ["CLIENT_ID"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert query["state"] == [request.state]
    assert query["login_hint"] == ["user@example.com"]


@pytest.mark.asyncio
async def test_callback_validates_state_exchanges_token_and_fetches_profile() -> None:
    client = FakeHttpClient(
        [
            FakeResponse(
                200, {"access_token": "ACCESS", "refresh_token": "REFRESH", "expires_in": 3600}
            ),
            FakeResponse(200, {"emailAddress": "user@example.com", "userId": "google-user"}),
        ]
    )
    store = FakeRefreshStore()
    mailbox_id = uuid4()
    service = GoogleOAuthService(settings(), http_client=client, refresh_store=store)
    request = await service.start_authorization(mailbox_id=mailbox_id)

    result = await service.handle_callback(code="CODE", state=request.state)

    assert result.account.email == "user@example.com"
    assert result.account.gmail_user_id == "google-user"
    assert result.token.access_token == "ACCESS"
    assert result.refresh_token_stored is True
    assert store.saved[0]["refresh_token"] == "REFRESH"
    assert client.calls[0][0:2] == ("post", GOOGLE_TOKEN_ENDPOINT)


@pytest.mark.asyncio
async def test_callback_rejects_missing_or_replayed_state() -> None:
    service = GoogleOAuthService(settings())

    with pytest.raises(InvalidOAuthStateError):
        await service.handle_callback(code="CODE", state=None)

    request = await service.start_authorization()
    with pytest.raises(InvalidOAuthStateError):
        await service.handle_callback(code="CODE", state="unknown-state")
    assert await service.state_store.consume(request.state) is not None
    with pytest.raises(InvalidOAuthStateError):
        await service.handle_callback(code="CODE", state=request.state)


@pytest.mark.asyncio
async def test_callback_preserves_existing_refresh_token_when_google_omits_it() -> None:
    client = FakeHttpClient(
        [
            FakeResponse(200, {"access_token": "ACCESS", "expires_in": 3600}),
            FakeResponse(200, {"emailAddress": "user@example.com"}),
        ]
    )
    store = FakeRefreshStore(FakeCredential("OLD_REFRESH"))
    service = GoogleOAuthService(settings(), http_client=client, refresh_store=store)
    request = await service.start_authorization(mailbox_id=uuid4())

    result = await service.handle_callback(code="CODE", state=request.state)

    assert result.token.refresh_token == "OLD_REFRESH"
    assert result.refresh_token_stored is False
    assert store.saved[0]["refresh_token"] == "OLD_REFRESH"


@pytest.mark.asyncio
async def test_callback_requires_refresh_token_for_new_authorization() -> None:
    client = FakeHttpClient(
        [
            FakeResponse(200, {"access_token": "ACCESS", "expires_in": 3600}),
        ]
    )
    service = GoogleOAuthService(settings(), http_client=client)
    request = await service.start_authorization(mailbox_id=uuid4())

    with pytest.raises(RefreshTokenMissingError):
        await service.handle_callback(code="CODE", state=request.state)


@pytest.mark.asyncio
async def test_callback_denial_is_classified_without_exposing_provider_details() -> None:
    service = GoogleOAuthService(settings())
    request = await service.start_authorization()

    with pytest.raises(OAuthAuthorizationDeniedError):
        await service.handle_callback(code=None, state=request.state, error="access_denied")


@pytest.mark.asyncio
async def test_refresh_success_and_concurrent_calls_share_cached_token() -> None:
    client = FakeHttpClient(
        [FakeResponse(200, {"access_token": "REFRESHED", "expires_in": 3600})], delay=0.02
    )
    store = FakeRefreshStore(FakeCredential("REFRESH"))
    service = GoogleOAuthService(settings(), http_client=client, refresh_store=store)
    mailbox_id = uuid4()

    first, second = await asyncio.gather(
        service.refresh_access_token(mailbox_id), service.refresh_access_token(mailbox_id)
    )

    assert first.access_token == second.access_token == "REFRESHED"
    assert len([call for call in client.calls if call[0] == "post"]) == 1


@pytest.mark.asyncio
async def test_refresh_invalid_grant_marks_reauthorization() -> None:
    client = FakeHttpClient([FakeResponse(400, {"error": "invalid_grant"})])
    store = FakeRefreshStore(FakeCredential("REFRESH"))
    service = GoogleOAuthService(settings(), http_client=client, refresh_store=store)
    mailbox_id = uuid4()

    with pytest.raises(OAuthReauthorizationRequired):
        await service.refresh_access_token(mailbox_id)

    assert store.reauth == [(mailbox_id, "invalid_grant")]


@pytest.mark.asyncio
async def test_refresh_server_failure_is_retryable_oauth_error() -> None:
    client = FakeHttpClient([FakeResponse(503, {"error": "temporarily_unavailable"})])
    store = FakeRefreshStore(FakeCredential("REFRESH"))
    service = GoogleOAuthService(settings(), http_client=client, refresh_store=store)

    with pytest.raises(OAuthTokenExchangeError, match="temporarily_unavailable"):
        await service.refresh_access_token(uuid4())
