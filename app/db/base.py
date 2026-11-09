"""PostgreSQL ORM 模型：邮箱、IMAP 游标、邮件正文和管理员会话。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB}


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AdminUser(UUIDMixin, Timestamps, Base):
    __tablename__ = "admin_users"
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminSession(UUIDMixin, Base):
    __tablename__ = "admin_sessions"
    session_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    admin_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (Index("ix_admin_sessions_expiry", "expires_at"),)


class AuditLog(UUIDMixin, Base):
    __tablename__ = "audit_logs"
    admin_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str | None] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    remote_addr: Mapped[str | None] = mapped_column(String(128))
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (Index("ix_audit_logs_created_at", "created_at"),)


class Mailbox(UUIDMixin, Timestamps, Base):
    __tablename__ = "mailboxes"
    address: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    imap_host: Mapped[str] = mapped_column(String(255), nullable=False)
    imap_port: Mapped[int] = mapped_column(Integer, default=993, nullable=False)
    username: Mapped[str] = mapped_column(String(320), nullable=False)
    app_password_secret_ref: Mapped[str | None] = mapped_column(String(512))
    app_password_encrypted: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    proxy_mode: Mapped[str] = mapped_column(String(16), default="direct", nullable=False)
    proxy_host: Mapped[str | None] = mapped_column(String(255))
    proxy_port: Mapped[int | None] = mapped_column(Integer)
    proxy_username: Mapped[str | None] = mapped_column(String(320))
    proxy_password_secret_ref: Mapped[str | None] = mapped_column(String(512))
    proxy_password_encrypted: Mapped[str | None] = mapped_column(Text)
    proxy_dns_mode: Mapped[str | None] = mapped_column(String(16))
    proxy_status: Mapped[str] = mapped_column(String(32), default="direct", nullable=False)
    connection_state: Mapped[str] = mapped_column(String(32), default="stopped", nullable=False)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    folders: Mapped[list[MailboxFolder]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (Index("ix_mailboxes_enabled", "enabled"),)


class MailboxFolder(UUIDMixin, Timestamps, Base):
    __tablename__ = "mailbox_folders"
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    imap_name: Mapped[str] = mapped_column(String(512), nullable=False)
    folder_role: Mapped[str] = mapped_column(String(32), default="other", nullable=False)
    monitor_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    uidvalidity: Mapped[int | None] = mapped_column(Integer)
    uidnext: Mapped[int | None] = mapped_column(Integer)
    last_seen_uid: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    baseline_uid: Mapped[int | None] = mapped_column(Integer)
    sync_state: Mapped[str] = mapped_column(String(32), default="baseline_required", nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("mailbox_id", "imap_name", name="uq_mailbox_folder_name"),
        Index("ix_folder_sync", "mailbox_id", "monitor_enabled"),
    )


class Message(UUIDMixin, Timestamps, Base):
    __tablename__ = "messages"
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    stable_message_key: Mapped[str] = mapped_column(String(512), nullable=False)
    message_id_header: Mapped[str | None] = mapped_column(String(998))
    thread_key: Mapped[str | None] = mapped_column(String(512))
    from_address: Mapped[str | None] = mapped_column(Text)
    to_addresses: Mapped[list[str] | None] = mapped_column(JSONB)
    cc_addresses: Mapped[list[str] | None] = mapped_column(JSONB)
    subject: Mapped[str | None] = mapped_column(Text)
    internal_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    text_body: Mapped[str | None] = mapped_column(Text)
    html_body_sanitized: Mapped[str | None] = mapped_column(Text)
    has_attachments: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    body_hash: Mapped[str | None] = mapped_column(String(64))
    parser_state: Mapped[str] = mapped_column(String(32), default="parsed", nullable=False)
    parser_error_code: Mapped[str | None] = mapped_column(String(64))
    labels: Mapped[list[MessageLabel]] = relationship(cascade="all, delete-orphan")
    codes: Mapped[list[VerificationCode]] = relationship(cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("mailbox_id", "stable_message_key", name="uq_message_stable_key"),
        Index("ix_messages_stored_at", "stored_at"),
        Index("ix_messages_subject", "subject"),
        Index("ix_messages_message_id_header", "message_id_header"),
    )


class MessageLabel(Base):
    __tablename__ = "message_labels"
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    mailbox_folder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mailbox_folders.id", ondelete="CASCADE"), primary_key=True
    )
    folder_uid: Mapped[int] = mapped_column(Integer, nullable=False)
    uidvalidity_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    is_present: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "mailbox_folder_id", "folder_uid", "uidvalidity_snapshot", name="uq_folder_uid"
        ),
        Index("ix_labels_folder_present", "mailbox_folder_id", "is_present"),
    )


class VerificationCode(UUIDMixin, Base):
    __tablename__ = "verification_codes"
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    code_value: Mapped[str] = mapped_column(String(128), nullable=False)
    code_type: Mapped[str] = mapped_column(String(32), default="numeric", nullable=False)
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    __table_args__ = (UniqueConstraint("message_id", "code_value", name="uq_message_code"),)


class RetentionRun(UUIDMixin, Base):
    __tablename__ = "retention_runs"
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cutoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    scanned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running", nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))


class ErrorEvent(UUIDMixin, Base):
    __tablename__ = "error_events"
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mailboxes.id", ondelete="SET NULL")
    )
    error_code: Mapped[str] = mapped_column(String(64), nullable=False)
    retryable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    message_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# 旧模块导入兼容别名；新业务只使用上述模型。
OAuthCredential = AdminSession
GmailSyncState = MailboxFolder
InboundEvent = ErrorEvent
SyncRequest = ErrorEvent
GmailMessage = Message
NotificationEndpoint = ErrorEvent
NotificationDelivery = ErrorEvent
