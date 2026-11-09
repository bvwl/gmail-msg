"""按 Gmail quota units 进行 Redis 共享限流。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID


class QuotaExceededError(TimeoutError):
    """在等待窗口内没有足够 Gmail 配额。"""


class QuotaLimiter(Protocol):
    async def acquire(self, mailbox_id: UUID, units: int) -> None: ...


@dataclass(frozen=True)
class GmailQuotaConfig:
    """配额窗口配置。

    Gmail 官方 quota unit 会随方法和政策变化，因此这里只保存可调整的预算，
    方法权重由客户端显式传入。
    """

    units_per_minute: int = 6_000
    burst_units: int = 1_000
    wait_timeout_seconds: float = 30.0
    key_prefix: str = "gmail:quota:"


_ACQUIRE_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
local requested = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])
if current + requested <= limit then
  local value = redis.call('INCRBY', KEYS[1], requested)
  redis.call('EXPIRE', KEYS[1], ttl)
  return {1, value, ttl}
end
local remaining = redis.call('TTL', KEYS[1])
if remaining < 1 then remaining = ttl end
return {0, current, remaining}
"""


class RedisQuotaLimiter:
    """使用 Redis Lua 原子扣减预算，多个 Worker 可共享同一邮箱窗口。"""

    def __init__(
        self,
        client: Any,
        config: GmailQuotaConfig | None = None,
        *,
        sleep: Any = asyncio.sleep,
        clock: Any = time.monotonic,
    ) -> None:
        self.client = client
        self.config = config or GmailQuotaConfig()
        self.sleep = sleep
        self.clock = clock
        self._fallback_lock = asyncio.Lock()

    def _key(self, mailbox_id: UUID) -> str:
        return f"{self.config.key_prefix}{mailbox_id}"

    async def _try_acquire(self, key: str, units: int) -> tuple[bool, float]:
        eval_method = getattr(self.client, "eval", None)
        if eval_method is not None:
            result = await eval_method(
                _ACQUIRE_SCRIPT,
                1,
                key,
                units,
                self.config.units_per_minute + self.config.burst_units,
                60,
            )
            granted = bool(int(result[0]))
            return granted, max(0.05, float(result[2]))

        # 仅用于不支持 EVAL 的测试替身；真实 redis-py 会走 Lua 原子路径。
        async with self._fallback_lock:
            current_raw = await self.client.get(key)
            current = int(current_raw or 0)
            limit = self.config.units_per_minute + self.config.burst_units
            if current + units <= limit:
                await self.client.incrby(key, units)
                await self.client.expire(key, 60)
                return True, 1.0
            ttl = await self.client.ttl(key)
            return False, max(0.05, float(ttl if ttl and ttl > 0 else 1))

    async def acquire(self, mailbox_id: UUID, units: int) -> None:
        if units <= 0:
            raise ValueError("quota units must be positive")
        deadline = self.clock() + self.config.wait_timeout_seconds
        key = self._key(mailbox_id)
        while True:
            granted, retry_after = await self._try_acquire(key, units)
            if granted:
                incr = getattr(self.client, "incr", None)
                if incr is not None:
                    await incr(f"{key}:requests")
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                incr = getattr(self.client, "incr", None)
                if incr is not None:
                    await incr(f"{key}:limited")
                raise QuotaExceededError(f"Gmail quota budget exhausted for mailbox {mailbox_id}")
            await self.sleep(min(retry_after, remaining))


class InMemoryQuotaLimiter:
    """离线单元测试使用的同语义限流器。"""

    def __init__(
        self, config: GmailQuotaConfig | None = None, *, clock: Any = time.monotonic
    ) -> None:
        self.config = config or GmailQuotaConfig()
        self.clock = clock
        self._entries: dict[UUID, tuple[float, int]] = {}
        self._lock = asyncio.Lock()
        self.consumed: dict[UUID, int] = {}
        self.limited: dict[UUID, int] = {}

    async def acquire(self, mailbox_id: UUID, units: int) -> None:
        if units <= 0:
            raise ValueError("quota units must be positive")
        now = self.clock()
        async with self._lock:
            started, used = self._entries.get(mailbox_id, (now, 0))
            if now - started >= 60:
                started, used = now, 0
            if used + units > self.config.units_per_minute + self.config.burst_units:
                self.limited[mailbox_id] = self.limited.get(mailbox_id, 0) + 1
                raise QuotaExceededError(f"Gmail quota budget exhausted for mailbox {mailbox_id}")
            self._entries[mailbox_id] = (started, used + units)
            self.consumed[mailbox_id] = self.consumed.get(mailbox_id, 0) + units
