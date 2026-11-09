from __future__ import annotations

import base64
import quopri

from app.gmail.mime import parse_gmail_full


def enc(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def test_parse_nested_multipart_headers_bodies_and_attachment() -> None:
    payload = {
        "id": "msg-1",
        "threadId": "thread-1",
        "internalDate": "1735689600000",
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [
                {"name": "From", "value": "=?utf-8?b?5byg5LiJ?= <sender@example.com>"},
                {"name": "To", "value": "receiver@example.com"},
                {"name": "Reply-To", "value": "reply@example.com"},
                {"name": "Subject", "value": "=?utf-8?b?5YWs5ZGK?="},
                {"name": "Date", "value": "Wed, 01 Jan 2025 00:00:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "Content-Type", "value": "text/plain; charset=utf-8"}
                            ],
                            "body": {"data": enc("你好 😀".encode())},
                        },
                        {"mimeType": "text/html", "body": {"data": enc(b"<p>Hello</p>")}},
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "invoice.pdf",
                    "headers": [{"name": "Content-Disposition", "value": "attachment"}],
                    "body": {"attachmentId": "att-1", "size": 123},
                },
            ],
        },
    }

    result = parse_gmail_full(payload)
    assert result.subject == "公告"
    assert result.sender[0].name == "张三"
    assert result.to[0].email == "receiver@example.com"
    assert result.reply_to[0].email == "reply@example.com"
    assert result.text_body == "你好 😀"
    assert result.html_body == "<p>Hello</p>"
    assert result.attachments[0].attachment_id == "att-1"
    assert result.internal_date is not None


def test_parse_bad_base64_and_body_limit_are_recoverable() -> None:
    result = parse_gmail_full(
        {
            "id": "msg-2",
            "payload": {"mimeType": "text/plain", "body": {"data": "%%%bad%%%"}},
        },
        max_body_chars=4,
    )
    assert result.text_body is not None
    assert len(result.text_body) <= 4


def test_large_attachment_is_metadata_only() -> None:
    result = parse_gmail_full(
        {
            "id": "msg-3",
            "payload": {
                "mimeType": "application/zip",
                "filename": "../unsafe.zip",
                "body": {"attachmentId": "att", "size": 99},
            },
        },
        max_attachment_bytes=10,
    )
    assert result.attachments[0].too_large is True


def test_charset_and_quoted_printable_are_decoded() -> None:
    encoded = quopri.encodestring("你好".encode("gb18030"))
    result = parse_gmail_full(
        {
            "id": "msg-4",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "Content-Type", "value": "text/plain; charset=gb18030"},
                    {"name": "Content-Transfer-Encoding", "value": "quoted-printable"},
                ],
                "body": {"data": enc(encoded)},
            },
        }
    )
    assert result.text_body == "你好"
