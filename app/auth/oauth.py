"""Google OAuth 2.0 Authorization Code 流程和 Refresh Token 管理。

该模块只实现 Gmail 官方 Web Server OAuth 流程，不包含账号密码登录或浏览器自动化。
HTTP 客户端和凭据存储都可注入，便于离线测试并避免真实 Google 凭据进入测试。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlencode
from uuid import UUID

import httpx
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.base import Mailbox, OAuthCredential
from app.state_machine import MailboxStatus, transition_mailbox

GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOCATION_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GMAIL_PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


class OAuthError(RuntimeError):
    """OAuth 流程错误基类。"""


class OAuthCallbackError(OAuthError):
    """回调参数或授权码交换失败。"""


class InvalidOAuthStateError(OAuthCallbackError):
    """state 缺失、过期或已经消费。"""


class OAuthAuthorizationDeniedError(OAuthCallbackError):
    """用户拒绝授权。"""


class OAuthTokenExchangeError(OAuthError):
    """Google Token Endpoint 返回错误。"""


class RefreshTokenMissingError(OAuthError):
    """无法刷新且没有可用 Refresh Token。"""


class OAuthReauthorizationRequired(OAuthError):
    """Refresh Token 已撤销或失效，需要重新授权。"""


@dataclass(frozen=True)
class OAuthStateData:
    state_hash: str
    expires_at: float
    mailbox_id: UUID | None
    return_to: str | None


class OAuthStateStore(Protocol):
    async def create(
        self, state: str, *, mailbox_id: UUID | None, return_to: str | None, ttl_seconds: int
    ) -> None: ...

    async def consume(self, state: str) -> OAuthStateData | None: ...


class InMemoryOAuthStateStore:
    """进程内短期 state 存储，生产环境应替换为 Redis 或数据库实现。"""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._entries: dict[str, OAuthStateData] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, state: str, *, mailbox_id: UUID | None, return_to: str | None, ttl_seconds: int
    ) -> None:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        async with self._lock:
            self._entries[state_hash] = OAuthStateData(
                state_hash=state_hash,
                expires_at=self._clock() + ttl_seconds,
                mailbox_id=mailbox_id,
                return_to=return_to,
            )

    async def consume(self, state: str) -> OAuthStateData | None:
        state_hash = hashlib.sha256(state.encode("utf-8")).hexdigest()
        async with self._lock:
            entry = self._entries.pop(state_hash, None)
        if entry is None or entry.expires_at <= self._clock():
            return None
        return entry


class RedisOAuthStateStore:
    """使用 Redis 保存哈希后的 OAuth state，并依赖 TTL 自动过期。"""

    def __init__(self, client: Any, *, key_prefix: str = "oauth:state:") -> None:
        self.client = client
        self.key_prefix = key_prefix

    def _key(self, state: str) -> str:
        digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        return f"{self.key_prefix}{digest}"

    async def create(
        self, state: str, *, mailbox_id: UUID | None, return_to: str | None, ttl_seconds: int
    ) -> None:
        payload = json.dumps(
            {
                "mailbox_id": str(mailbox_id) if mailbox_id else None,
                "return_to": return_to,
            },
            separators=(",", ":"),
        )
        await self.client.set(self._key(state), payload, ex=ttl_seconds)

    async def consume(self, state: str) -> OAuthStateData | None:
        key = self._key(state)
        getdel = getattr(self.client, "getdel", None)
        if getdel is not None:
            payload = await getdel(key)
        else:
            payload = await self.client.get(key)
            if payload is not None:
                await self.client.delete(key)
        if payload is None:
            return None
        try:
            data = json.loads(payload)
            mailbox_id = UUID(data["mailbox_id"]) if data.get("mailbox_id") else None
            return OAuthStateData(
                state_hash=key.rsplit(":", 1)[-1],
                expires_at=time.time() + 1,
                mailbox_id=mailbox_id,
                return_to=data.get("return_to"),
            )
        except (TypeError, ValueError, KeyError):
            return None


@dataclass(frozen=True)
class OAuthToken:
    access_token: str
    expires_at: float
    refresh_token: str | None = None
    scopes: tuple[str, ...] = ()

    @property
    def expires_in(self) -> float:
        return max(0.0, self.expires_at - time.time())


@dataclass(frozen=True)
class CurrentAccount:
    email: str
    gmail_user_id: str | None
    messages_total: int | None = None
    threads_total: int | None = None


@dataclass(frozen=True)
class AuthorizationRequest:
    state: str
    authorization_url: str
    mailbox_id: UUID | None


@dataclass(frozen=True)
class AuthorizationResult:
    account: CurrentAccount
    token: OAuthToken
    refresh_token_stored: bool
    return_to: str | None
    mailbox_id: UUID | None


@dataclass(frozen=True)
class StoredRefreshCredential:
    refresh_token: str
    scopes: tuple[str, ...]
    client_id_ref: str
    key_version: str


class RefreshTokenCipher(Protocol):
    def encrypt(self, token: str) -> str: ...

    def decrypt(self, ciphertext: str) -> str: ...


class FernetRefreshTokenCipher:
    """使用 Fernet 对 Refresh Token 做应用层加密。"""

    def __init__(self, key: str | bytes) -> None:
        raw_key = key.encode("utf-8") if isinstance(key, str) else key
        try:
            self._fernet = Fernet(raw_key)
        except (ValueError, TypeError):
            derived = base64.urlsafe_b64encode(hashlib.sha256(raw_key).digest())
            self._fernet = Fernet(derived)

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise OAuthError("refresh token decryption failed") from exc


class SqlAlchemyRefreshTokenStore:
    """基于现有 ``oauth_credentials`` 表的加密 Refresh Token 存储。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        cipher: RefreshTokenCipher,
        key_version: str,
        client_id_ref: str,
    ) -> None:
        self.session_factory = session_factory
        self.cipher = cipher
        self.key_version = key_version
        self.client_id_ref = client_id_ref

    async def get(self, mailbox_id: UUID) -> StoredRefreshCredential | None:
        async with self.session_factory() as session:
            row = await session.scalar(
                select(OAuthCredential).where(OAuthCredential.mailbox_id == mailbox_id)
            )
            if row is None:
                return None
            raw_scopes = row.scopes.get("items", []) if isinstance(row.scopes, dict) else []
            return StoredRefreshCredential(
                refresh_token=self.cipher.decrypt(row.refresh_token_ciphertext),
                scopes=tuple(str(value) for value in raw_scopes),
                client_id_ref=row.client_id_ref,
                key_version=row.refresh_token_key_version,
            )

    async def save(
        self,
        mailbox_id: UUID,
        *,
        refresh_token: str,
        scopes: tuple[str, ...],
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(OAuthCredential)
                    .where(OAuthCredential.mailbox_id == mailbox_id)
                    .with_for_update()
                )
                if row is None:
                    row = OAuthCredential(
                        mailbox_id=mailbox_id,
                        client_id_ref=self.client_id_ref,
                        scopes={"items": list(scopes)},
                        refresh_token_ciphertext=self.cipher.encrypt(refresh_token),
                        refresh_token_key_version=self.key_version,
                    )
                    session.add(row)
                else:
                    row.refresh_token_ciphertext = self.cipher.encrypt(refresh_token)
                    row.refresh_token_key_version = self.key_version
                    row.scopes = {"items": list(scopes)}
                    row.token_last_error = None
                    row.reauth_required_at = None
                await session.flush()

    async def mark_refreshed(self, mailbox_id: UUID) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(OAuthCredential)
                    .where(OAuthCredential.mailbox_id == mailbox_id)
                    .with_for_update()
                )
                if row is not None:
                    row.token_last_refreshed_at = datetime.now(UTC)
                    row.token_last_error = None

    async def mark_reauth_required(self, mailbox_id: UUID, error_code: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                row = await session.scalar(
                    select(OAuthCredential)
                    .where(OAuthCredential.mailbox_id == mailbox_id)
                    .with_for_update()
                )
                if row is not None:
                    row.token_last_error = error_code
                    row.reauth_required_at = datetime.now(UTC)
                mailbox = await session.scalar(
                    select(Mailbox).where(Mailbox.id == mailbox_id).with_for_update()
                )
                if mailbox is not None and mailbox.status != MailboxStatus.REAUTH_REQUIRED:
                    try:
                        await transition_mailbox(session, mailbox, MailboxStatus.REAUTH_REQUIRED)
                    except ValueError:
                        # 终态邮箱保持原状态；凭据表仍记录需要重新授权。
                        pass

    async def invalidate(self, mailbox_id: UUID, error_code: str = "revoked") -> None:
        await self.mark_reauth_required(mailbox_id, error_code)


class GoogleOAuthService:
    """Google OAuth 服务，HTTP 和持久化依赖均可替换。"""

    def __init__(
        self,
        settings: Settings,
        *,
        state_store: OAuthStateStore | None = None,
        refresh_store: Any | None = None,
        http_client: Any | None = None,
        state_ttl_seconds: int = 600,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not settings.google_client_id or not settings.google_client_secret:
            raise ValueError("Google OAuth client configuration is incomplete")
        if not settings.google_redirect_uri:
            raise ValueError("GOOGLE_REDIRECT_URI is required")
        self.settings = settings
        self.state_store = state_store or InMemoryOAuthStateStore(clock=clock)
        self.refresh_store = refresh_store
        self.http_client = http_client
        self.state_ttl_seconds = state_ttl_seconds
        self._clock = clock
        self._refresh_locks: dict[UUID, asyncio.Lock] = {}
        self._access_tokens: dict[UUID, OAuthToken] = {}

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(
            value for value in self.settings.google_oauth_scopes.replace(",", " ").split() if value
        )

    def _client_secret(self) -> str:
        secret = self.settings.google_client_secret
        if secret is None:
            raise OAuthTokenExchangeError("Google OAuth client secret is not configured")
        return secret.get_secret_value()

    async def start_authorization(
        self,
        *,
        mailbox_id: UUID | None = None,
        mailbox_hint: str | None = None,
        return_to: str | None = None,
        force_consent: bool = True,
    ) -> AuthorizationRequest:
        if return_to is not None and (not return_to.startswith("/") or return_to.startswith("//")):
            raise OAuthCallbackError("return_to must be a local path")
        state = secrets.token_urlsafe(32)
        await self.state_store.create(
            state,
            mailbox_id=mailbox_id,
            return_to=return_to,
            ttl_seconds=self.state_ttl_seconds,
        )
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": self.settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "access_type": "offline",
            "include_granted_scopes": "true",
            "state": state,
        }
        if force_consent:
            params["prompt"] = "consent"
        if mailbox_hint:
            params["login_hint"] = mailbox_hint
        return AuthorizationRequest(
            state=state,
            authorization_url=f"{GOOGLE_AUTHORIZATION_ENDPOINT}?{urlencode(params)}",
            mailbox_id=mailbox_id,
        )

    async def create_authorization_url(self, **kwargs: Any) -> AuthorizationRequest:
        return await self.start_authorization(**kwargs)

    async def handle_callback(
        self,
        *,
        code: str | None,
        state: str | None,
        error: str | None = None,
    ) -> AuthorizationResult:
        if not state:
            raise InvalidOAuthStateError("missing OAuth state")
        state_data = await self.state_store.consume(state)
        if state_data is None:
            raise InvalidOAuthStateError("invalid or expired OAuth state")
        if error:
            raise OAuthAuthorizationDeniedError("Google authorization was denied")
        if not code:
            raise OAuthCallbackError("missing authorization code")
        token = await self.exchange_code(code)
        existing = None
        if state_data.mailbox_id is not None and self.refresh_store is not None:
            existing = await self.refresh_store.get(state_data.mailbox_id)
        refresh_token = token.refresh_token or (existing.refresh_token if existing else None)
        if refresh_token is None:
            raise RefreshTokenMissingError(
                "Google did not return a refresh token; reauthorize with consent"
            )
        account = await self.get_current_account(token.access_token)
        if state_data.mailbox_id is not None and self.refresh_store is not None:
            await self.refresh_store.save(
                state_data.mailbox_id,
                refresh_token=refresh_token,
                scopes=token.scopes or self.scopes,
            )
        return AuthorizationResult(
            account=account,
            token=OAuthToken(
                access_token=token.access_token,
                expires_at=token.expires_at,
                refresh_token=refresh_token,
                scopes=token.scopes or self.scopes,
            ),
            refresh_token_stored=token.refresh_token is not None,
            return_to=state_data.return_to,
            mailbox_id=state_data.mailbox_id,
        )

    async def exchange_code(self, code: str) -> OAuthToken:
        response = await self._request(
            "post",
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self._client_secret(),
                "redirect_uri": self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        return self._parse_token(response)

    async def refresh_access_token(self, mailbox_id: UUID) -> OAuthToken:
        if self.refresh_store is None:
            raise RefreshTokenMissingError("refresh token store is not configured")
        lock = self._refresh_locks.setdefault(mailbox_id, asyncio.Lock())
        async with lock:
            cached = self._access_tokens.get(mailbox_id)
            if cached is not None and cached.expires_at - self._clock() > 30:
                return cached
            stored = await self.refresh_store.get(mailbox_id)
            if stored is None:
                raise RefreshTokenMissingError(f"no refresh token for mailbox {mailbox_id}")
            try:
                response = await self._request(
                    "post",
                    GOOGLE_TOKEN_ENDPOINT,
                    data={
                        "client_id": self.settings.google_client_id,
                        "client_secret": self._client_secret(),
                        "refresh_token": stored.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
            except OAuthTokenExchangeError as exc:
                if exc.args and "invalid_grant" in str(exc):
                    await self.refresh_store.mark_reauth_required(mailbox_id, "invalid_grant")
                    raise OAuthReauthorizationRequired(
                        "refresh token is invalid or revoked"
                    ) from exc
                raise
            token = self._parse_token(response)
            self._access_tokens[mailbox_id] = token
            mark_refreshed = getattr(self.refresh_store, "mark_refreshed", None)
            if mark_refreshed is not None:
                await mark_refreshed(mailbox_id)
            return token

    def invalidate_access_token(self, mailbox_id: UUID) -> None:
        """丢弃指定邮箱的短期 Access Token 缓存。

        Gmail API 返回 401 时由客户端调用，下一次请求会通过现有刷新锁重新获取令牌。
        """

        self._access_tokens.pop(mailbox_id, None)

    async def get_current_account(self, access_token: str) -> CurrentAccount:
        payload = await self._request(
            "get",
            GMAIL_PROFILE_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        email = str(payload.get("emailAddress", "")).strip()
        if not email:
            raise OAuthCallbackError("Google profile did not include an email address")
        return CurrentAccount(
            email=email,
            gmail_user_id=str(payload["userId"]) if payload.get("userId") else None,
            messages_total=int(payload["messagesTotal"]) if payload.get("messagesTotal") else None,
            threads_total=int(payload["threadsTotal"]) if payload.get("threadsTotal") else None,
        )

    async def revoke(self, mailbox_id: UUID) -> None:
        if self.refresh_store is None:
            raise RefreshTokenMissingError("refresh token store is not configured")
        stored = await self.refresh_store.get(mailbox_id)
        if stored is None:
            raise RefreshTokenMissingError(f"no refresh token for mailbox {mailbox_id}")
        await self._request(
            "post",
            GOOGLE_REVOCATION_ENDPOINT,
            data={"token": stored.refresh_token},
            expect_json=False,
        )
        await self.refresh_store.invalidate(mailbox_id, "revoked")
        self._access_tokens.pop(mailbox_id, None)

    async def _request(
        self, method: str, url: str, *, expect_json: bool = True, **kwargs: Any
    ) -> dict[str, Any]:
        own_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(timeout=15.0)
        try:
            try:
                response = await getattr(client, method)(url, **kwargs)
            except httpx.HTTPError as exc:
                raise OAuthTokenExchangeError("Google OAuth network error") from exc
            status_code = int(getattr(response, "status_code", 200))
            if not expect_json:
                if status_code >= 400:
                    raise OAuthTokenExchangeError("Google OAuth revocation failed")
                return {}
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise OAuthTokenExchangeError("Google returned invalid JSON") from exc
            if status_code >= 400:
                error_code = str(payload.get("error", "http_error"))
                raise OAuthTokenExchangeError(f"Google OAuth error: {error_code}")
            if not isinstance(payload, dict):
                raise OAuthTokenExchangeError("Google returned an invalid response")
            return payload
        finally:
            if own_client:
                await client.aclose()

    def _parse_token(self, payload: dict[str, Any]) -> OAuthToken:
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            error_code = payload.get("error", "missing_access_token")
            raise OAuthTokenExchangeError(f"Google OAuth error: {error_code}")
        expires_in = int(payload.get("expires_in", 3600))
        scope_value = payload.get("scope")
        scopes = tuple(str(scope_value).split()) if scope_value else self.scopes
        refresh_token = payload.get("refresh_token")
        return OAuthToken(
            access_token=access_token,
            expires_at=self._clock() + max(0, expires_in),
            refresh_token=refresh_token if isinstance(refresh_token, str) else None,
            scopes=scopes,
        )


__all__ = [
    "AuthorizationRequest",
    "AuthorizationResult",
    "CurrentAccount",
    "FernetRefreshTokenCipher",
    "GoogleOAuthService",
    "GMAIL_PROFILE_ENDPOINT",
    "GOOGLE_AUTHORIZATION_ENDPOINT",
    "GOOGLE_REVOCATION_ENDPOINT",
    "GOOGLE_TOKEN_ENDPOINT",
    "InMemoryOAuthStateStore",
    "RedisOAuthStateStore",
    "InvalidOAuthStateError",
    "OAuthAuthorizationDeniedError",
    "OAuthCallbackError",
    "OAuthError",
    "OAuthReauthorizationRequired",
    "OAuthStateData",
    "OAuthToken",
    "OAuthTokenExchangeError",
    "RefreshTokenMissingError",
    "SqlAlchemyRefreshTokenStore",
    "StoredRefreshCredential",
]
