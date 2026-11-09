"""原始 RFC822 邮件解析：只保留规范化正文和元数据，不保存 MIME。"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html import escape
from html.parser import HTMLParser
from typing import Any, cast


@dataclass(frozen=True)
class ParsedMail:
    stable_key: str
    message_id: str | None
    thread_key: str | None
    subject: str | None
    from_address: str | None
    to_addresses: list[str]
    cc_addresses: list[str]
    received_at: datetime | None
    text_body: str | None
    html_body: str | None
    has_attachments: bool
    body_hash: str


def _addresses(value: str | None) -> list[str]:
    return [addr for _, addr in getaddresses([value or ""]) if addr]


def _date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = parsedate_to_datetime(value)
        return (result if result.tzinfo else result.replace(tzinfo=UTC)).astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


class _SafeHTMLParser(HTMLParser):
    _allowed_tags = {
        "a",
        "b",
        "blockquote",
        "br",
        "code",
        "div",
        "em",
        "i",
        "li",
        "ol",
        "p",
        "pre",
        "span",
        "strong",
        "u",
        "ul",
    }
    _void_tags = {"br"}
    _blocked_tags = {"iframe", "object", "embed", "form", "script", "style", "svg", "math"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.stack: list[str] = []
        self.blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._blocked_tags:
            self.blocked_depth += 1
            return
        if self.blocked_depth or tag not in self._allowed_tags:
            return
        if tag == "a":
            href = next((value for key, value in attrs if key.lower() == "href"), None)
            if href and href.split(":", 1)[0].lower() in {"http", "https", "mailto"}:
                self.parts.append(
                    f'<a href="{escape(href, quote=True)}" '
                    'rel="noopener noreferrer" target="_blank">'
                )
            else:
                self.parts.append("<a>")
        else:
            self.parts.append(f"<{tag}>")
        if tag not in self._void_tags:
            self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        blocked = tag.lower() in self._blocked_tags
        self.handle_starttag(tag, attrs)
        if blocked:
            self.blocked_depth = max(0, self.blocked_depth - 1)
            return
        if self.stack and self.stack[-1] == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._blocked_tags:
            self.blocked_depth = max(0, self.blocked_depth - 1)
            return
        if self.blocked_depth or tag not in self._allowed_tags or tag in self._void_tags:
            return
        if tag in self.stack:
            while self.stack:
                current = self.stack.pop()
                self.parts.append(f"</{current}>")
                if current == tag:
                    break

    def handle_data(self, data: str) -> None:
        if not self.blocked_depth:
            self.parts.append(escape(data))


def sanitize_html(value: str) -> str:
    parser = _SafeHTMLParser()
    parser.feed(value)
    parser.close()
    while parser.stack:
        parser.parts.append(f"</{parser.stack.pop()}>")
    return "".join(parser.parts)


def _body(
    message: EmailMessage, *, max_body_chars: int = 1_000_000
) -> tuple[str | None, str | None, bool]:
    texts: list[str] = []
    html_parts: list[str] = []
    has_attachment = False
    for part in message.walk():
        if part.is_multipart():
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename or "attachment" in disposition:
            has_attachment = True
            continue
        content_type = part.get_content_type()
        try:
            content = part.get_content()
        except (LookupError, UnicodeError, ValueError):
            payload = part.get_payload(decode=True) or b""
            content = (
                payload.decode("utf-8", errors="replace")
                if isinstance(payload, bytes)
                else str(payload)
            )
        if not isinstance(content, str):
            continue
        if content_type == "text/plain":
            texts.append(content)
        elif content_type == "text/html":
            html_parts.append(sanitize_html(content))
    text_body = "\n".join(texts)[:max_body_chars] or None
    html_body = "\n".join(html_parts)[:max_body_chars] or None
    return text_body, html_body, has_attachment


def parse_rfc822(
    raw: bytes,
    *,
    x_gm_msgid: str | None = None,
    x_gm_thrid: str | None = None,
    max_body_chars: int = 1_000_000,
) -> ParsedMail:
    message = cast(
        EmailMessage,
        BytesParser(policy=cast(Any, policy.default)).parsebytes(raw),
    )
    message_id = (message.get("Message-ID") or "").strip() or None
    stable_key = x_gm_msgid or message_id
    text_body, html_body, has_attachment = _body(message, max_body_chars=max_body_chars)
    if not stable_key:
        stable_key = hashlib.sha256(
            "|".join(
                [
                    message.get("From", ""),
                    message.get("Date", ""),
                    message.get("Subject", ""),
                    text_body or "",
                    html_body or "",
                ]
            ).encode()
        ).hexdigest()
    body_hash = hashlib.sha256(
        (text_body or "").encode() + b"\0" + (html_body or "").encode()
    ).hexdigest()
    return ParsedMail(
        stable_key,
        message_id,
        x_gm_thrid,
        message.get("Subject"),
        message.get("From"),
        _addresses(message.get("To")),
        _addresses(message.get("Cc")),
        _date(message.get("Date")),
        text_body,
        html_body,
        has_attachment,
        body_hash,
    )
