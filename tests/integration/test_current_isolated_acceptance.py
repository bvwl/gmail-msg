from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from app.auth.admin import hash_password
from app.config import Settings
from app.db.base import (
    AdminUser,
    AuditLog,
    Mailbox,
    MailboxFolder,
    Message,
    MessageLabel,
    VerificationCode,
)
from app.db.session import DatabaseManager
from app.main import create_app
from sqlalchemy import select

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_admin_audit_is_persisted_for_isolated_api_flow() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url or os.getenv("TEST_ALLOW_ISOLATED_INTEGRATION") != "1":
        pytest.skip("需要显式 TEST_ALLOW_ISOLATED_INTEGRATION=1 和隔离 TEST_DATABASE_URL")
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url=database_url,
        admin_cookie_secure=False,
        cors_origins=["http://127.0.0.1:5173"],
    )
    database = DatabaseManager(settings)
    app = create_app(settings, database=database)
    username = f"ADMIN_{uuid4().hex[:12]}"
    mailbox_address = f"mailbox-{uuid4().hex[:12]}@example.test"
    async with database.session() as session:
        user = AdminUser(
            username=username, password_hash=hash_password("ADMIN_PASSWORD_PLACEHOLDER")
        )
        mailbox = Mailbox(
            address=mailbox_address,
            imap_host="127.0.0.1",
            imap_port=5993,
            username="MAILBOX",
            app_password_secret_ref="SECRET_REF",
            proxy_mode="socks5",
            proxy_host="127.0.0.1",
            proxy_port=51080,
            proxy_dns_mode="remote",
            proxy_status="idle",
            connection_state="idle",
        )
        session.add_all([user, mailbox])
        await session.flush()
        folder = MailboxFolder(
            mailbox_id=mailbox.id,
            imap_name="INBOX",
            folder_role="inbox",
            uidvalidity=7,
            uidnext=2,
            baseline_uid=1,
            last_seen_uid=1,
            sync_state="idle",
        )
        session.add(folder)
        await session.flush()
        message = Message(
            mailbox_id=mailbox.id,
            stable_message_key=f"<{uuid4().hex}@example.test>",
            subject="Verification 654321",
            from_address="sender@example.test",
            received_at=datetime.now(UTC),
            text_body="Your code is 654321",
            html_body_sanitized="<p>Your code is 654321</p>",
            body_hash="placeholder",
        )
        session.add(message)
        await session.flush()
        session.add_all(
            [
                MessageLabel(
                    message_id=message.id,
                    mailbox_folder_id=folder.id,
                    folder_uid=1,
                    uidvalidity_snapshot=7,
                ),
                VerificationCode(message_id=message.id, code_value="654321"),
            ]
        )
        await session.commit()
        mailbox_id = mailbox.id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (
            await client.post(
                "/api/admin/login",
                json={"username": username, "password": "WRONG_PASSWORD_PLACEHOLDER"},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/admin/login",
                json={"username": username, "password": "ADMIN_PASSWORD_PLACEHOLDER"},
            )
        ).status_code == 204
        session_cookie = client.cookies.get("admin_session")
        message_rows = (
            await client.get(f"/api/mailboxes/{mailbox_id}/messages?folder=inbox")
        ).json()
        message_id = message_rows["items"][0]["id"]
        assert (await client.get(f"/api/messages/{message_id}")).status_code == 200
        assert (
            await client.get(f"/api/mailboxes/{mailbox_id}/codes?include_value=true")
        ).json()[0]["value"] == "654321"
        assert (
            await client.patch(
                f"/api/mailboxes/{mailbox_id}", json={"proxy_host": "127.0.0.1"}
            )
        ).status_code == 200
        assert (await client.post("/api/admin/logout")).status_code == 204
        assert (await client.get("/api/mailboxes")).status_code == 401
        client.cookies.set("admin_session", session_cookie or "STALE_SESSION")
        assert (
            await client.get("/api/mailboxes")
        ).status_code == 401

    async with database.session() as session:
        actions = {
            row.action
            for row in (
                await session.execute(
                    select(AuditLog).where(
                        (AuditLog.admin_user_id == user.id) | (AuditLog.action == "session.invalid")
                    )
                )
            ).scalars()
        }
    await database.dispose()
    assert {
        "admin.login",
        "admin.logout",
        "mailbox.update",
        "message.view",
        "codes.view",
        "session.invalid",
    } <= actions
