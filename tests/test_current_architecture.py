from __future__ import annotations

import pytest
from app.auth.admin import hash_password, verify_password
from app.config import Settings
from app.imap.proxy import ProxyConfig, ProxyMode, ProxyState
from app.main import create_app
from app.services.codes import extract_codes
from app.services.mail_parser import parse_rfc822
from fastapi.testclient import TestClient


def test_admin_password_is_argon2_hash_and_never_plaintext() -> None:
    password = "ADMIN_PASSWORD_PLACEHOLDER"
    password_hash = hash_password(password)

    assert password_hash != password
    assert password_hash.startswith("$argon2")
    assert verify_password(password_hash, password)
    assert not verify_password(password_hash, "wrong-password")
    with pytest.raises(ValueError, match="password_too_short"):
        hash_password("short")


def test_proxy_modes_enforce_no_direct_fallback() -> None:
    ProxyConfig(ProxyMode.DIRECT).validate()
    with pytest.raises(ValueError, match="direct mode"):
        ProxyConfig(ProxyMode.DIRECT, host="proxy.test").validate()

    with pytest.raises(ValueError, match="dns_mode"):
        ProxyConfig(ProxyMode.SOCKS5, host="127.0.0.1", port=51080).validate()
    ProxyConfig(
        ProxyMode.SOCKS5,
        host="127.0.0.1",
        port=51080,
        dns_mode="remote",
    ).validate()
    assert ProxyState.WAITING.value == "waiting_for_proxy"


def test_rfc822_parser_keeps_body_and_attachment_metadata_only() -> None:
    raw = (
        b"From: sender@example.test\n"
        b"To: mailbox@example.test\n"
        b"Subject: Verification 654321\n"
        b"Message-ID: <stable@example.test>\n"
        b"MIME-Version: 1.0\n"
        b"Content-Type: multipart/mixed; boundary=part\n\n"
        b"--part\nContent-Type: text/plain; charset=utf-8\n\ncode 654321\n"
        b"--part\nContent-Type: application/pdf\n"
        b"Content-Disposition: attachment; filename=invoice.pdf\n\nsecret\n"
        b"--part--\n"
    )

    parsed = parse_rfc822(raw)

    assert parsed.stable_key == "<stable@example.test>"
    assert parsed.text_body == "code 654321"
    assert parsed.has_attachments is True
    assert "secret" not in (parsed.text_body or "")
    assert "raw" not in parsed.__dict__
    assert [
        item.value for item in extract_codes(parsed.subject, parsed.text_body, parsed.html_body)
    ] == ["654321"]


def test_admin_api_requires_session_before_database_access() -> None:
    app = create_app(Settings(_env_file=None, app_env="test"))  # type: ignore[call-arg]

    with TestClient(app) as client:
        response = client.get("/api/mailboxes")

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "AUTH_REQUIRED"
