from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from app.config import Settings
from app.redis_client import StreamMessage
from app.workers.gmail_sync_worker import GmailSyncWorker


class FakeSession:
    def __init__(self, request: SimpleNamespace) -> None:
        self.request = request

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    def begin(self) -> FakeSession:
        return self

    async def get(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
        return self.request


class FakeRedis:
    def __init__(self) -> None:
        self.acks: list[str] = []
        self.dead_letters: list[str] = []

    async def ack(self, _stream: str, _group: str, message_id: str) -> int:
        self.acks.append(message_id)
        return 1

    async def move_to_dead_letter(
        self, _stream: str, message: StreamMessage, **_kwargs: object
    ) -> str:
        self.dead_letters.append(message.id)
        return "dlq-1"


class FakeSync:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    async def sync_mailbox(self, *_args: object, **_kwargs: object) -> None:
        if self.error:
            raise self.error


def _message() -> StreamMessage:
    return StreamMessage(
        "1-0",
        {"mailbox_id": str(uuid4()), "request_id": str(uuid4())},
    )


@pytest.mark.asyncio
async def test_worker_acks_only_after_sync_and_request_commit() -> None:
    request = SimpleNamespace(
        attempt_count=0,
        status="enqueued",
        last_error_code=None,
        next_attempt_at=None,
    )
    redis = FakeRedis()
    worker = GmailSyncWorker(
        FakeSync(),  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        lambda: FakeSession(request),  # type: ignore[arg-type]
        Settings(_env_file=None, gmail_sync_task_timeout_seconds=2),
    )

    assert await worker.process_message(_message()) is True
    assert request.status == "completed"
    assert redis.acks == ["1-0"]


@pytest.mark.asyncio
async def test_worker_keeps_failed_message_pending_for_recovery() -> None:
    request = SimpleNamespace(
        attempt_count=0,
        status="enqueued",
        last_error_code=None,
        next_attempt_at=None,
    )
    redis = FakeRedis()
    worker = GmailSyncWorker(
        FakeSync(RuntimeError("temporary")),  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        lambda: FakeSession(request),  # type: ignore[arg-type]
        Settings(_env_file=None, gmail_sync_task_timeout_seconds=2),
    )

    assert await worker.process_message(_message()) is False
    assert request.status == "retry"
    assert redis.acks == []
