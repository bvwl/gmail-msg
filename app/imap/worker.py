"""单邮箱 IMAP IDLE Worker。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.db.base import Mailbox, MailboxFolder
from app.imap.client import ImapConnectionFactory, ImapSettings
from app.imap.proxy import Backoff, ProxyError, ProxyMode, ProxyState
from app.security.secrets import MailboxCredentialCipher
from app.services.sync import ImapSyncService


class MailboxWorker:
    def __init__(
        self,
        mailbox_id: UUID,
        session_factory: async_sessionmaker[AsyncSession],
        connection_factory: ImapConnectionFactory,
        sync_service: ImapSyncService,
        *,
        backoff: Backoff | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self.mailbox_id, self.session_factory, self.connection_factory, self.sync_service = (
            mailbox_id,
            session_factory,
            connection_factory,
            sync_service,
        )
        self.backoff = backoff or Backoff()
        self.stop_event = stop_event or asyncio.Event()
        self.proxy_state = ProxyState.STOPPED
        self.last_error_code: str | None = None

    async def _load_mailbox(self) -> Mailbox | None:
        async with self.session_factory() as session:
            return await session.get(Mailbox, self.mailbox_id)

    async def _set_status(
        self,
        *,
        proxy_status: str | None = None,
        connection_state: str | None = None,
        error: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                mailbox = await session.get(Mailbox, self.mailbox_id, with_for_update=True)
                if mailbox is None:
                    return
                if proxy_status:
                    mailbox.proxy_status = proxy_status
                if connection_state:
                    mailbox.connection_state = connection_state
                mailbox.last_error_code = error
                mailbox.last_error_at = datetime.now(UTC) if error else mailbox.last_error_at
                mailbox.last_heartbeat_at = datetime.now(UTC)

    async def run_once(self) -> None:
        mailbox = await self._load_mailbox()
        if mailbox is None or not mailbox.enabled:
            await self._set_status(proxy_status="stopped", connection_state="stopped")
            return
        cipher = None
        app_password_encrypted = getattr(mailbox, "app_password_encrypted", None)
        proxy_password_encrypted = getattr(mailbox, "proxy_password_encrypted", None)
        if app_password_encrypted or proxy_password_encrypted:
            cipher = MailboxCredentialCipher(get_settings().mailbox_credentials_fernet_key)
        proxy = __import__("app.imap.proxy", fromlist=["ProxyConfig"]).ProxyConfig(
            ProxyMode(mailbox.proxy_mode),
            mailbox.proxy_host,
            mailbox.proxy_port,
            mailbox.proxy_username,
            cipher.decrypt(proxy_password_encrypted)
            if cipher and proxy_password_encrypted
            else None,
            mailbox.proxy_dns_mode,
        )
        app_password = (
            cipher.decrypt(app_password_encrypted)
            if cipher and app_password_encrypted
            else None
        )
        settings = ImapSettings(
            mailbox.imap_host,
            mailbox.imap_port,
            mailbox.username,
            mailbox.app_password_secret_ref,
            proxy,
            app_password=app_password,
            allowed_hosts=tuple(get_settings().imap_allowed_hosts),
            allowed_proxy_hosts=tuple(get_settings().proxy_allowed_hosts),
        )
        client = None
        try:
            if proxy.mode is ProxyMode.SOCKS5:
                await self._set_status(proxy_status="proxy_checking", connection_state="stopped")
            else:
                await self._set_status(proxy_status="direct", connection_state="connecting")
            client = await self.connection_factory.connect(settings)
            await self._set_status(
                proxy_status="idle" if proxy.mode is ProxyMode.SOCKS5 else "direct",
                connection_state="idle",
                error=None,
            )
            self.backoff.reset()
            folders = await asyncio.to_thread(client.list_folders)
            await self._ensure_special_folders(folders)
            async with self.session_factory() as session:
                result = await session.execute(
                    select(MailboxFolder).where(
                        MailboxFolder.mailbox_id == self.mailbox_id,
                        MailboxFolder.monitor_enabled.is_(True),
                    )
                )
                configured = list(result.scalars())
            for folder in configured:
                if self.stop_event.is_set():
                    break
                uidvalidity, uidnext, _ = await asyncio.to_thread(client.select, folder.imap_name)
                if folder.uidvalidity is not None and folder.uidvalidity != uidvalidity:
                    await self._mark_folder_error(folder.id, "uidvalidity_changed")
                    continue
                start = (folder.last_seen_uid + 1) if folder.baseline_uid is not None else uidnext
                if folder.baseline_uid is None:
                    await self._set_baseline(folder.id, uidvalidity, uidnext)
                    continue
                uids = await asyncio.to_thread(client.search_uids, start)
                for uid in uids:
                    raw, meta = await asyncio.to_thread(client.fetch_message, uid)
                    await self.sync_service.store_message(
                        self.mailbox_id,
                        folder.id,
                        uid,
                        uidvalidity,
                        raw,
                        x_gm_msgid=meta.get("x_gm_msgid"),
                        x_gm_thrid=meta.get("x_gm_thrid"),
                    )
                await self.sync_service.advance_cursor(
                    folder.id, uidvalidity, uidnext, max(uids, default=folder.last_seen_uid)
                )
            idle_ok = await asyncio.to_thread(client.idle, 300)
            if not idle_ok:
                raise ConnectionError("imap_idle_disconnected")
        except ProxyError as exc:
            self.last_error_code = exc.code
            await self._set_status(
                proxy_status="waiting_for_proxy", connection_state="reconnecting", error=exc.code
            )
            await asyncio.sleep(self.backoff.next_delay())
        except PermissionError:
            await self._set_status(
                proxy_status="auth_failed", connection_state="auth_failed", error="imap_auth_failed"
            )
        except Exception as exc:
            error_code = (
                "imap_idle_disconnected"
                if str(exc) == "imap_idle_disconnected"
                else "imap_connection_failed"
            )
            await self._set_status(connection_state="reconnecting", error=error_code)
            await asyncio.sleep(self.backoff.next_delay())
        finally:
            if client is not None:
                try:
                    await asyncio.to_thread(client.close)
                except Exception:
                    pass

    async def _ensure_special_folders(self, discovered: list[tuple[str, str]]) -> None:
        """从 LIST/SPECIAL-USE 结果识别 Inbox/Junk，避免硬编码本地化名称。"""
        async with self.session_factory() as session:
            async with session.begin():
                existing = set(
                    (
                        await session.execute(
                            select(MailboxFolder.imap_name).where(
                                MailboxFolder.mailbox_id == self.mailbox_id
                            )
                        )
                    ).scalars()
                )
                for name, flags in discovered:
                    lowered = flags.lower()
                    monitor_enabled = "\\noselect" not in lowered
                    role = (
                        "inbox"
                        if "\\inbox" in lowered
                        else "spam"
                        if "\\junk" in lowered or "spam" in name.lower()
                        else "other"
                    )
                    if name and name not in existing:
                        session.add(
                            MailboxFolder(
                                mailbox_id=self.mailbox_id,
                                imap_name=name,
                                folder_role=role,
                                monitor_enabled=monitor_enabled,
                            )
                        )
                    elif name:
                        existing_folder = (
                            await session.execute(
                                select(MailboxFolder).where(
                                    MailboxFolder.mailbox_id == self.mailbox_id,
                                    MailboxFolder.imap_name == name,
                                )
                            )
                        ).scalar_one_or_none()
                        if existing_folder and "\\noselect" in lowered:
                            existing_folder.monitor_enabled = False

    async def _set_baseline(self, folder_id: UUID, uidvalidity: int, uidnext: int) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                folder = await session.get(MailboxFolder, folder_id, with_for_update=True)
                if folder:
                    folder.uidvalidity = uidvalidity
                    folder.uidnext = uidnext
                    folder.baseline_uid = max(0, uidnext - 1)
                    folder.last_seen_uid = max(0, uidnext - 1)
                    folder.sync_state = "idle"
                    folder.last_success_at = datetime.now(UTC)

    async def _mark_folder_error(self, folder_id: UUID, code: str) -> None:
        async with self.session_factory() as session:
            async with session.begin():
                folder = await session.get(MailboxFolder, folder_id, with_for_update=True)
                if folder:
                    folder.sync_state = "uidvalidity_changed"
                    folder.last_error_code = code
                    folder.last_error_at = datetime.now(UTC)

    async def run_forever(self) -> None:
        while not self.stop_event.is_set():
            await self.run_once()

    def stop(self) -> None:
        self.stop_event.set()
