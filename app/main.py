"""IMAP 邮件监控 FastAPI 管理 API。"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, cast
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, SecretStr, field_validator
from sqlalchemy import Text, func, or_, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app import __version__
from app.auth.admin import hash_password
from app.config import Settings, get_settings
from app.db.base import (
    AdminSession,
    AdminUser,
    AuditLog,
    Mailbox,
    MailboxFolder,
    Message,
    MessageLabel,
    RetentionRun,
    VerificationCode,
)
from app.db.session import DatabaseManager, build_database, get_db_session
from app.security.secrets import MailboxCredentialCipher
from app.security.validation import (
    origin_from_referer,
    validate_folder_name,
    validate_imap_host,
    validate_proxy_host,
)

SERVICE_NAME = "imap-mail-monitor"
_password_hasher = PasswordHasher()
_docs_basic = HTTPBasic(auto_error=False)


class LoginRequest(BaseModel):
    username: str
    password: str


class MailboxRequest(BaseModel):
    address: str
    imap_host: str = "imap.gmail.com"
    imap_port: int = Field(993, ge=1, le=65535)
    username: str
    app_password: SecretStr | None = None
    app_password_secret_ref: str | None = None
    proxy_mode: str = "direct"
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    proxy_password: SecretStr | None = None
    proxy_password_secret_ref: str | None = None
    proxy_dns_mode: str | None = None
    folders: list[str] = Field(default_factory=lambda: ["INBOX"], max_length=100)

    @field_validator("imap_host")
    @classmethod
    def normalize_imap_host(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("app_password", mode="before")
    @classmethod
    def normalize_app_password(cls, value: object) -> object:
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        normalized = "".join(raw.split())
        if not normalized:
            raise ValueError("APP_PASSWORD_REQUIRED")
        return normalized

    @field_validator("proxy_host")
    @classmethod
    def validate_proxy_host_input(cls, value: str | None) -> str | None:
        return validate_proxy_host(value) if value else None

    @field_validator("folders")
    @classmethod
    def validate_folders(cls, value: list[str]) -> list[str]:
        result = [validate_folder_name(item) for item in value]
        if len(set(result)) != len(result):
            raise ValueError("folders_must_be_unique")
        return result or ["INBOX"]


class MailboxPatch(BaseModel):
    enabled: bool | None = None
    app_password: SecretStr | None = None
    app_password_secret_ref: str | None = None
    proxy_mode: str | None = None
    proxy_host: str | None = None
    proxy_port: int | None = None
    proxy_username: str | None = None
    proxy_password: SecretStr | None = None
    proxy_password_secret_ref: str | None = None
    proxy_dns_mode: str | None = None

    @field_validator("app_password", mode="before")
    @classmethod
    def normalize_app_password(cls, value: object) -> object:
        if value is None:
            return None
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        if not isinstance(raw, str):
            return value
        normalized = "".join(raw.split())
        if not normalized:
            raise ValueError("APP_PASSWORD_REQUIRED")
        return normalized

    @field_validator("proxy_host")
    @classmethod
    def validate_proxy_host_input(cls, value: str | None) -> str | None:
        return validate_proxy_host(value) if value else None


def _hash_session(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _docs_auth(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_docs_basic),  # noqa: B008
) -> None:
    cfg = request.app.state.settings
    if not cfg.docs_username or not cfg.docs_password:
        raise HTTPException(status_code=404, detail="DOCS_NOT_CONFIGURED")
    if credentials is None or not (
        secrets.compare_digest(credentials.username, cfg.docs_username)
        and secrets.compare_digest(credentials.password, cfg.docs_password)
    ):
        raise HTTPException(
            status_code=401,
            detail="DOCS_AUTH_REQUIRED",
            headers={"WWW-Authenticate": "Basic"},
        )


def _audit_row(
    admin_user_id: UUID | None,
    action: str,
    success: bool,
    request: Request,
    *,
    resource_type: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> AuditLog:
    return AuditLog(
        admin_user_id=admin_user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        success=success,
        remote_addr=request.client.host if request.client else None,
        metadata_json=metadata,
    )


async def _current_admin(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None),
) -> AdminUser:
    configured_api_key = request.app.state.settings.external_api_key
    bearer_key = (
        authorization.removeprefix("Bearer ").strip()
        if authorization and authorization.startswith("Bearer ")
        else None
    )
    if configured_api_key and (
        (x_api_key and secrets.compare_digest(x_api_key, configured_api_key))
        or (bearer_key and secrets.compare_digest(bearer_key, configured_api_key))
    ):
        async with request.app.state.database.session() as db:
            service_admin = (
                await db.execute(
                    select(AdminUser)
                    .where(AdminUser.is_active.is_(True))
                    .order_by(AdminUser.created_at)
                )
            ).scalars().first()
            if service_admin is not None:
                return cast(AdminUser, service_admin)
    session_id = request.cookies.get(request.app.state.settings.admin_session_cookie)
    if not session_id:
        raise HTTPException(
            status_code=401, detail={"code": "AUTH_REQUIRED", "message": "登录已失效"}
        )
    async with request.app.state.database.session() as db:
        row = (
            await db.execute(
                select(AdminUser)
                .join(AdminSession, AdminSession.admin_user_id == AdminUser.id)
                .where(
                    AdminSession.session_hash == _hash_session(session_id),
                    AdminSession.revoked_at.is_(None),
                    AdminSession.expires_at > datetime.now(UTC),
                    AdminUser.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            db.add(
                _audit_row(
                    None,
                    "session.invalid",
                    False,
                    request,
                    metadata={"reason": "expired_or_revoked"},
                )
            )
            await db.commit()
            raise HTTPException(
                status_code=401, detail={"code": "AUTH_REQUIRED", "message": "登录已失效"}
            )
        return cast(AdminUser, row)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = app.state.settings
    if cfg.admin_username or cfg.admin_password:
        if not cfg.admin_username or not cfg.admin_password:
            raise RuntimeError("ADMIN_USERNAME and ADMIN_PASSWORD must be configured together")
        if len(cfg.admin_password) < 12:
            raise RuntimeError("ADMIN_PASSWORD must be at least 12 characters")
        async with app.state.database.session() as db:
            user = (
                await db.execute(
                    select(AdminUser).where(AdminUser.username == cfg.admin_username)
                )
            ).scalar_one_or_none()
            if user is None:
                db.add(
                    AdminUser(
                        username=cfg.admin_username,
                        password_hash=hash_password(cfg.admin_password),
                    )
                )
            else:
                try:
                    password_matches: bool = False
                    password_matches = _password_hasher.verify(
                        user.password_hash, cfg.admin_password
                    )
                except Exception:
                    password_matches = False
                if not password_matches:
                    user.password_hash = hash_password(cfg.admin_password)
                    user.failed_attempts = 0
                    user.locked_until = None
            await db.commit()
    yield
    db = getattr(app.state, "database", None)
    if db:
        await db.dispose()


def create_app(
    settings: Settings | None = None, *, database: DatabaseManager | None = None
) -> FastAPI:
    cfg = settings or get_settings()
    app = FastAPI(
        title="IMAP 邮件监控管理 API",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = cfg
    app.state.database = database or build_database(cfg)

    @app.middleware("http")
    async def csrf_guard(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            session_cookie = request.cookies.get(cfg.admin_session_cookie)
            if session_cookie and cfg.app_env == "production":
                origin = request.headers.get("origin")
                if origin is None:
                    origin = origin_from_referer(request.headers.get("referer", ""))
                if origin not in cfg.cors_origins:
                    return Response(
                        content='{"detail":"CSRF_ORIGIN_REJECTED"}',
                        status_code=403,
                        media_type="application/json",
                    )
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_credentials=cfg.cors_allow_credentials,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    )

    @app.get("/docs", include_in_schema=False, dependencies=[Depends(_docs_auth)])
    async def protected_docs() -> Response:
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} - Swagger UI")

    @app.get("/redoc", include_in_schema=False, dependencies=[Depends(_docs_auth)])
    async def protected_redoc() -> Response:
        return get_redoc_html(openapi_url="/openapi.json", title=f"{app.title} - ReDoc")

    @app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(_docs_auth)])
    async def protected_openapi() -> dict[str, object]:
        return get_openapi(title=app.title, version=app.version, routes=app.routes)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"service": SERVICE_NAME, "version": __version__, "status": "ok"}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready() -> Response:
        try:
            await app.state.database.check()
            return Response(
                content='{"status":"ok","checks":{"postgres":"ok"}}', media_type="application/json"
            )
        except Exception:
            return Response(
                content='{"status":"degraded","checks":{"postgres":"unavailable"}}',
                media_type="application/json",
                status_code=503,
            )

    @app.post("/api/admin/login", status_code=200)
    async def login(payload: LoginRequest, response: Response, request: Request) -> None:
        now = datetime.now(UTC)
        async with app.state.database.session() as db:
            user = (
                await db.execute(select(AdminUser).where(AdminUser.username == payload.username))
            ).scalar_one_or_none()
            valid = False
            if user and user.is_active and (not user.locked_until or user.locked_until <= now):
                try:
                    valid = _password_hasher.verify(user.password_hash, payload.password)
                except (VerifyMismatchError, Exception):
                    valid = False
            if not valid:
                if user:
                    user.failed_attempts += 1
                    if user.failed_attempts >= cfg.admin_login_max_attempts:
                        user.locked_until = now + timedelta(seconds=cfg.admin_lockout_seconds)
                        user.failed_attempts = 0
                db.add(_audit_row(user.id if user else None, "admin.login", False, request))
                await db.commit()
                raise HTTPException(
                    status_code=401, detail={"code": "AUTH_FAILED", "message": "用户名或密码错误"}
                )
            user.failed_attempts = 0
            user.locked_until = None
            user.last_login_at = now
            token = secrets.token_urlsafe(32)
            db.add(
                AdminSession(
                    session_hash=_hash_session(token),
                    admin_user_id=user.id,
                    expires_at=now + timedelta(seconds=cfg.admin_session_ttl_seconds),
                )
            )
            db.add(_audit_row(user.id, "admin.login", True, request))
            await db.commit()
            response.set_cookie(
                cfg.admin_session_cookie,
                token,
                max_age=cfg.admin_session_ttl_seconds,
                httponly=True,
                secure=cfg.admin_cookie_secure,
                samesite=cfg.admin_cookie_samesite,
                path="/",
            )

    @app.post("/api/admin/logout", status_code=200)
    async def logout(
        response: Response,
        request: Request,
    ) -> None:
        session_id = request.cookies.get(cfg.admin_session_cookie)
        if session_id:
            async with app.state.database.session() as db:
                row = (
                    await db.execute(
                        select(AdminSession).where(
                            AdminSession.session_hash == _hash_session(session_id)
                        )
                    )
                ).scalar_one_or_none()
                if row:
                    row.revoked_at = datetime.now(UTC)
                    db.add(_audit_row(row.admin_user_id, "admin.logout", True, request))
                    await db.commit()
                else:
                    db.add(_audit_row(None, "admin.logout", False, request))
                    await db.commit()
        else:
            async with app.state.database.session() as db:
                db.add(_audit_row(None, "admin.logout", False, request))
                await db.commit()
        response.delete_cookie(cfg.admin_session_cookie, path="/")

    @app.get("/api/config/frontend")
    async def frontend_config(
        _admin: Annotated[AdminUser, Depends(_current_admin)],
    ) -> dict[str, object]:
        return {"api_base": "", "origins": app.state.settings.cors_origins}

    @app.get("/api/mailboxes")
    async def list_mailboxes(
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> list[dict[str, object]]:
        rows = (
            (
                await db.execute(
                    select(Mailbox).options(selectinload(Mailbox.folders)).order_by(Mailbox.address)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": str(m.id),
                "address": m.address,
                "enabled": m.enabled,
                "proxy_mode": m.proxy_mode,
                "proxy_status": m.proxy_status,
                "connection_state": m.connection_state,
                "last_error_code": m.last_error_code,
                "last_heartbeat_at": m.last_heartbeat_at,
                "folders": [
                    {
                        "name": f.imap_name,
                        "role": f.folder_role,
                        "last_seen_uid": f.last_seen_uid,
                        "uidvalidity": f.uidvalidity,
                        "last_success_at": f.last_success_at,
                    }
                    for f in m.folders
                ],
            }
            for m in rows
        ]

    @app.post("/api/mailboxes", status_code=201)
    async def create_mailbox(
        payload: MailboxRequest,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, str]:
        try:
            imap_host = validate_imap_host(payload.imap_host, cfg.imap_allowed_hosts)
        except ValueError as exc:
            raise HTTPException(400, "IMAP_HOST_NOT_ALLOWED") from exc
        proxy_mode = "socks5" if payload.proxy_mode == "socks5h" else payload.proxy_mode
        proxy_dns_mode = "remote" if payload.proxy_mode == "socks5h" else payload.proxy_dns_mode
        if proxy_mode not in {"direct", "socks5"}:
            raise HTTPException(400, "INVALID_INPUT")
        if not payload.app_password and not payload.app_password_secret_ref:
            raise HTTPException(400, "APP_PASSWORD_REQUIRED")
        if payload.app_password and payload.app_password_secret_ref:
            raise HTTPException(400, "INVALID_INPUT")
        if proxy_mode == "direct" and any(
            (
                payload.proxy_host,
                payload.proxy_port,
                payload.proxy_username,
                payload.proxy_password,
                payload.proxy_password_secret_ref,
                payload.proxy_dns_mode,
            )
        ):
            raise HTTPException(400, "INVALID_INPUT")
        if proxy_mode == "socks5" and not (
            payload.proxy_host
            and payload.proxy_port
            and proxy_dns_mode in {"local", "remote"}
        ):
            raise HTTPException(400, "INVALID_INPUT")
        if (
            payload.proxy_host
            and cfg.proxy_allowed_hosts
            and payload.proxy_host.lower() not in {item.lower() for item in cfg.proxy_allowed_hosts}
        ):
            raise HTTPException(400, "PROXY_HOST_NOT_ALLOWED")
        if payload.proxy_password and payload.proxy_password_secret_ref:
            raise HTTPException(400, "INVALID_INPUT")
        cipher = MailboxCredentialCipher(cfg.mailbox_credentials_fernet_key)
        app_password_encrypted = (
            cipher.encrypt(payload.app_password.get_secret_value())
            if payload.app_password
            else None
        )
        proxy_password_encrypted = (
            cipher.encrypt(payload.proxy_password.get_secret_value())
            if payload.proxy_password
            else None
        )
        mailbox = Mailbox(
            address=payload.address,
            imap_host=imap_host,
            imap_port=payload.imap_port,
            username=payload.username,
            app_password_secret_ref=(
                "DATABASE_ENCRYPTED" if app_password_encrypted else payload.app_password_secret_ref
            ),
            app_password_encrypted=app_password_encrypted,
            proxy_mode=proxy_mode,
            proxy_host=payload.proxy_host,
            proxy_port=payload.proxy_port,
            proxy_username=payload.proxy_username,
            proxy_password_encrypted=proxy_password_encrypted,
            proxy_password_secret_ref=payload.proxy_password_secret_ref,
            proxy_dns_mode=proxy_dns_mode,
            proxy_status="direct" if proxy_mode == "direct" else "waiting_for_proxy",
        )
        db.add(mailbox)
        await db.flush()
        for name in payload.folders:
            db.add(
                MailboxFolder(
                    mailbox_id=mailbox.id,
                    imap_name=name,
                    folder_role="inbox"
                    if name.upper() == "INBOX"
                    else "spam"
                    if "SPAM" in name.upper()
                    else "other",
                )
            )
        db.add(
            _audit_row(
                _admin.id,
                "mailbox.create",
                True,
                request,
                resource_type="mailbox",
                resource_id=str(mailbox.id),
            )
        )
        await db.commit()
        return {"id": str(mailbox.id)}

    @app.patch("/api/mailboxes/{mailbox_id}")
    async def patch_mailbox(
        mailbox_id: UUID,
        payload: MailboxPatch,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, str]:
        mailbox = await db.get(Mailbox, mailbox_id)
        if not mailbox:
            raise HTTPException(404, "NOT_FOUND")
        data = payload.model_dump(exclude_none=True)
        if payload.app_password and payload.app_password_secret_ref:
            raise HTTPException(400, "INVALID_INPUT")
        cipher = None
        if payload.app_password or payload.proxy_password:
            cipher = MailboxCredentialCipher(cfg.mailbox_credentials_fernet_key)
        if payload.app_password:
            if cipher is None:
                raise HTTPException(500, "CREDENTIAL_CIPHER_UNAVAILABLE")
            data["app_password_encrypted"] = cipher.encrypt(payload.app_password.get_secret_value())
            data["app_password_secret_ref"] = "DATABASE_ENCRYPTED"
        data.pop("app_password", None)
        if payload.proxy_password:
            if cipher is None:
                raise HTTPException(500, "CREDENTIAL_CIPHER_UNAVAILABLE")
            data["proxy_password_encrypted"] = cipher.encrypt(
                payload.proxy_password.get_secret_value()
            )
            data["proxy_password_secret_ref"] = "DATABASE_ENCRYPTED"
        data.pop("proxy_password", None)
        if data.get("proxy_mode") == "socks5h":
            data["proxy_mode"] = "socks5"
            data["proxy_dns_mode"] = "remote"
        if data.get("proxy_mode") == "direct" and any(
            data.get(k)
            for k in (
                "proxy_host",
                "proxy_port",
                "proxy_username",
                "proxy_password_secret_ref",
                "proxy_dns_mode",
            )
        ):
            raise HTTPException(400, "INVALID_INPUT")
        if data.get("proxy_mode") == "socks5" and data.get("proxy_dns_mode") not in {
            None,
            "local",
            "remote",
        }:
            raise HTTPException(400, "INVALID_INPUT")
        for key, value in data.items():
            setattr(mailbox, key, value)
        db.add(
            _audit_row(
                _admin.id,
                "mailbox.update",
                True,
                request,
                resource_type="mailbox",
                resource_id=str(mailbox_id),
                metadata={"fields": sorted(data)},
            )
        )
        await db.commit()
        return {"status": "updated"}

    @app.post("/api/mailboxes/{mailbox_id}/start")
    async def start_mailbox(
        mailbox_id: UUID,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, str]:
        mailbox = await db.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise HTTPException(404, "NOT_FOUND")
        mailbox.enabled = True
        db.add(
            _audit_row(
                _admin.id,
                "mailbox.start",
                True,
                request,
                resource_type="mailbox",
                resource_id=str(mailbox_id),
            )
        )
        await db.commit()
        return {"status": "started"}

    @app.post("/api/mailboxes/{mailbox_id}/stop")
    async def stop_mailbox(
        mailbox_id: UUID,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, str]:
        mailbox = await db.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise HTTPException(404, "NOT_FOUND")
        mailbox.enabled = False
        db.add(
            _audit_row(
                _admin.id,
                "mailbox.stop",
                True,
                request,
                resource_type="mailbox",
                resource_id=str(mailbox_id),
            )
        )
        await db.commit()
        return {"status": "stopped"}

    @app.delete("/api/mailboxes/{mailbox_id}", status_code=200)
    async def delete_mailbox(
        mailbox_id: UUID,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> None:
        mailbox = await db.get(Mailbox, mailbox_id)
        if mailbox is None:
            raise HTTPException(404, "NOT_FOUND")
        db.add(
            _audit_row(
                _admin.id,
                "mailbox.delete",
                True,
                request,
                resource_type="mailbox",
                resource_id=str(mailbox_id),
            )
        )
        await db.delete(mailbox)
        await db.commit()

    @app.get("/api/mailboxes/{mailbox_id}/status")
    async def mailbox_status(
        mailbox_id: UUID,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, object]:
        mailbox = (
            await db.execute(
                select(Mailbox)
                .options(selectinload(Mailbox.folders))
                .where(Mailbox.id == mailbox_id)
            )
        ).scalar_one_or_none()
        if not mailbox:
            raise HTTPException(404, "NOT_FOUND")
        return {
            "id": str(mailbox.id),
            "proxy_mode": mailbox.proxy_mode,
            "proxy_status": mailbox.proxy_status,
            "connection_state": mailbox.connection_state,
            "last_error_code": mailbox.last_error_code,
            "folders": [
                {
                    "id": str(f.id),
                    "name": f.imap_name,
                    "uidvalidity": f.uidvalidity,
                    "uidnext": f.uidnext,
                    "last_seen_uid": f.last_seen_uid,
                    "sync_state": f.sync_state,
                    "last_success_at": f.last_success_at,
                    "last_error_code": f.last_error_code,
                }
                for f in mailbox.folders
            ],
        }

    @app.get("/api/mailboxes/{mailbox_id}/messages")
    async def messages(
        mailbox_id: UUID,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
        folder: str = "all",
        q: str | None = None,
        recipient: str | None = None,
        page: int = 1,
        page_size: int = 10,
    ) -> dict[str, object]:
        if q and len(q) > cfg.max_query_length:
            raise HTTPException(400, "QUERY_TOO_LONG")
        if recipient and len(recipient) > cfg.max_query_length:
            raise HTTPException(400, "RECIPIENT_QUERY_TOO_LONG")
        page = max(page, 1)
        page_size = min(max(page_size, 1), cfg.max_page_size)
        stmt = (
            select(Message)
            .options(selectinload(Message.codes))
            .where(Message.mailbox_id == mailbox_id)
        )
        if q:
            escaped_q = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            stmt = stmt.where(
                or_(
                    Message.subject.ilike(f"%{escaped_q}%", escape="\\"),
                    Message.from_address.ilike(f"%{escaped_q}%", escape="\\"),
                    Message.text_body.ilike(f"%{escaped_q}%", escape="\\"),
                )
            )
        if recipient and recipient.strip():
            normalized_recipient = recipient.strip().lower().replace(".", "")
            normalized_recipients = func.replace(
                func.lower(sql_cast(Message.to_addresses, Text)), ".", ""
            )
            stmt = stmt.where(
                func.strpos(normalized_recipients, f'"{normalized_recipient}"') > 0
            )
        if folder in {"inbox", "spam"}:
            stmt = (
                stmt.join(MessageLabel)
                .join(MailboxFolder)
                .where(MailboxFolder.folder_role == folder, MessageLabel.is_present.is_(True))
            )
        total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
        rows = (
            (
                await db.execute(
                    stmt.order_by(Message.received_at.desc().nullslast())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            )
            .scalars()
            .all()
        )
        return {
            "page": page,
            "page_size": page_size,
            "total": total,
            "items": [
                {
                    "id": str(m.id),
                    "subject": m.subject,
                    "from_address": m.from_address,
                    "to_addresses": m.to_addresses or [],
                    "received_at": m.received_at,
                    "has_attachments": m.has_attachments,
                    "has_code": bool(m.codes),
                    "preview": (m.text_body or m.html_body_sanitized or "")[:240],
                }
                for m in rows
            ],
        }

    @app.get("/api/messages/{message_id}")
    async def message_detail(
        message_id: UUID,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, object]:
        m = (
            await db.execute(
                select(Message)
                .options(selectinload(Message.labels), selectinload(Message.codes))
                .where(Message.id == message_id)
            )
        ).scalar_one_or_none()
        if not m:
            raise HTTPException(404, "NOT_FOUND")
        db.add(
            _audit_row(
                _admin.id,
                "message.view",
                True,
                request,
                resource_type="message",
                resource_id=str(message_id),
            )
        )
        await db.commit()
        return {
            "id": str(m.id),
            "subject": m.subject,
            "from_address": m.from_address,
            "to_addresses": m.to_addresses,
            "cc_addresses": m.cc_addresses,
            "received_at": m.received_at,
            "text_body": m.text_body,
            "html_body_sanitized": m.html_body_sanitized,
            "labels": [
                {"folder_uid": x.folder_uid, "folder_id": str(x.mailbox_folder_id)}
                for x in m.labels
            ],
            "codes": [
                {"value": c.code_value, "type": c.code_type, "confidence": c.confidence}
                for c in m.codes
            ],
        }

    @app.get("/api/mailboxes/{mailbox_id}/codes")
    async def codes(
        mailbox_id: UUID,
        response: Response,
        request: Request,
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
        include_value: bool = False,
    ) -> list[dict[str, object]]:
        response.headers["Cache-Control"] = "no-store"
        stmt = (
            select(VerificationCode, Message)
            .join(Message, Message.id == VerificationCode.message_id)
            .where(Message.mailbox_id == mailbox_id)
            .order_by(VerificationCode.extracted_at.desc())
        )
        db.add(
            _audit_row(
                _admin.id,
                "codes.view",
                True,
                request,
                resource_type="mailbox",
                resource_id=str(mailbox_id),
            )
        )
        await db.commit()
        return [
            {
                "message_id": str(m.id),
                "code_type": c.code_type,
                "value": c.code_value if include_value else "******",
                "extracted_at": c.extracted_at,
            }
            for c, m in (await db.execute(stmt)).all()
        ]

    @app.get("/api/sync/status")
    async def sync_status(
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> list[dict[str, object]]:
        rows = (
            await db.execute(
                select(MailboxFolder, Mailbox.address).join(
                    Mailbox, Mailbox.id == MailboxFolder.mailbox_id
                )
            )
        ).all()
        return [
            {
                "mailbox": address,
                "folder": f.imap_name,
                "sync_state": f.sync_state,
                "uidvalidity": f.uidvalidity,
                "uidnext": f.uidnext,
                "last_seen_uid": f.last_seen_uid,
                "last_success_at": f.last_success_at,
                "last_error_code": f.last_error_code,
            }
            for f, address in rows
        ]

    @app.get("/api/admin/audit-logs")
    async def audit_logs(
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
        limit: int = 100,
    ) -> list[dict[str, object]]:
        limit = min(max(limit, 1), 500)
        rows = (
            await db.execute(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit))
        ).scalars().all()
        return [
            {
                "id": str(row.id),
                "admin_user_id": str(row.admin_user_id) if row.admin_user_id else None,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "success": row.success,
                "remote_addr": row.remote_addr,
                "metadata": row.metadata_json,
                "created_at": row.created_at,
            }
            for row in rows
        ]

    @app.get("/api/retention/status")
    async def retention_status(
        _admin: Annotated[AdminUser, Depends(_current_admin)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
    ) -> dict[str, object]:
        run = (
            await db.execute(select(RetentionRun).order_by(RetentionRun.started_at.desc()).limit(1))
        ).scalar_one_or_none()
        return (
            {}
            if run is None
            else {
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "cutoff_at": run.cutoff_at,
                "scanned_count": run.scanned_count,
                "deleted_count": run.deleted_count,
                "failed_count": run.failed_count,
                "status": run.status,
            }
        )

    return app


app = create_app()
