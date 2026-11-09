from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from app.redis_client import RedisManager

pytestmark = pytest.mark.integration


def _test_redis_url() -> str:
    value = os.getenv("TEST_REDIS_URL", "").strip()
    if not value:
        if os.getenv("REQUIRE_REAL_INFRA") == "1":
            pytest.fail("REQUIRE_REAL_INFRA=1 但未设置 TEST_REDIS_URL")
        pytest.skip("未设置 TEST_REDIS_URL，真实 Redis Streams 验收未执行")
    if not value.startswith("redis://"):
        pytest.fail("TEST_REDIS_URL 必须使用 redis://")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("为避免误连生产 Redis，默认只接受本机 TEST_REDIS_URL")
    return value


@pytest.mark.asyncio
async def test_stream_group_pending_autoclaim_ack_and_dlq() -> None:
    manager = RedisManager.__new__(RedisManager)
    manager.settings = type(
        "Settings",
        (),
        {
            "redis_url": _test_redis_url(),
            "redis_connect_timeout_seconds": 2.0,
            "redis_socket_timeout_seconds": 2.0,
            "redis_health_check_interval_seconds": 1,
        },
    )()
    from redis.asyncio import Redis

    manager.client = Redis.from_url(manager.settings.redis_url, decode_responses=True)
    stream = f"test:gmail:sync:{uuid4().hex}"
    dlq = f"{stream}:dlq"
    group = f"g-{uuid4().hex}"
    try:
        message_id = await manager.xadd(
            stream, {"mailbox_id": str(uuid4()), "request_id": str(uuid4()), "attempts": "0"}
        )
        messages = await manager.read_group(stream, group, "consumer-a", block_ms=100, count=1)
        assert len(messages) == 1 and messages[0].id == message_id
        pending = await manager.pending(stream, group)
        assert int(pending.get("pending", 0)) == 1
        next_id, claimed, _deleted = await manager.autoclaim(
            stream, group, "consumer-b", min_idle_ms=0, count=10
        )
        assert next_id and claimed and claimed[0].id == message_id
        assert await manager.ack(stream, group, message_id) == 1
        assert int((await manager.pending(stream, group)).get("pending", 0)) == 0
        dead_id = await manager.move_to_dead_letter(
            dlq, messages[0], error_code="test_failure", attempts=3
        )
        assert dead_id
        assert await manager.client.xlen(dlq) == 1
    finally:
        try:
            await manager.client.delete(stream, dlq)
        except Exception:
            pass
        await manager.close()


@pytest.mark.asyncio
async def test_multiple_consumers_do_not_ack_before_processing() -> None:
    manager = RedisManager.__new__(RedisManager)
    manager.settings = type(
        "Settings",
        (),
        {
            "redis_url": _test_redis_url(),
            "redis_connect_timeout_seconds": 2.0,
            "redis_socket_timeout_seconds": 2.0,
            "redis_health_check_interval_seconds": 1,
        },
    )()
    from redis.asyncio import Redis

    manager.client = Redis.from_url(manager.settings.redis_url, decode_responses=True)
    stream = f"test:gmail:sync:{uuid4().hex}"
    group = f"g-{uuid4().hex}"
    try:
        for _ in range(20):
            await manager.xadd(stream, {"mailbox_id": str(uuid4()), "request_id": str(uuid4())})
        await manager.ensure_consumer_group(stream, group)
        first = await manager.read_group(stream, group, "consumer-a", count=10, block_ms=100)
        second = await manager.read_group(stream, group, "consumer-b", count=10, block_ms=100)
        assert len(first) + len(second) == 20
        assert int((await manager.pending(stream, group)).get("pending", 0)) == 20
        await asyncio.gather(*(manager.ack(stream, group, item.id) for item in first + second))
        assert int((await manager.pending(stream, group)).get("pending", 0)) == 0
    finally:
        try:
            await manager.client.delete(stream)
        except Exception:
            pass
        await manager.close()
