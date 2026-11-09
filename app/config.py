"""IMAP 邮箱监控服务配置。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_name: str = "imap-mail-monitor"
    app_env: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://localhost:5432/imap_mail"
    database_ssl_mode: Literal["disable", "require", "verify-full"] = "disable"
    database_ssl_ca: str = ""
    database_pool_size: int = Field(5, ge=1, le=100)
    database_max_overflow: int = Field(10, ge=0, le=100)
    database_connect_timeout_seconds: float = Field(5.0, gt=0, le=60)
    admin_session_cookie: str = "admin_session"
    admin_username: str = ""
    admin_password: str = ""
    docs_username: str = ""
    docs_password: str = ""
    external_api_key: str = ""
    admin_session_ttl_seconds: int = Field(8 * 3600, ge=300, le=7 * 86400)
    admin_cookie_secure: bool = True
    admin_cookie_samesite: Literal["strict", "lax", "none"] = "strict"
    admin_login_max_attempts: int = Field(5, ge=1, le=20)
    admin_login_window_seconds: int = Field(300, ge=30, le=3600)
    admin_lockout_seconds: int = Field(900, ge=60, le=86400)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])
    cors_allow_credentials: bool = True
    imap_default_host: str = "imap.gmail.com"
    imap_default_port: int = Field(993, ge=1, le=65535)
    imap_connect_timeout_seconds: float = Field(15.0, gt=0, le=120)
    imap_idle_timeout_seconds: int = Field(300, ge=30, le=3600)
    imap_reconnect_max_seconds: float = Field(300.0, gt=1, le=3600)
    proxy_check_timeout_seconds: float = Field(5.0, gt=0, le=60)
    proxy_backoff_base_seconds: float = Field(2.0, gt=0, le=60)
    proxy_backoff_max_seconds: float = Field(300.0, gt=1, le=3600)
    message_retention_days: int = Field(30, ge=1, le=365)
    cleanup_interval_seconds: int = Field(3600, ge=60, le=86400)
    max_page_size: int = Field(100, ge=1, le=500)
    max_query_length: int = Field(200, ge=16, le=2000)
    message_body_max_chars: int = Field(1_000_000, ge=1024, le=5_000_000)
    imap_allowed_hosts: list[str] = Field(default_factory=lambda: ["imap.gmail.com"])
    proxy_allowed_hosts: list[str] = Field(default_factory=list)
    secret_ref_prefix: str = "SECRET_REF:"
    mailbox_credentials_fernet_key: str = ""

    @field_validator("database_url")
    @classmethod
    def non_blank_database_url(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("DATABASE_URL cannot be blank")
        return value

    @field_validator("imap_allowed_hosts", "proxy_allowed_hosts", mode="before")
    @classmethod
    def parse_allowed_hosts(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip().lower() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return []

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, list):
            return [str(item) for item in value]
        return []

    @model_validator(mode="after")
    def production_cookie_policy(self) -> Settings:
        def is_placeholder(value: str) -> bool:
            upper = value.upper()
            return any(
                marker in upper
                for marker in ("REPLACE_WITH", "CHANGE_ME", "YOUR_", "PLACEHOLDER")
            )

        if self.app_env == "production":
            self.admin_cookie_secure = True
            if self.admin_cookie_samesite != "strict":
                raise ValueError("production requires ADMIN_COOKIE_SAMESITE=strict")
            if "*" in self.cors_origins:
                raise ValueError("CORS_ORIGINS cannot contain * in production")
            if not self.cors_origins:
                raise ValueError("CORS_ORIGINS is required in production")
            if (
                not self.database_url
                or self.database_url == "postgresql+asyncpg://localhost:5432/imap_mail"
                or is_placeholder(self.database_url)
            ):
                raise ValueError("DATABASE_URL is required in production")
            if not self.admin_username or not self.admin_password:
                raise ValueError("ADMIN_USERNAME and ADMIN_PASSWORD are required in production")
            if len(self.admin_password) < 12 or is_placeholder(self.admin_password):
                raise ValueError("ADMIN_PASSWORD must be at least 12 characters")
            if (
                not self.docs_username
                or not self.docs_password
                or is_placeholder(self.docs_password)
            ):
                raise ValueError("DOCS_USERNAME and DOCS_PASSWORD are required in production")
            if not self.external_api_key or is_placeholder(self.external_api_key):
                raise ValueError("EXTERNAL_API_KEY is required in production")
            if not self.mailbox_credentials_fernet_key or is_placeholder(
                self.mailbox_credentials_fernet_key
            ):
                raise ValueError("MAILBOX_CREDENTIALS_FERNET_KEY is required in production")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def clear_settings_cache() -> None:
    get_settings.cache_clear()
