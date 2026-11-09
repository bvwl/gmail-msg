from __future__ import annotations

import pytest
from app.config import Settings
from app.main import MailboxRequest
from app.security.validation import validate_folder_name, validate_imap_host
from app.services.mail_parser import parse_rfc822, sanitize_html
from pydantic import ValidationError


def test_html_sanitizer_uses_an_allowlist() -> None:
    rendered = sanitize_html(
        '<svg><a xlink:href="javascript:alert(1)">x</a></svg>'
        '<img src="https://tracker.invalid/x" onerror="alert(1)">'
        '<p style="background:url(https://tracker.invalid/y)">safe</p>'
    )
    assert "javascript" not in rendered.lower()
    assert "tracker.invalid" not in rendered
    assert "<svg" not in rendered
    assert "<img" not in rendered
    assert "safe" in rendered


def test_folder_names_reject_command_control_characters() -> None:
    with pytest.raises(ValueError):
        validate_folder_name('INBOX\r\nNOOP')
    assert validate_folder_name('Archive "2026"') == 'Archive "2026"'


def test_imap_host_is_allowlisted_and_private_addresses_are_rejected() -> None:
    assert validate_imap_host("imap.gmail.com", ["imap.gmail.com"]) == "imap.gmail.com"
    with pytest.raises(ValueError):
        validate_imap_host("127.0.0.1", ["imap.gmail.com"])
    with pytest.raises(ValueError):
        validate_imap_host("mail.example.test", ["imap.gmail.com"])


def test_mailbox_request_rejects_invalid_folder_input() -> None:
    with pytest.raises(ValidationError):
        MailboxRequest(folders=["INBOX\nNOOP"])


def test_parser_truncates_body() -> None:
    raw = (
        b"From: sender@example.test\n"
        b"Subject: test\n"
        b"Content-Type: text/plain; charset=utf-8\n\n"
        b"0123456789"
    )
    parsed = parse_rfc822(raw, max_body_chars=4)
    assert parsed.text_body == "0123"


def test_production_settings_fail_closed() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")
