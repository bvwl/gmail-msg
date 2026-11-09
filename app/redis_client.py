"""Redis asyncio 客户端和连接池生命周期。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from redis.asyncio import Redis

from app.config import Settings, get_settings


class RedisManager:
    """管理 redis-py asyncio 客户端，为后续 Streams 保留统一入口。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=settings.redis_connect_timeout_seconds,
            socket_timeout=settings.redis_socket_timeout_seconds,
            health_check_interval=settings.redis_health_check_interval_seconds,
        )

    async def check(self) -> None:
        """执行 PING，失败时向上抛出 Redis 异常。"""

        await self.client.ping()

    async def xadd(self, stream: str, fields: dict[str, str], *, maxlen: int | None = None) -> str:
        """为后续 Redis Streams 使用保留的最小扩展接口。"""

        result = await self.client.xadd(stream, cast(Any, fields), maxlen=maxlen, approximate=True)
        return str(result)

    async def ensure_consumer_group(self, stream: str, group: str) -> None:
        """创建 Consumer Group；已存在时保持幂等。"""

        try:
            await self.client.xgroup_create(stream, group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc).upper():
                raise

    async def enqueue_sync(
        self,
        stream: str,
        *,
        mailbox_id: str,
        observed_history_id: str | None,
        request_id: str,
        reason: str,
        maxlen: int | None = None,
    ) -> str:
        fields = {
            "mailbox_id": mailbox_id,
            "observed_history_id": observed_history_id or "",
            "request_id": request_id,
            "reason": reason,
        }
        return await self.xadd(stream, fields, maxlen=maxlen)

    async def read_group(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        count: int = 1,
        block_ms: int = 5_000,
        stream_id: str = ">",
    ) -> list[StreamMessage]:
        await self.ensure_consumer_group(stream, group)
        rows = await self.client.xreadgroup(
            group,
            consumer,
            {stream: stream_id},
            count=count,
            block=block_ms,
        )
        messages: list[StreamMessage] = []
        for _stream_name, entries in cast(Any, rows or []):
            for message_id, fields in entries:
                messages.append(StreamMessage(str(message_id), dict(fields)))
        return messages

    async def ack(self, stream: str, group: str, message_id: str) -> int:
        return int(await self.client.xack(stream, group, message_id))

    async def pending(self, stream: str, group: str) -> dict[str, Any]:
        result = await self.client.xpending(stream, group)
        if isinstance(result, dict):
            return dict(result)
        return {"raw": result}

    async def autoclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        *,
        min_idle_ms: int,
        start_id: str = "0-0",
        count: int = 100,
    ) -> tuple[str, list[StreamMessage], list[str]]:
        result = await self.client.xautoclaim(
            stream,
            group,
            consumer,
            min_idle_time=min_idle_ms,
            start_id=start_id,
            count=count,
        )
        next_id = str(result[0]) if result else "0-0"
        entries = result[1] if len(result) > 1 else []
        deleted = [str(item) for item in (result[2] if len(result) > 2 else [])]
        return next_id, [StreamMessage(str(mid), dict(fields)) for mid, fields in entries], deleted

    async def move_to_dead_letter(
        self,
        dead_letter_stream: str,
        message: StreamMessage,
        *,
        error_code: str,
        attempts: int,
        maxlen: int | None = None,
    ) -> str:
        fields = {**message.fields, "error_code": error_code, "attempts": str(attempts)}
        return await self.xadd(dead_letter_stream, fields, maxlen=maxlen)

    async def acquire_lock(self, key: str, token: str, *, ttl_seconds: int) -> bool:
        result = await self.client.set(key, token, nx=True, ex=ttl_seconds)
        return bool(result)

    async def release_lock(self, key: str, token: str) -> bool:
        script = (
            "if redis.call('get', KEYS[1]) == ARGV[1] "
            "then return redis.call('del', KEYS[1]) else return 0 end"
        )
        return bool(await self.client.eval(script, 1, key, token))

    async def close(self) -> None:
        await self.client.aclose()


def build_redis(settings: Settings | None = None) -> RedisManager:
    return RedisManager(settings or get_settings())


@dataclass(frozen=True)
class StreamMessage:
    id: str
    fields: Mapping[str, str]

    @property
    def mailbox_id(self) -> str:
        return str(self.fields.get("mailbox_id", ""))

    @property
    def observed_history_id(self) -> str | None:
        value = str(self.fields.get("observed_history_id", "")).strip()
        return value or None

    @property
    def request_id(self) -> str:
        return str(self.fields.get("request_id", ""))


def new_consumer_name(prefix: str = "worker") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def encode_stream_fields(fields: Mapping[str, Any]) -> dict[str, str]:
    return {
        str(key): value if isinstance(value, str) else json.dumps(value, separators=(",", ":"))
        for key, value in fields.items()
    }
