"""前向兼容的完整 schema 修复迁移。

仓库早期没有把 0001--0004 的历史迁移提交进来，0005 也只包含
``IF EXISTS`` 的增量字段。因此 0005 单独从空库执行时不会创建任何表。
本迁移挂在 0005 之后，使用幂等 DDL 补齐当前 ORM 所需的完整结构：

* 旧环境如果已经执行过 0005，只需升级到 0006；
* 新环境执行 ``upgrade head`` 时，0005 先空操作，0006 创建全部表；
* 已存在的表只补列、索引和约束，不删除数据，也不重写历史迁移。

降级保持保守：不删除业务表或数据。重新执行 upgrade 仍然幂等，便于在
无法确认历史数据库来源时安全前向修复。
"""

from alembic import op

revision = "0006_schema_repair"
down_revision = "0005_stage5_email_content_outbox"
branch_labels = None
depends_on = None


def _execute(sql: str) -> None:
    op.execute(sql)


def upgrade() -> None:
    # CREATE TABLE IF NOT EXISTS 负责全新数据库；后续 ALTER TABLE 负责旧环境中
    # 可能缺少的列。UUID 由应用生成，不依赖 uuid-ossp/pgcrypto 扩展。
    tables = (
        """
        CREATE TABLE IF NOT EXISTS mailboxes (
            id UUID PRIMARY KEY,
            email VARCHAR(320) NOT NULL UNIQUE,
            provider VARCHAR(32) NOT NULL DEFAULT 'gmail',
            status VARCHAR(32) NOT NULL DEFAULT 'pending_auth',
            gmail_user_id VARCHAR(255), watch_topic TEXT, watch_subscription TEXT,
            watch_expiration_at TIMESTAMPTZ, last_watch_at TIMESTAMPTZ,
            last_push_at TIMESTAMPTZ, last_sync_at TIMESTAMPTZ,
            last_error_code VARCHAR(64), last_error_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS oauth_credentials (
            mailbox_id UUID PRIMARY KEY REFERENCES mailboxes(id) ON DELETE CASCADE,
            client_id_ref VARCHAR(128) NOT NULL,
            scopes JSONB NOT NULL DEFAULT '{}'::jsonb,
            refresh_token_ciphertext TEXT NOT NULL,
            refresh_token_key_version VARCHAR(32) NOT NULL,
            token_last_refreshed_at TIMESTAMPTZ, token_last_error VARCHAR(64),
            reauth_required_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gmail_sync_states (
            mailbox_id UUID PRIMARY KEY REFERENCES mailboxes(id) ON DELETE CASCADE,
            cursor_history_id VARCHAR(64), baseline_history_id VARCHAR(64),
            last_observed_history_id VARCHAR(64),
            mode VARCHAR(32) NOT NULL DEFAULT 'baseline_required',
            recovery_started_at TIMESTAMPTZ, recovery_completed_at TIMESTAMPTZ,
            last_compensation_at TIMESTAMPTZ, consecutive_failures INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS inbound_events (
            id UUID PRIMARY KEY,
            provider VARCHAR(32) NOT NULL DEFAULT 'google_pubsub',
            pubsub_message_id VARCHAR(255) NOT NULL,
            subscription TEXT NOT NULL, mailbox_id UUID REFERENCES mailboxes(id),
            observed_history_id VARCHAR(64), published_at TIMESTAMPTZ,
            received_at TIMESTAMPTZ NOT NULL, payload_hash VARCHAR(64) NOT NULL,
            status VARCHAR(32) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_inbound_events_provider_message UNIQUE (provider, pubsub_message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sync_requests (
            id UUID PRIMARY KEY,
            mailbox_id UUID NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
            observed_history_id VARCHAR(64) NOT NULL, reason VARCHAR(32) NOT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'queued', attempt_count INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMPTZ, last_error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_sync_requests_mailbox_history_reason UNIQUE (mailbox_id, observed_history_id, reason)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS gmail_messages (
            id UUID PRIMARY KEY,
            mailbox_id UUID NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
            gmail_message_id VARCHAR(128) NOT NULL, thread_id VARCHAR(128), history_id VARCHAR(64),
            label_ids JSONB, from_address TEXT, to_addresses JSONB, cc_addresses JSONB,
            reply_to_addresses JSONB, subject TEXT, internal_date TIMESTAMPTZ,
            received_at TIMESTAMPTZ, body_text TEXT, body_html TEXT, attachments JSONB,
            attachment_count INTEGER NOT NULL DEFAULT 0, parse_attempt_count INTEGER NOT NULL DEFAULT 0,
            parse_error TEXT, parsed_at TIMESTAMPTZ, raw_headers JSONB, content_hash VARCHAR(64),
            state VARCHAR(32) NOT NULL DEFAULT 'discovered', fetch_attempt_count INTEGER NOT NULL DEFAULT 0,
            next_fetch_at TIMESTAMPTZ, last_error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_gmail_messages_mailbox_message UNIQUE (mailbox_id, gmail_message_id)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_endpoints (
            id UUID PRIMARY KEY, name VARCHAR(128) NOT NULL UNIQUE, url TEXT NOT NULL,
            secret_ciphertext TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT true,
            timeout_ms INTEGER NOT NULL DEFAULT 10000, max_attempts INTEGER NOT NULL DEFAULT 5,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS notification_deliveries (
            id UUID PRIMARY KEY,
            message_id UUID NOT NULL REFERENCES gmail_messages(id) ON DELETE CASCADE,
            endpoint_id UUID NOT NULL REFERENCES notification_endpoints(id) ON DELETE CASCADE,
            event_type VARCHAR(64) NOT NULL DEFAULT 'gmail.message.received',
            schema_version VARCHAR(16) NOT NULL DEFAULT '1', payload_json JSONB,
            idempotency_key VARCHAR(255) NOT NULL, state VARCHAR(32) NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0, next_attempt_at TIMESTAMPTZ,
            last_http_status INTEGER, last_error_code VARCHAR(64), last_response_hash VARCHAR(64),
            delivered_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(), updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_notification_deliveries_message_endpoint_event UNIQUE (message_id, endpoint_id, event_type),
            CONSTRAINT uq_notification_deliveries_idempotency UNIQUE (idempotency_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS error_events (
            id BIGSERIAL PRIMARY KEY, component VARCHAR(64) NOT NULL,
            mailbox_id UUID REFERENCES mailboxes(id), entity_type VARCHAR(128), entity_id VARCHAR(128),
            error_code VARCHAR(64) NOT NULL, retryable BOOLEAN NOT NULL,
            message_redacted TEXT NOT NULL, metadata JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """,
    )
    for statement in tables:
        _execute(statement)

    # 旧表可能来自未提交的早期 Base.metadata.create_all；补齐第五阶段字段。
    columns = {
        "mailboxes": (
            "gmail_user_id VARCHAR(255)",
            "watch_topic TEXT",
            "watch_subscription TEXT",
            "watch_expiration_at TIMESTAMPTZ",
            "last_watch_at TIMESTAMPTZ",
            "last_push_at TIMESTAMPTZ",
            "last_sync_at TIMESTAMPTZ",
            "last_error_code VARCHAR(64)",
            "last_error_at TIMESTAMPTZ",
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        ),
        "gmail_messages": (
            "reply_to_addresses JSONB",
            "internal_date TIMESTAMPTZ",
            "attachments JSONB",
            "attachment_count INTEGER NOT NULL DEFAULT 0",
            "parse_attempt_count INTEGER NOT NULL DEFAULT 0",
            "parse_error TEXT",
            "parsed_at TIMESTAMPTZ",
            "raw_headers JSONB",
            "content_hash VARCHAR(64)",
            "state VARCHAR(32) NOT NULL DEFAULT 'discovered'",
            "fetch_attempt_count INTEGER NOT NULL DEFAULT 0",
            "next_fetch_at TIMESTAMPTZ",
            "last_error_code VARCHAR(64)",
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        ),
        "notification_deliveries": (
            "schema_version VARCHAR(16) NOT NULL DEFAULT '1'",
            "payload_json JSONB",
            "attempt_count INTEGER NOT NULL DEFAULT 0",
            "next_attempt_at TIMESTAMPTZ",
            "last_http_status INTEGER",
            "last_error_code VARCHAR(64)",
            "last_response_hash VARCHAR(64)",
            "delivered_at TIMESTAMPTZ",
            "created_at TIMESTAMPTZ NOT NULL DEFAULT now()",
            "updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
        ),
    }
    for table, definitions in columns.items():
        for definition in definitions:
            _execute(f"ALTER TABLE IF EXISTS {table} ADD COLUMN IF NOT EXISTS {definition}")

    # 对可能由早期 create_all 生成的旧表补齐约束。检查按实际列顺序和目标表匹配，
    # 不依赖约束名称，避免因为名称不同而创建重复唯一索引或重复外键。
    constraints = (
        """
        DO $$ BEGIN
            IF to_regclass('public.mailboxes') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.mailboxes'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='email')]::smallint[]
            ) THEN
                ALTER TABLE mailboxes ADD CONSTRAINT uq_mailboxes_email UNIQUE (email);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.oauth_credentials') IS NOT NULL
               AND to_regclass('public.mailboxes') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.oauth_credentials'::regclass AND c.contype='f'
                  AND c.confrelid='public.mailboxes'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.oauth_credentials'::regclass AND attname='mailbox_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE oauth_credentials ADD CONSTRAINT fk_oauth_credentials_mailbox
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id) ON DELETE CASCADE;
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.gmail_sync_states') IS NOT NULL
               AND to_regclass('public.mailboxes') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.gmail_sync_states'::regclass AND c.contype='f'
                  AND c.confrelid='public.mailboxes'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.gmail_sync_states'::regclass AND attname='mailbox_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE gmail_sync_states ADD CONSTRAINT fk_gmail_sync_states_mailbox
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id) ON DELETE CASCADE;
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.inbound_events') IS NOT NULL
               AND to_regclass('public.mailboxes') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.inbound_events'::regclass AND c.contype='f'
                  AND c.confrelid='public.mailboxes'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.inbound_events'::regclass AND attname='mailbox_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE inbound_events ADD CONSTRAINT fk_inbound_events_mailbox
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.inbound_events') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.inbound_events'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.inbound_events'::regclass AND attname='provider'),
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.inbound_events'::regclass AND attname='pubsub_message_id')
                  ]::smallint[]
            ) THEN
                ALTER TABLE inbound_events ADD CONSTRAINT uq_inbound_events_provider_message
                UNIQUE (provider, pubsub_message_id);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.sync_requests') IS NOT NULL
               AND to_regclass('public.mailboxes') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.sync_requests'::regclass AND c.contype='f'
                  AND c.confrelid='public.mailboxes'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.sync_requests'::regclass AND attname='mailbox_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE sync_requests ADD CONSTRAINT fk_sync_requests_mailbox
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id) ON DELETE CASCADE;
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.sync_requests') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.sync_requests'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.sync_requests'::regclass AND attname='mailbox_id'),
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.sync_requests'::regclass AND attname='observed_history_id'),
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.sync_requests'::regclass AND attname='reason')
                  ]::smallint[]
            ) THEN
                ALTER TABLE sync_requests ADD CONSTRAINT uq_sync_requests_mailbox_history_reason
                UNIQUE (mailbox_id, observed_history_id, reason);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.gmail_messages') IS NOT NULL
               AND to_regclass('public.mailboxes') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.gmail_messages'::regclass AND c.contype='f'
                  AND c.confrelid='public.mailboxes'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.gmail_messages'::regclass AND attname='mailbox_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE gmail_messages ADD CONSTRAINT fk_gmail_messages_mailbox
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id) ON DELETE CASCADE;
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.notification_endpoints') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.notification_endpoints'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_endpoints'::regclass AND attname='name')]::smallint[]
            ) THEN
                ALTER TABLE notification_endpoints ADD CONSTRAINT uq_notification_endpoints_name UNIQUE (name);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.gmail_messages') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.gmail_messages'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.gmail_messages'::regclass AND attname='mailbox_id'),
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.gmail_messages'::regclass AND attname='gmail_message_id')
                  ]::smallint[]
            ) THEN
                ALTER TABLE gmail_messages ADD CONSTRAINT uq_gmail_messages_mailbox_message UNIQUE (mailbox_id, gmail_message_id);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.notification_deliveries') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.notification_deliveries'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_deliveries'::regclass AND attname='idempotency_key')
                  ]::smallint[]
            ) THEN
                ALTER TABLE notification_deliveries ADD CONSTRAINT uq_notification_deliveries_idempotency UNIQUE (idempotency_key);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.notification_deliveries') IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.notification_deliveries'::regclass AND c.contype='u'
                  AND c.conkey=ARRAY[
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_deliveries'::regclass AND attname='message_id'),
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_deliveries'::regclass AND attname='endpoint_id'),
                    (SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_deliveries'::regclass AND attname='event_type')
                  ]::smallint[]
            ) THEN
                ALTER TABLE notification_deliveries ADD CONSTRAINT uq_notification_deliveries_message_endpoint_event
                UNIQUE (message_id, endpoint_id, event_type);
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.notification_deliveries') IS NOT NULL
               AND to_regclass('public.gmail_messages') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.notification_deliveries'::regclass AND c.contype='f'
                  AND c.confrelid='public.gmail_messages'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_deliveries'::regclass AND attname='message_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.gmail_messages'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE notification_deliveries ADD CONSTRAINT fk_notification_deliveries_message
                FOREIGN KEY (message_id) REFERENCES gmail_messages(id) ON DELETE CASCADE;
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.notification_deliveries') IS NOT NULL
               AND to_regclass('public.notification_endpoints') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.notification_deliveries'::regclass AND c.contype='f'
                  AND c.confrelid='public.notification_endpoints'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_deliveries'::regclass AND attname='endpoint_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.notification_endpoints'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE notification_deliveries ADD CONSTRAINT fk_notification_deliveries_endpoint
                FOREIGN KEY (endpoint_id) REFERENCES notification_endpoints(id) ON DELETE CASCADE;
            END IF;
        END $$
        """,
        """
        DO $$ BEGIN
            IF to_regclass('public.error_events') IS NOT NULL
               AND to_regclass('public.mailboxes') IS NOT NULL
               AND NOT EXISTS (
                SELECT 1 FROM pg_constraint c
                WHERE c.conrelid='public.error_events'::regclass AND c.contype='f'
                  AND c.confrelid='public.mailboxes'::regclass
                  AND c.conkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.error_events'::regclass AND attname='mailbox_id')]::smallint[]
                  AND c.confkey=ARRAY[(SELECT attnum FROM pg_attribute WHERE attrelid='public.mailboxes'::regclass AND attname='id')]::smallint[]
            ) THEN
                ALTER TABLE error_events ADD CONSTRAINT fk_error_events_mailbox
                FOREIGN KEY (mailbox_id) REFERENCES mailboxes(id);
            END IF;
        END $$
        """,
    )
    for statement in constraints:
        _execute(statement)

    indexes = (
        "CREATE INDEX IF NOT EXISTS ix_mailboxes_status ON mailboxes (status)",
        "CREATE INDEX IF NOT EXISTS ix_mailboxes_watch_expiration_at ON mailboxes (watch_expiration_at)",
        "CREATE INDEX IF NOT EXISTS ix_mailboxes_last_push_at ON mailboxes (last_push_at)",
        "CREATE INDEX IF NOT EXISTS ix_gmail_messages_state ON gmail_messages (mailbox_id, state, next_fetch_at)",
        "CREATE INDEX IF NOT EXISTS ix_gmail_messages_received_at ON gmail_messages (received_at)",
        "CREATE INDEX IF NOT EXISTS ix_gmail_messages_thread_id ON gmail_messages (thread_id)",
        "CREATE INDEX IF NOT EXISTS ix_notification_deliveries_due ON notification_deliveries (state, next_attempt_at)",
        "CREATE INDEX IF NOT EXISTS ix_error_events_component_code_created ON error_events (component, error_code, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_error_events_mailbox_created ON error_events (mailbox_id, created_at)",
    )
    for statement in indexes:
        _execute(statement)


def downgrade() -> None:
    # 不能确认 0001--0004 是否曾经部署，删除表会造成不可逆的数据损失。
    # 该迁移的降级因此是保守 no-op；upgrade head 可重复执行并保持幂等。
    pass
