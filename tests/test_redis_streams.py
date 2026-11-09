from __future__ import annotations

from typing import cast

import pytest
from app.config import Settings
from app.redis_client import RedisManager, StreamMessage
from redis.asyncio import Redis


class FakeRedis:
    def __init__(self) -> None:
        self.created: list[tuple[str, str]] = []
        self.acked: list[str] = []

    async def xgroup_create(self, stream: str, group: str, **_kwargs: object) -> None:
        self.created.append((stream, group))

    async def xreadgroup(
        self, *_args: object, **_kwargs: object
    ) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
        return [("gmail:sync", [("1-0", {"mailbox_id": "m", "request_id": "r"})])]

    async def xack(self, _stream: str, _group: str, message_id: str) -> int:
        self.acked.append(message_id)
        return 1

    async def xadd(self, _stream: str, _fields: object, **_kwargs: object) -> str:
        return "2-0"


@pytest.mark.asyncio
async def test_stream_group_read_and_ack() -> None:
    client = FakeRedis()
    manager = RedisManager.__new__(RedisManager)
    manager.client = cast(Redis, client)
    manager.settings = Settings(_env_file=None)

    messages = await manager.read_group("gmail:sync", "workers", "c1")
    assert messages == [StreamMessage("1-0", {"mailbox_id": "m", "request_id": "r"})]
    assert await manager.ack("gmail:sync", "workers", "1-0") == 1
    assert client.acked == ["1-0"]
