from __future__ import annotations

from app.imap.proxy import Backoff, ProxyConfig, ProxyMode
from app.services.codes import extract_codes
from app.services.mail_parser import parse_rfc822


def test_direct_proxy_rejects_proxy_fields() -> None:
    try:
        ProxyConfig(ProxyMode.DIRECT, host="proxy").validate()
    except ValueError:
        pass
    else:
        raise AssertionError("direct proxy settings must be rejected")


def test_socks5_requires_dns_mode() -> None:
    try:
        ProxyConfig(ProxyMode.SOCKS5, host="proxy", port=1080).validate()
    except ValueError:
        pass
    else:
        raise AssertionError("dns mode is required")


def test_backoff_is_truncated_and_resettable() -> None:
    backoff = Backoff(base=1, maximum=4, jitter=0, random_fn=lambda: 0)
    assert [backoff.next_delay() for _ in range(5)] == [1, 2, 4, 4, 4]
    backoff.reset()
    assert backoff.next_delay() == 1


def test_parser_saves_one_body_and_sanitizes_html() -> None:
    raw = b"".join(
        [
            b"From: sender@example.com\n",
            b"To: user@example.com\n",
            b"Subject: Your verification code 123456\n",
            b"Message-ID: <m@example.com>\n",
            b"Date: Thu, 06 Nov 2026 00:00:00 +0000\n",
            b"MIME-Version: 1.0\n",
            b'Content-Type: multipart/alternative; boundary="x"\n\n',
            b"--x\nContent-Type: text/plain; charset=utf-8\n\ncode 123456\n",
            b"--x\nContent-Type: text/html; charset=utf-8\n\n",
            b"<script>alert(1)</script><p>code 123456</p>\n--x--\n",
        ]
    )
    parsed = parse_rfc822(raw)
    assert parsed.stable_key == "<m@example.com>"
    assert parsed.text_body == "code 123456"
    assert "<script" not in (parsed.html_body or "")
    assert [c.value for c in extract_codes(parsed.subject, parsed.text_body, parsed.html_body)] == [
        "123456"
    ]
