"""第五阶段：邮件内容、附件元数据和通知 Outbox 字段。

项目早期迁移由部署环境根据 ``Base.metadata`` 建表；本迁移使用
``IF EXISTS``/``IF NOT EXISTS``，可安全应用在已有第四阶段数据库上。
"""

from alembic import op

revision = "0005_stage5_email_content_outbox"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    for statement in (
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS reply_to_addresses JSONB",
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS internal_date TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS attachments JSONB",
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS attachment_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS parse_attempt_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS parse_error TEXT",
        "ALTER TABLE IF EXISTS gmail_messages ADD COLUMN IF NOT EXISTS parsed_at TIMESTAMPTZ",
        "ALTER TABLE IF EXISTS notification_deliveries ADD COLUMN IF NOT EXISTS schema_version VARCHAR(16) NOT NULL DEFAULT '1'",
        "ALTER TABLE IF EXISTS notification_deliveries ADD COLUMN IF NOT EXISTS payload_json JSONB",
        # 0005 可能运行在尚未建立基础表的空库；只有表存在时才创建索引，
        # 让后续 0006 schema repair 负责完整建表和索引。
        """
        DO $$
        BEGIN
            IF to_regclass('public.notification_deliveries') IS NOT NULL
               AND EXISTS (
                   SELECT 1 FROM information_schema.columns
                   WHERE table_schema='public' AND table_name='notification_deliveries'
                     AND column_name IN ('state', 'next_attempt_at')
                   GROUP BY table_schema, table_name
                   HAVING count(*) = 2
               ) THEN
                CREATE INDEX IF NOT EXISTS ix_notification_deliveries_due
                ON notification_deliveries (state, next_attempt_at);
            END IF;
        END $$
        """,
    ):
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP INDEX IF EXISTS ix_notification_deliveries_due",
        "ALTER TABLE IF EXISTS notification_deliveries DROP COLUMN IF EXISTS payload_json",
        "ALTER TABLE IF EXISTS notification_deliveries DROP COLUMN IF EXISTS schema_version",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS parsed_at",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS parse_error",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS parse_attempt_count",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS attachment_count",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS attachments",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS reply_to_addresses",
        "ALTER TABLE IF EXISTS gmail_messages DROP COLUMN IF EXISTS internal_date",
    ):
        op.execute(statement)
