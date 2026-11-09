"""创建当前 IMAP + PostgreSQL schema，并隔离 0006 的历史表。

0006 创建的是已经废弃的 Gmail API/Outbox schema，其中 ``mailboxes`` 和
``error_events`` 与当前模型同名。升级时先把这些旧表重命名为 ``legacy_*``，
保留原数据和外键关系，再使用 Alembic 结构化操作创建当前表。每个 DDL
操作都是独立语句，避免 asyncpg prepared statement 不接受多条 SQL 的问题。
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_imap_postgres_schema"
down_revision = "0006_schema_repair"
branch_labels = None
depends_on = None

_LEGACY_TABLES: tuple[tuple[str, str, str | None], ...] = (
    ("mailboxes", "legacy_mailboxes", "email"),
    ("oauth_credentials", "legacy_oauth_credentials", None),
    ("gmail_sync_states", "legacy_gmail_sync_states", None),
    ("inbound_events", "legacy_inbound_events", None),
    ("sync_requests", "legacy_sync_requests", None),
    ("gmail_messages", "legacy_gmail_messages", None),
    ("notification_endpoints", "legacy_notification_endpoints", None),
    ("notification_deliveries", "legacy_notification_deliveries", None),
    ("error_events", "legacy_error_events", "entity_type"),
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in _inspector().get_table_names()


def _column_names(table_name: str) -> set[str]:
    return {column["name"] for column in _inspector().get_columns(table_name)}


def _rename_legacy_tables() -> None:
    for current_name, legacy_name, marker in _LEGACY_TABLES:
        if not _table_exists(current_name):
            continue
        if marker is not None and marker not in _column_names(current_name):
            continue
        if _table_exists(legacy_name):
            raise RuntimeError(
                f"cannot migrate {current_name}: destination {legacy_name} already exists"
            )
        op.rename_table(current_name, legacy_name)


def _create_table_if_missing(
    name: str,
    *columns: sa.Column[object],
    constraints: Iterable[sa.Constraint] = (),
) -> None:
    if _table_exists(name):
        return
    op.create_table(name, *columns, *constraints)


def _create_index_if_missing(
    name: str, table_name: str, columns: list[str], *, unique: bool = False
) -> None:
    existing = {index["name"] for index in _inspector().get_indexes(table_name)}
    if name not in existing:
        op.create_index(name, table_name, columns, unique=unique)


def _create_current_schema() -> None:
    _create_table_if_missing(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("username", sa.String(128), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("failed_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        constraints=(sa.UniqueConstraint("username", name="uq_admin_users_username"),),
    )
    _create_table_if_missing(
        "admin_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("session_hash", sa.String(128), nullable=False),
        sa.Column("admin_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        constraints=(
            sa.UniqueConstraint("session_hash", name="uq_admin_sessions_hash"),
            sa.ForeignKeyConstraint(
                ["admin_user_id"],
                ["admin_users.id"],
                ondelete="CASCADE",
                name="fk_admin_sessions_user",
            ),
        ),
    )
    _create_index_if_missing("ix_admin_sessions_expiry", "admin_sessions", ["expires_at"])

    _create_table_if_missing(
        "mailboxes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("imap_host", sa.String(255), nullable=False),
        sa.Column("imap_port", sa.Integer(), nullable=False, server_default=sa.text("993")),
        sa.Column("username", sa.String(320), nullable=False),
        sa.Column("app_password_secret_ref", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("proxy_mode", sa.String(16), nullable=False, server_default=sa.text("'direct'")),
        sa.Column("proxy_host", sa.String(255)),
        sa.Column("proxy_port", sa.Integer()),
        sa.Column("proxy_username", sa.String(320)),
        sa.Column("proxy_password_secret_ref", sa.String(512)),
        sa.Column("proxy_dns_mode", sa.String(16)),
        sa.Column(
            "proxy_status", sa.String(32), nullable=False, server_default=sa.text("'direct'")
        ),
        sa.Column(
            "connection_state", sa.String(32), nullable=False, server_default=sa.text("'stopped'")
        ),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        constraints=(sa.UniqueConstraint("address", name="uq_mailboxes_address"),),
    )
    _create_index_if_missing("ix_mailboxes_enabled", "mailboxes", ["enabled"])

    _create_table_if_missing(
        "mailbox_folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mailbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("imap_name", sa.String(512), nullable=False),
        sa.Column("folder_role", sa.String(32), nullable=False, server_default=sa.text("'other'")),
        sa.Column("monitor_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("uidvalidity", sa.Integer()),
        sa.Column("uidnext", sa.Integer()),
        sa.Column("last_seen_uid", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("baseline_uid", sa.Integer()),
        sa.Column(
            "sync_state",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'baseline_required'"),
        ),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_at", sa.DateTime(timezone=True)),
        sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        constraints=(
            sa.UniqueConstraint("mailbox_id", "imap_name", name="uq_mailbox_folder_name"),
            sa.ForeignKeyConstraint(
                ["mailbox_id"],
                ["mailboxes.id"],
                ondelete="CASCADE",
                name="fk_mailbox_folders_mailbox",
            ),
        ),
    )
    _create_index_if_missing("ix_folder_sync", "mailbox_folders", ["mailbox_id", "monitor_enabled"])

    _create_table_if_missing(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("mailbox_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stable_message_key", sa.String(512), nullable=False),
        sa.Column("message_id_header", sa.String(998)),
        sa.Column("thread_key", sa.String(512)),
        sa.Column("from_address", sa.Text()),
        sa.Column("to_addresses", postgresql.JSONB()),
        sa.Column("cc_addresses", postgresql.JSONB()),
        sa.Column("subject", sa.Text()),
        sa.Column("internal_date", sa.DateTime(timezone=True)),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column(
            "stored_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")
        ),
        sa.Column("text_body", sa.Text()),
        sa.Column("html_body_sanitized", sa.Text()),
        sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("body_hash", sa.String(64)),
        sa.Column(
            "parser_state", sa.String(32), nullable=False, server_default=sa.text("'parsed'")
        ),
        sa.Column("parser_error_code", sa.String(64)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        constraints=(
            sa.UniqueConstraint("mailbox_id", "stable_message_key", name="uq_message_stable_key"),
            sa.ForeignKeyConstraint(
                ["mailbox_id"], ["mailboxes.id"], ondelete="CASCADE", name="fk_messages_mailbox"
            ),
        ),
    )
    _create_index_if_missing("ix_messages_stored_at", "messages", ["stored_at"])
    _create_index_if_missing("ix_messages_subject", "messages", ["subject"])
    _create_index_if_missing("ix_messages_message_id_header", "messages", ["message_id_header"])

    _create_table_if_missing(
        "message_labels",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "mailbox_folder_id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False
        ),
        sa.Column("folder_uid", sa.Integer(), nullable=False),
        sa.Column("uidvalidity_snapshot", sa.Integer(), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("is_present", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        constraints=(
            sa.ForeignKeyConstraint(
                ["message_id"],
                ["messages.id"],
                ondelete="CASCADE",
                name="fk_message_labels_message",
            ),
            sa.ForeignKeyConstraint(
                ["mailbox_folder_id"],
                ["mailbox_folders.id"],
                ondelete="CASCADE",
                name="fk_message_labels_folder",
            ),
            sa.UniqueConstraint(
                "mailbox_folder_id", "folder_uid", "uidvalidity_snapshot", name="uq_folder_uid"
            ),
        ),
    )
    _create_index_if_missing(
        "ix_labels_folder_present", "message_labels", ["mailbox_folder_id", "is_present"]
    )

    _create_table_if_missing(
        "verification_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_value", sa.String(128), nullable=False),
        sa.Column("code_type", sa.String(32), nullable=False, server_default=sa.text("'numeric'")),
        sa.Column(
            "extracted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        constraints=(
            sa.ForeignKeyConstraint(
                ["message_id"], ["messages.id"], ondelete="CASCADE", name="fk_codes_message"
            ),
            sa.UniqueConstraint("message_id", "code_value", name="uq_message_code"),
        ),
    )

    _create_table_if_missing(
        "retention_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("cutoff_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scanned_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("error_code", sa.String(64)),
    )

    _create_table_if_missing(
        "error_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column("component", sa.String(64), nullable=False),
        sa.Column("mailbox_id", postgresql.UUID(as_uuid=True)),
        sa.Column("error_code", sa.String(64), nullable=False),
        sa.Column("retryable", sa.Boolean(), nullable=False),
        sa.Column("message_redacted", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        constraints=(
            sa.ForeignKeyConstraint(
                ["mailbox_id"],
                ["mailboxes.id"],
                ondelete="SET NULL",
                name="fk_error_events_mailbox",
            ),
        ),
    )
    # 0006 的 legacy_error_events 索引保留原名称；当前索引使用独立命名空间，
    # 避免 PostgreSQL schema 级 relation 名称冲突。
    _create_index_if_missing(
        "ix_imap_error_events_component_code_created",
        "error_events",
        ["component", "error_code", "created_at"],
    )
    _create_index_if_missing(
        "ix_imap_error_events_mailbox_created",
        "error_events",
        ["mailbox_id", "created_at"],
    )


def upgrade() -> None:
    _rename_legacy_tables()
    _create_current_schema()


def _drop_if_exists(table_name: str) -> None:
    if _table_exists(table_name):
        op.drop_table(table_name)


def _restore_legacy_tables() -> None:
    for current_name, legacy_name, _marker in _LEGACY_TABLES:
        if _table_exists(legacy_name) and not _table_exists(current_name):
            op.rename_table(legacy_name, current_name)


def downgrade() -> None:
    for table_name in (
        "error_events",
        "retention_runs",
        "verification_codes",
        "message_labels",
        "messages",
        "mailbox_folders",
        "mailboxes",
        "admin_sessions",
        "admin_users",
    ):
        _drop_if_exists(table_name)
    _restore_legacy_tables()
