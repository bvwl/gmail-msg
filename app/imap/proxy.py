"""每邮箱代理检查、SOCKS5 拨号和截断指数退避。"""

from __future__ import annotations

import asyncio
import random
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class ProxyMode(StrEnum):
    DIRECT = "direct"
    SOCKS5 = "socks5"


class ProxyState(StrEnum):
    DIRECT = "direct"
    CHECKING = "proxy_checking"
    WAITING = "waiting_for_proxy"
    UNAVAILABLE = "proxy_unavailable"
    CONNECTING = "connecting"
    IDLE = "idle"
    RECONNECTING = "reconnecting"
    AUTH_FAILED = "auth_failed"
    ERROR = "error"
    STOPPED = "stopped"


class ProxyError(ConnectionError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ProxyConfig:
    mode: ProxyMode
    host: str | None = None
    port: int | None = None
    username: str | None = None
    password: str | None = None
    dns_mode: str | None = None

    def validate(self) -> None:
        if self.mode is ProxyMode.DIRECT:
            if any((self.host, self.port, self.username, self.password, self.dns_mode)):
                raise ValueError("direct mode cannot contain proxy settings")
            return
        if not self.host or not self.port or self.dns_mode not in {"remote", "local"}:
            raise ValueError("socks5 requires host, port and dns_mode")


class Backoff:
    def __init__(
        self,
        base: float = 2.0,
        maximum: float = 300.0,
        jitter: float = 0.2,
        *,
        random_fn: Callable[[], float] = random.random,
    ) -> None:
        self.base, self.maximum, self.jitter, self.random_fn = base, maximum, jitter, random_fn
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    def next_delay(self) -> float:
        delay = min(self.maximum, self.base * (2**self.attempt))
        self.attempt += 1
        return float(delay * (1 - self.jitter + self.random_fn() * 2 * self.jitter))


async def wait_for_proxy(
    backoff: Backoff, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
) -> float:
    delay = backoff.next_delay()
    await sleep(delay)
    return delay


async def _open_target(
    host: str, port: int, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    try:
        return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        raise ProxyError("proxy_unavailable", str(exc)) from exc


async def open_socks5_connection(
    config: ProxyConfig, target_host: str, target_port: int, *, timeout: float = 5.0
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    config.validate()
    if config.mode is not ProxyMode.SOCKS5:
        raise ValueError("SOCKS5 dial requires socks5 mode")
    reader, writer = await _open_target(config.host or "", config.port or 0, timeout)
    try:
        methods = b"\x05\x01\x02" if config.username else b"\x05\x01\x00"
        writer.write(methods)
        await writer.drain()
        response = await asyncio.wait_for(reader.readexactly(2), timeout)
        if response[0] != 5 or response[1] == 0xFF:
            raise ProxyError("proxy_auth_failed")
        if response[1] == 0x02:
            if config.password is None or config.username is None:
                raise ProxyError("proxy_auth_failed")
            user, password = config.username.encode(), config.password.encode()
            writer.write(bytes([1, len(user)]) + user + bytes([len(password)]) + password)
            await writer.drain()
            auth = await asyncio.wait_for(reader.readexactly(2), timeout)
            if auth[1] != 0:
                raise ProxyError("proxy_auth_failed")
        if config.dns_mode == "remote":
            host_bytes = target_host.encode()
            request = (
                b"\x05\x01\x00\x03"
                + bytes([len(host_bytes)])
                + host_bytes
                + target_port.to_bytes(2, "big")
            )
        else:
            ip = socket.inet_aton(socket.gethostbyname(target_host))
            request = b"\x05\x01\x00\x01" + ip + target_port.to_bytes(2, "big")
        writer.write(request)
        await writer.drain()
        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        if head[1] != 0:
            raise ProxyError("proxy_connect_failed")
        atyp = head[3]
        length = 4 if atyp == 1 else 16 if atyp == 4 else (await reader.readexactly(1))[0]
        await reader.readexactly(length + 2)
        return reader, writer
    except (OSError, asyncio.IncompleteReadError, TimeoutError) as exc:
        writer.close()
        await writer.wait_closed()
        raise ProxyError("proxy_unavailable", str(exc)) from exc


async def check_proxy(
    config: ProxyConfig, *, timeout: float = 5.0, check_host: str = "1.1.1.1", check_port: int = 443
) -> None:
    config.validate()
    if config.mode is ProxyMode.DIRECT:
        return
    reader, writer = await open_socks5_connection(config, check_host, check_port, timeout=timeout)
    writer.close()
    await writer.wait_closed()
