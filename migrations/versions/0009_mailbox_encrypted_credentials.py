"""为邮箱凭据增加应用层加密字段。"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009_mailbox_creds"
down_revision = "0008_admin_audit_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("mailboxes", "app_password_secret_ref", existing_type=sa.String(512), nullable=True)
    op.add_column("mailboxes", sa.Column("app_password_encrypted", sa.Text(), nullable=True))
    op.add_column("mailboxes", sa.Column("proxy_password_encrypted", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("mailboxes", "proxy_password_encrypted")
    op.drop_column("mailboxes", "app_password_encrypted")
    op.alter_column("mailboxes", "app_password_secret_ref", existing_type=sa.String(512), nullable=False)
