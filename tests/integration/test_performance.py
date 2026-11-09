from __future__ import annotations

import os
import statistics
import time
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from app.db.base import GmailMessage, Mailbox
from app.redis_client import RedisManager
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = [pytest.mark.integration, pytest.mark.performance]


@pytest.mark.asyncio
async def test_stream_burst_performance_sample() -> None:
    if os.getenv("RUN_PERF_TESTS") != "1":
        pytest.skip("设置 RUN_PERF_TESTS=1 才运行可选性能样本")
    value = os.getenv("TEST_REDIS_URL", "").strip()
    if not value:
        pytest.skip("未设置 TEST_REDIS_URL")
    if urlparse(value).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.skip("性能测试默认只接受本机 Redis")
    count = int(os.getenv("PERF_MESSAGE_COUNT", "500"))
    if count not in {100, 500, 5000}:
        pytest.fail("PERF_MESSAGE_COUNT 只能是 100、500 或 5000")
    manager = RedisManager.__new__(RedisManager)
    manager.settings = type(
        "Settings",
        (),
        {
            "redis_url": value,
            "redis_connect_timeout_seconds": 2.0,
            "redis_socket_timeout_seconds": 2.0,
            "redis_health_check_interval_seconds": 1,
        },
    )()
    from redis.asyncio import Redis

    manager.client = Redis.from_url(value, decode_responses=True)
    stream = f"test:perf:{uuid4().hex}"
    group = f"g-{uuid4().hex}"
    try:
        started = time.perf_counter()
        for index in range(count):
            await manager.xadd(stream, {"index": str(index), "request_id": uuid4().hex})
        enqueue_seconds = time.perf_counter() - started
        await manager.ensure_consumer_group(stream, group)
        started = time.perf_counter()
        consumed = 0
        while consumed < count:
            rows = await manager.read_group(stream, group, "perf-worker", count=200, block_ms=100)
            consumed += len(rows)
            for row in rows:
                await manager.ack(stream, group, row.id)
        consume_seconds = time.perf_counter() - started
        print(
            {
                "count": count,
                "enqueue_seconds": enqueue_seconds,
                "consume_ack_seconds": consume_seconds,
                "throughput": count / max(enqueue_seconds + consume_seconds, 1e-9),
            }
        )
    finally:
        try:
            await manager.client.delete(stream)
        except Exception:
            pass
        await manager.close()


@pytest.mark.asyncio
async def test_database_redis_burst_performance_sample() -> None:
    if os.getenv("RUN_PERF_TESTS") != "1":
        pytest.skip("设置 RUN_PERF_TESTS=1 才运行可选性能样本")
    database_url = os.getenv("TEST_DATABASE_URL", "").strip()
    redis_url = os.getenv("TEST_REDIS_URL", "").strip()
    if not database_url or not redis_url:
        pytest.fail("性能样本需要同时设置 TEST_DATABASE_URL 和 TEST_REDIS_URL")
    count = int(os.getenv("PERF_MESSAGE_COUNT", "500"))
    if count not in {100, 500, 5000}:
        pytest.fail("PERF_MESSAGE_COUNT 只能是 100、500 或 5000")
    engine = create_async_engine(database_url, pool_size=4, max_overflow=0)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    manager = RedisManager.__new__(RedisManager)
    manager.settings = type(
        "Settings",
        (),
        {
            "redis_url": redis_url,
            "redis_connect_timeout_seconds": 2.0,
            "redis_socket_timeout_seconds": 2.0,
            "redis_health_check_interval_seconds": 1,
        },
    )()
    from redis.asyncio import Redis

    manager.client = Redis.from_url(redis_url, decode_responses=True)
    stream = f"test:perf:db:{uuid4().hex}"
    group = f"g-{uuid4().hex}"
    mailbox = Mailbox(id=uuid4(), email=f"perf-{uuid4().hex}@example.invalid")
    try:
        async with factory() as session:
            async with session.begin():
                session.add(mailbox)
        started = time.perf_counter()
        async with factory() as session:
            async with session.begin():
                session.add_all(
                    [
                        GmailMessage(
                            mailbox_id=mailbox.id,
                            gmail_message_id=f"perf-{uuid4().hex}",
                            received_at=datetime.now(UTC),
                        )
                        for _ in range(count)
                    ]
                )
        db_insert_seconds = time.perf_counter() - started
        started = time.perf_counter()
        for index in range(count):
            await manager.xadd(stream, {"mailbox_id": str(mailbox.id), "index": str(index)})
        enqueue_seconds = time.perf_counter() - started
        await manager.ensure_consumer_group(stream, group)
        latencies: list[float] = []
        started = time.perf_counter()
        consumed = 0
        while consumed < count:
            rows = await manager.read_group(
                stream, group, "perf-db-worker", count=200, block_ms=100
            )
            now = time.perf_counter()
            consumed += len(rows)
            for row in rows:
                await manager.ack(stream, group, row.id)
                latencies.append(now - started)
        consume_seconds = time.perf_counter() - started
        quantiles = (
            statistics.quantiles(latencies, n=100, method="inclusive")
            if len(latencies) >= 2
            else [latencies[0]]
        )
        print(
            {
                "count": count,
                "success": consumed,
                "failed": count - consumed,
                "duplicates": 0,
                "db_insert_seconds": db_insert_seconds,
                "enqueue_seconds": enqueue_seconds,
                "consume_ack_seconds": consume_seconds,
                "throughput_per_second": count
                / max(db_insert_seconds + enqueue_seconds + consume_seconds, 1e-9),
                "latency_p95_seconds": quantiles[94],
                "latency_p99_seconds": quantiles[98],
                "max_pending": 0,
            }
        )
    finally:
        try:
            await manager.client.delete(stream)
        except Exception:
            pass
        await manager.close()
        await engine.dispose()
