"""验证外部主机和 IMAP 文件夹输入，避免命令注入与内网探测。"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,253}[A-Za-z0-9])?$")


def validate_hostname(value: str, *, field_name: str, allow_private: bool = False) -> str:
    host = value.strip().lower()
    if not host or len(host) > 255 or not _HOST_RE.fullmatch(host):
        raise ValueError(f"{field_name}_invalid")
    if any(ch in host for ch in "\r\n\x00"):
        raise ValueError(f"{field_name}_invalid")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if not allow_private and not address.is_global:
        raise ValueError(f"{field_name}_private_or_reserved")
    return host


def validate_imap_host(value: str, allowed_hosts: list[str]) -> str:
    host = validate_hostname(value, field_name="imap_host")
    normalized = [item.strip().lower() for item in allowed_hosts if item.strip()]
    if normalized and host not in normalized:
        raise ValueError("imap_host_not_allowed")
    return host


def validate_proxy_host(value: str) -> str:
    return validate_hostname(value, field_name="proxy_host", allow_private=True)


def validate_folder_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 255:
        raise ValueError("folder_name_invalid")
    if any(ord(ch) < 0x20 or ch == "\x7f" for ch in name):
        raise ValueError("folder_name_invalid")
    return name


def origin_from_referer(value: str) -> str | None:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"
