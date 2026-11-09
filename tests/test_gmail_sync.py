from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from app.config import Settings
from app.gmail.client import GmailApiClient, GmailPage
from app.gmail.sync import GmailSyncService


class FakeClient:
    async def list_history(self, _mailbox_id: object, **kwargs: object) -> GmailPage:
        if kwargs.get("page_token"):
            return GmailPage(
                ({"messagesAdded": [{"message": {"id": "m2"}}]},),
                None,
                raw={"historyId": "30"},
            )
        return GmailPage(
            (
                {
                    "messagesAdded": [
                        {"message": {"id": "m1"}},
                        {"message": {"id": "m1"}},
                    ]
                },
            ),
            "p2",
            raw={"historyId": "20"},
        )


@pytest.mark.asyncio
async def test_collect_history_deduplicates_message_ids_and_keeps_final_cursor() -> None:
    service = GmailSyncService.__new__(GmailSyncService)
    service.client = cast(GmailApiClient, FakeClient())
    service.settings = Settings(_env_file=None)

    pages, message_ids, cursor = await service._collect_history(uuid4(), "10")

    assert pages == 2
    assert message_ids == ["m1", "m2"]
    assert cursor == "30"
