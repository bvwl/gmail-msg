"""IMAP SSL/TLS 连接抽象。代理恢复前不会读取凭据或建立 Gmail 连接。"""

from __future__ import annotations

import asyncio
import imaplib
import ssl
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from typing import Any, Protocol, cast

from app.imap.proxy import ProxyConfig, ProxyMode, check_proxy
from app.security.validation import validate_imap_host


class SecretResolver(Protocol):
    def resolve(self, secret_ref: str) -> str: ...


class EnvironmentSecretResolver:
    def resolve(self, secret_ref: str) -> str:
        import os

        key = secret_ref.removeprefix("ENV:")
        value = os.getenv(key)
        if not value:
            raise ValueError("secret_not_configured")
        return value


@dataclass(frozen=True)
class ImapSettings:
    host: str
    port: int
    username: str
    app_password_secret_ref: str | None
    proxy: ProxyConfig
    connect_timeout: float = 15.0
    app_password: str | None = None
    allowed_hosts: tuple[str, ...] = ("imap.gmail.com",)
    allowed_proxy_hosts: tuple[str, ...] = ()


class ImapClientProtocol(Protocol):
    def list_folders(self) -> list[tuple[str, str]]: ...
    def select(self, name: str) -> tuple[int, int, int]: ...
    def search_uids(self, start_uid: int) -> list[int]: ...
    def fetch_message(self, uid: int) -> tuple[bytes, dict[str, Any]]: ...
    def idle(self, timeout: int) -> bool: ...
    def close(self) -> None: ...


class ImapConnectionFactory:
    def __init__(self, secret_resolver: SecretResolver | None = None) -> None:
        self.secret_resolver = secret_resolver or EnvironmentSecretResolver()

    async def connect(self, settings: ImapSettings) -> ImapClient:
        validate_imap_host(settings.host, list(settings.allowed_hosts))
        settings.proxy.validate()
        if settings.proxy.host and settings.allowed_proxy_hosts:
            if settings.proxy.host.lower() not in {
                item.lower() for item in settings.allowed_proxy_hosts
            }:
                raise ValueError("proxy_host_not_allowed")
        if settings.proxy.mode is ProxyMode.SOCKS5:
            await check_proxy(settings.proxy, timeout=settings.connect_timeout)
            # imaplib cannot take an asyncio stream directly. A dedicated worker thread
            # uses a SOCKS-capable socket; credentials are resolved only after the check.
            password = self._resolve_password(settings)
            return await asyncio.to_thread(self._connect_socks, settings, password)
        password = self._resolve_password(settings)
        return await asyncio.to_thread(self._connect_direct, settings, password)

    def _resolve_password(self, settings: ImapSettings) -> str:
        if settings.app_password:
            return settings.app_password
        if not settings.app_password_secret_ref:
            raise ValueError("imap_password_not_configured")
        return self.secret_resolver.resolve(settings.app_password_secret_ref)

    def _connect_direct(self, settings: ImapSettings, password: str) -> ImapClient:
        client = imaplib.IMAP4_SSL(
            settings.host,
            settings.port,
            ssl_context=ssl.create_default_context(),
            timeout=settings.connect_timeout,
        )
        client.login(settings.username, password)
        return ImapClient(client)

    def _connect_socks(self, settings: ImapSettings, password: str) -> ImapClient:
        import socks  # type: ignore[import-untyped]

        sock = socks.socksocket()
        sock.set_proxy(
            socks.SOCKS5,
            settings.proxy.host,
            settings.proxy.port,
            rdns=settings.proxy.dns_mode == "remote",
            username=settings.proxy.username,
            password=settings.proxy.password,
        )
        sock.settimeout(settings.connect_timeout)
        sock.connect((settings.host, settings.port))
        context = ssl.create_default_context()
        tls_sock = context.wrap_socket(sock, server_hostname=settings.host)
        client = _SocksIMAP4SSL(
            settings.host,
            settings.port,
            ssl_context=context,
            timeout=settings.connect_timeout,
            connected_socket=tls_sock,
        )
        client.login(settings.username, password)
        return ImapClient(client)


class _SocksIMAP4SSL(imaplib.IMAP4_SSL):
    """IMAP4_SSL 变体：复用已通过 SOCKS5 建立的 TLS socket。"""

    def __init__(self, *args: Any, connected_socket: Any, **kwargs: Any) -> None:
        self._connected_socket = connected_socket
        super().__init__(*args, **kwargs)

    def _create_socket(self, timeout: float | None) -> Any:
        socket_obj = self._connected_socket
        self._connected_socket = None
        if socket_obj is not None:
            return socket_obj
        return cast(Any, imaplib.IMAP4)._create_socket(self, timeout)


class ImapClient:
    def __init__(self, client: imaplib.IMAP4_SSL) -> None:
        self.client = client

    def list_folders(self) -> list[tuple[str, str]]:
        typ, data = self.client.list()
        if typ != "OK":
            raise ConnectionError("imap_list_failed")
        folders: list[tuple[str, str]] = []
        for raw in data or []:
            if not raw:
                continue
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            # LIST response: flags, delimiter, quoted name. Keep server name intact.
            import re

            match = re.search(r'\(([^)]*)\).*?"([^"]*)"\s+(.*)$', line)
            if match:
                folders.append((match.group(3).strip().strip('"'), match.group(1)))
        return folders

    def select(self, name: str) -> tuple[int, int, int]:
        if any(ord(ch) < 0x20 or ch == "\x7f" for ch in name):
            raise ValueError("imap_folder_name_invalid")
        # imaplib sends command arguments verbatim; use its quoting routine instead
        # of interpolating the folder name into a quoted command ourselves.
        quoted_name = self.client._quote(name)
        typ, data = self.client.select(quoted_name, readonly=True)
        if typ != "OK":
            raise ConnectionError("imap_select_failed")
        uidvalidity = self._response_int("UIDVALIDITY", 0)
        uidnext = self._response_int("UIDNEXT", 1)
        raw_exists = (data or [b"0"])[0]
        if raw_exists is None:
            exists = 0
        else:
            exists = int(raw_exists.decode() if isinstance(raw_exists, bytes) else raw_exists)
        return uidvalidity, uidnext, exists

    def _response_int(self, name: str, default: int) -> int:
        _response_type, response = self.client.response(name)
        if not response or response[0] is None:
            return default
        return int(cast(Any, response[0] or default))

    def search_uids(self, start_uid: int) -> list[int]:
        typ, data = self.client.uid("SEARCH", None, f"UID {start_uid}:*")  # type: ignore[arg-type]
        if typ != "OK":
            raise ConnectionError("imap_search_failed")
        return [int(item) for item in (data[0] or b"").split()]

    def fetch_message(self, uid: int) -> tuple[bytes, dict[str, Any]]:
        typ, data = self.client.uid(
            "FETCH", str(uid), "(RFC822 X-GM-MSGID X-GM-THRID X-GM-LABELS INTERNALDATE UID)"
        )
        if typ != "OK":
            raise ConnectionError("imap_fetch_failed")
        raw = next(
            (item[1] for item in data if isinstance(item, tuple) and isinstance(item[1], bytes)),
            b"",
        )
        msg = cast(
            EmailMessage,
            BytesParser(policy=cast(Any, policy.default)).parsebytes(raw),
        )
        meta: dict[str, Any] = {
            "message_id": msg.get("Message-ID"),
            "subject": msg.get("Subject"),
            "date": msg.get("Date"),
            "x_gm_msgid": None,
            "x_gm_thrid": None,
            "labels": [],
        }
        return raw, meta

    def idle(self, timeout: int) -> bool:
        self.client.send(b"IDLE\r\n")
        self.client.readline() if hasattr(self.client, "readline") else None
        return True

    def close(self) -> None:
        try:
            self.client.logout()
        except Exception:
            pass
