from __future__ import annotations

from uuid import uuid4

import pytest
from app.gmail.quota import (
    GmailQuotaConfig,
    InMemoryQuotaLimiter,
    QuotaExceededError,
    RedisQuotaLimiter,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    async def eval(
        self, _script: str, _numkeys: int, key: str, units: int, limit: int, _ttl: int
    ) -> list[int]:
        current = self.values.get(key, 0)
        if current + units <= limit:
            current += units
            self.values[key] = current
            return [1, current, 60]
        return [0, current, 60]

    async def incr(self, key: str) -> int:
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]


@pytest.mark.asyncio
async def test_quota_consumption_and_exhaustion() -> None:
    mailbox_id = uuid4()
    limiter = InMemoryQuotaLimiter(GmailQuotaConfig(units_per_minute=10, burst_units=0))

    await limiter.acquire(mailbox_id, 6)
    await limiter.acquire(mailbox_id, 4)
    with pytest.raises(QuotaExceededError):
        await limiter.acquire(mailbox_id, 1)

    assert limiter.consumed[mailbox_id] == 10
    assert limiter.limited[mailbox_id] == 1


@pytest.mark.asyncio
async def test_each_mailbox_has_an_independent_budget() -> None:
    limiter = InMemoryQuotaLimiter(GmailQuotaConfig(units_per_minute=5, burst_units=0))
    first, second = uuid4(), uuid4()

    await limiter.acquire(first, 5)
    await limiter.acquire(second, 5)

    assert limiter.consumed[first] == limiter.consumed[second] == 5


@pytest.mark.asyncio
async def test_redis_limiters_share_one_mailbox_budget() -> None:
    redis = FakeRedis()
    config = GmailQuotaConfig(units_per_minute=10, burst_units=0)
    first = RedisQuotaLimiter(redis, config)
    second = RedisQuotaLimiter(redis, config)
    mailbox_id = uuid4()

    await first.acquire(mailbox_id, 4)
    await second.acquire(mailbox_id, 6)

    assert redis.values[f"gmail:quota:{mailbox_id}"] == 10
    assert redis.values[f"gmail:quota:{mailbox_id}:requests"] == 2
