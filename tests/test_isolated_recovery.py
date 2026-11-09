from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from app.db.base import Mailbox
from app.imap.client import ImapSettings
from app.imap.proxy import Backoff, ProxyError, ProxyMode
from app.imap.worker import MailboxWorker


class _Folder(SimpleNamespace):
    id: Any
    imap_name: str
    monitor_enabled: bool
    baseline_uid: int | None
    last_seen_uid: int
    uidvalidity: int | None
    uidnext: int | None


class _Result:
    def __init__(self, folders: list[_Folder]) -> None:
        self._folders = folders

    def scalars(self) -> list[_Folder]:
        return self._folders


class _Session:
    def __init__(self, folders: list[_Folder]) -> None:
        self.folders = folders

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, _statement: object) -> _Result:
        return _Result(self.folders)


class _Sync:
    def __init__(self, folder: _Folder, stop_event: asyncio.Event, *, stop_after: int) -> None:
        self.folder = folder
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.calls: list[int] = []
        self.keys: set[str] = set()

    async def store_message(self, *_args: object, **kwargs: object) -> None:
        uid = cast(int, _args[2])
        self.calls.append(uid)
        raw = cast(bytes, _args[4])
        key = raw.decode()
        self.keys.add(key)

    async def advance_cursor(
        self, _folder_id: object, uidvalidity: int, uidnext: int, last_seen_uid: int
    ) -> None:
        self.folder.uidvalidity = uidvalidity
        self.folder.uidnext = uidnext
        self.folder.last_seen_uid = last_seen_uid
        if len(self.calls) >= self.stop_after:
            self.stop_event.set()


class _Client:
    def __init__(self, *, disconnect: bool = False, uids: list[int] | None = None) -> None:
        self.disconnect = disconnect
        self.uids = uids or []
        self.closed = False

    def list_folders(self) -> list[tuple[str, str]]:
        return [("INBOX", "\\Inbox"), ("Spam", "\\Junk")]

    def select(self, _name: str) -> tuple[int, int, int]:
        return (7, 13, 12)

    def search_uids(self, _start_uid: int) -> list[int]:
        return self.uids

    def fetch_message(self, uid: int) -> tuple[bytes, dict[str, object]]:
        return f"message-{uid}".encode(), {}

    def idle(self, _timeout: int) -> bool:
        if self.disconnect:
            raise ConnectionError("socket closed")
        return True

    def close(self) -> None:
        self.closed = True


class _Factory:
    def __init__(self, clients: list[_Client | Exception]) -> None:
        self.clients = clients
        self.settings: list[object] = []

    async def connect(self, settings: object) -> _Client:
        self.settings.append(settings)
        item = self.clients.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _mailbox(mailbox_id: object, *, mode: str = "direct") -> SimpleNamespace:
    return SimpleNamespace(
        id=mailbox_id,
        enabled=True,
        proxy_mode=mode,
        proxy_host="proxy.test" if mode == "socks5" else None,
        proxy_port=1080 if mode == "socks5" else None,
        proxy_username=None,
        proxy_dns_mode="remote" if mode == "socks5" else None,
        imap_host="imap.test",
        imap_port=993,
        username="MAILBOX",
        app_password_secret_ref="SECRET_REF",
    )


@pytest.mark.asyncio
async def test_idle_disconnect_reconnects_and_replays_uid_range_without_duplicate_entities(
) -> None:
    mailbox_id = uuid4()
    folder = _Folder(
        id=uuid4(),
        imap_name="INBOX",
        monitor_enabled=True,
        baseline_uid=10,
        last_seen_uid=10,
        uidvalidity=7,
        uidnext=11,
    )
    stop_event = asyncio.Event()
    sync = _Sync(folder, stop_event, stop_after=4)
    factory = _Factory([_Client(disconnect=True), _Client(uids=[11, 12]), _Client(uids=[11, 12])])
    statuses: list[dict[str, str | None]] = []
    worker = MailboxWorker(
        mailbox_id,
        lambda: _Session([folder]),  # type: ignore[arg-type]
        factory,  # type: ignore[arg-type]
        sync,  # type: ignore[arg-type]
        backoff=Backoff(base=0, maximum=0, jitter=0),
        stop_event=stop_event,
    )
    async def load_mailbox() -> Mailbox:
        return cast(Mailbox, _mailbox(mailbox_id))

    async def set_status(**kwargs: str | None) -> None:
        statuses.append(kwargs)

    async def ensure_folders(_folders: list[tuple[str, str]]) -> None:
        return None

    worker._load_mailbox = load_mailbox  # type: ignore[method-assign]
    worker._set_status = set_status  # type: ignore[method-assign]
    worker._ensure_special_folders = ensure_folders  # type: ignore[assignment,method-assign]

    await worker.run_forever()

    assert any(item.get("connection_state") == "reconnecting" for item in statuses)
    assert any(item.get("connection_state") == "idle" for item in statuses)
    assert sync.calls == [11, 12, 11, 12]
    assert sync.keys == {"message-11", "message-12"}
    assert folder.last_seen_uid == 12
    assert folder.uidvalidity == 7


@pytest.mark.asyncio
async def test_proxy_recovery_never_falls_back_to_direct_and_resumes_sync() -> None:
    mailbox_id = uuid4()
    folder = _Folder(
        id=uuid4(),
        imap_name="INBOX",
        monitor_enabled=True,
        baseline_uid=20,
        last_seen_uid=20,
        uidvalidity=7,
        uidnext=21,
    )
    stop_event = asyncio.Event()
    sync = _Sync(folder, stop_event, stop_after=1)
    factory = _Factory([ProxyError("proxy_unavailable"), _Client(uids=[21])])
    statuses: list[dict[str, str | None]] = []
    worker = MailboxWorker(
        mailbox_id,
        lambda: _Session([folder]),  # type: ignore[arg-type]
        factory,  # type: ignore[arg-type]
        sync,  # type: ignore[arg-type]
        backoff=Backoff(base=0, maximum=0, jitter=0),
        stop_event=stop_event,
    )
    async def load_mailbox() -> Mailbox:
        return cast(Mailbox, _mailbox(mailbox_id, mode="socks5"))

    async def set_status(**kwargs: str | None) -> None:
        statuses.append(kwargs)

    async def ensure_folders(_folders: list[tuple[str, str]]) -> None:
        return None

    worker._load_mailbox = load_mailbox  # type: ignore[method-assign]
    worker._set_status = set_status  # type: ignore[method-assign]
    worker._ensure_special_folders = ensure_folders  # type: ignore[assignment,method-assign]

    await worker.run_forever()

    assert any(item.get("proxy_status") == "waiting_for_proxy" for item in statuses)
    assert any(item.get("proxy_status") == "idle" for item in statuses)
    assert sync.calls == [21]
    assert all(
        cast(ImapSettings, settings).proxy.mode is ProxyMode.SOCKS5
        for settings in factory.settings
    )
    assert all(
        cast(ImapSettings, settings).proxy.mode is not ProxyMode.DIRECT
        for settings in factory.settings
    )
