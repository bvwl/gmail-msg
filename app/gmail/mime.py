"""Gmail ``format=full`` 载荷的安全 MIME 解析。

解析器只处理字节和文本，不执行 HTML、附件或其中的脚本。Gmail API 返回的
``body.data`` 是 base64url 编码，因此所有解码都在受限的内存路径中完成。
"""

from __future__ import annotations

import base64
import binascii
import quopri
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any


@dataclass(frozen=True)
class ParsedAddress:
    name: str
    email: str


@dataclass(frozen=True)
class AttachmentMetadata:
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None = None
    inline: bool = False
    content_id: str | None = None
    too_large: bool = False


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    thread_id: str | None
    internal_date: datetime | None
    sender: tuple[ParsedAddress, ...]
    to: tuple[ParsedAddress, ...]
    cc: tuple[ParsedAddress, ...]
    reply_to: tuple[ParsedAddress, ...]
    subject: str | None
    date: datetime | None
    text_body: str | None
    html_body: str | None
    attachments: tuple[AttachmentMetadata, ...] = field(default_factory=tuple)
    body_truncated: bool = False

    @property
    def has_attachments(self) -> bool:
        return bool(self.attachments)


class MimeParseError(ValueError):
    """载荷结构无法解析。"""


class GmailMimeParser:
    def __init__(
        self, *, max_body_chars: int = 1_000_000, max_attachment_bytes: int = 25 * 1024 * 1024
    ) -> None:
        self.max_body_chars = max_body_chars
        self.max_attachment_bytes = max_attachment_bytes

    def parse(self, payload: Mapping[str, Any]) -> ParsedEmail:
        return parse_gmail_full(
            payload,
            max_body_chars=self.max_body_chars,
            max_attachment_bytes=self.max_attachment_bytes,
        )


def decode_mime_header(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeError, ValueError):
        return value.encode("utf-8", "replace").decode("utf-8", "replace")


def parse_addresses(value: str | None) -> tuple[ParsedAddress, ...]:
    if not value:
        return ()
    result: list[ParsedAddress] = []
    try:
        pairs = getaddresses([value])
    except (TypeError, ValueError):
        pairs = []
    for name, address in pairs:
        address = address.strip()
        if not address:
            continue
        result.append(ParsedAddress(decode_mime_header(name) or "", address))
    return tuple(result)


def parse_header_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _decode_base64url(value: str | bytes | None) -> bytes:
    if not value:
        return b""
    raw = value.encode("ascii", "ignore") if isinstance(value, str) else value
    raw += b"=" * (-len(raw) % 4)
    try:
        return base64.urlsafe_b64decode(raw)
    except (binascii.Error, ValueError):
        # Gmail sometimes contains malformed padding. Ignore invalid bytes but
        # preserve the decodable prefix instead of aborting the whole message.
        try:
            return base64.b64decode(raw, altchars=b"-_")
        except (binascii.Error, ValueError):
            return b""


decode_base64url = _decode_base64url


def _decode_part_bytes(part: Mapping[str, Any]) -> bytes:
    body = part.get("body")
    if not isinstance(body, Mapping):
        return b""
    decoded = _decode_base64url(body.get("data"))
    headers = _headers(part)
    transfer = headers.get("content-transfer-encoding", "").lower()
    try:
        if transfer == "quoted-printable":
            return quopri.decodestring(decoded)
        if transfer == "base64":
            compact = b"".join(decoded.split())
            if compact and len(compact) % 4 == 0:
                return base64.b64decode(compact, validate=False)
    except (binascii.Error, ValueError):
        return decoded
    return decoded


def _headers(part: Mapping[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    raw = part.get("headers")
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, Mapping) or not item.get("name"):
                continue
            result[str(item["name"]).lower()] = str(item.get("value", ""))
    return result


def _decode_text(data: bytes, charset: str | None) -> str:
    selected = (charset or "utf-8").strip().strip("\"'") or "utf-8"
    try:
        return data.decode(selected, errors="replace")
    except (LookupError, UnicodeError):
        for fallback in ("utf-8", "gb18030", "latin-1"):
            try:
                return data.decode(fallback, errors="replace")
            except UnicodeError:
                continue
    return data.decode("utf-8", errors="replace")


def parse_gmail_full(
    payload: Mapping[str, Any],
    *,
    max_body_chars: int = 1_000_000,
    max_attachment_bytes: int = 25 * 1024 * 1024,
) -> ParsedEmail:
    """解析 Gmail API ``format=full`` 对象。

    ``max_body_chars`` 同时限制纯文本和 HTML，超出部分会被截断并通过
    ``body_truncated`` 标记；附件只保留元数据，不读取远端二进制。
    """

    if not isinstance(payload, Mapping):
        raise MimeParseError("Gmail message payload must be an object")
    message_id = str(payload.get("id", "")).strip()
    if not message_id:
        raise MimeParseError("Gmail message id is missing")
    root = payload.get("payload")
    if not isinstance(root, Mapping):
        root = {}
    root_headers = _headers(root)
    text_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[AttachmentMetadata] = []
    truncated = False

    def visit(part: Mapping[str, Any]) -> None:
        nonlocal truncated
        mime_type = str(part.get("mimeType", "")).split(";", 1)[0].strip().lower()
        headers = _headers(part)
        filename = decode_mime_header(str(part.get("filename", ""))) or ""
        body = part.get("body")
        body_map = body if isinstance(body, Mapping) else {}
        attachment_id = str(body_map.get("attachmentId", "")).strip() or None
        size_raw = body_map.get("size", 0)
        try:
            size = max(0, int(size_raw or 0))
        except (TypeError, ValueError):
            size = 0
        disposition = headers.get("content-disposition", "").lower()
        if not filename:
            filename = decode_mime_header(_header_param(disposition, "filename")) or ""
        if not filename:
            filename = (
                decode_mime_header(_header_param(headers.get("content-type", ""), "name")) or ""
            )
        is_attachment = bool(filename or attachment_id or "attachment" in disposition)
        if is_attachment and (filename or attachment_id):
            attachments.append(
                AttachmentMetadata(
                    filename=filename or "attachment",
                    mime_type=mime_type or "application/octet-stream",
                    size=size,
                    attachment_id=attachment_id,
                    inline="inline" in disposition,
                    content_id=headers.get("content-id"),
                    too_large=size > max_attachment_bytes,
                )
            )
        children = part.get("parts")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, Mapping):
                    visit(child)
        if is_attachment or mime_type.startswith("multipart/"):
            return
        if mime_type not in {"text/plain", "text/html"}:
            return
        data = _decode_part_bytes(part)
        text = _decode_text(
            data, _charset(headers.get("content-type") or str(part.get("mimeType", "")))
        )
        remaining = max_body_chars - sum(len(x) for x in text_parts + html_parts)
        if remaining <= 0:
            truncated = True
            return
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        (html_parts if mime_type == "text/html" else text_parts).append(text)

    visit(root)
    internal_date = _internal_date(payload.get("internalDate"))
    return ParsedEmail(
        message_id=message_id,
        thread_id=str(payload.get("threadId", "")).strip() or None,
        internal_date=internal_date,
        sender=parse_addresses(root_headers.get("from")),
        to=parse_addresses(root_headers.get("to")),
        cc=parse_addresses(root_headers.get("cc")),
        reply_to=parse_addresses(root_headers.get("reply-to")),
        subject=decode_mime_header(root_headers.get("subject")),
        date=parse_header_date(root_headers.get("date")),
        text_body="\n".join(text_parts) or None,
        html_body="\n".join(html_parts) or None,
        attachments=tuple(attachments),
        body_truncated=truncated,
    )


def _charset(content_type: str | None) -> str | None:
    if not content_type:
        return None
    for item in content_type.split(";")[1:]:
        key, _, value = item.partition("=")
        if key.strip().lower() == "charset":
            return value.strip()
    return None


def _header_param(value: str, name: str) -> str | None:
    for item in value.split(";")[1:]:
        key, sep, raw = item.strip().partition("=")
        if sep and key.lower() == name.lower():
            return raw.strip().strip("\"'")
    # Handle RFC 2231's filename*=utf-8''... form without adding a dependency.
    for item in value.split(";")[1:]:
        key, sep, raw = item.strip().partition("=")
        if sep and key.lower() == name.lower() + "*":
            raw = raw.strip().strip("\"'")
            if "''" in raw:
                _, raw = raw.split("''", 1)
            try:
                from urllib.parse import unquote

                return unquote(raw)
            except (UnicodeError, ValueError):
                return raw
    return None


def _internal_date(value: Any) -> datetime | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


__all__ = [
    "AttachmentMetadata",
    "MimeParseError",
    "ParsedAddress",
    "ParsedEmail",
    "GmailMimeParser",
    "decode_mime_header",
    "decode_base64url",
    "parse_addresses",
    "parse_gmail_full",
    "parse_message",
]


parse_message = parse_gmail_full
