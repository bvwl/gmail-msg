"""异步 Gmail REST API 客户端。"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import httpx

from app.auth.oauth import OAuthToken
from app.config import Settings, get_settings
from app.gmail.quota import QuotaLimiter

GMAIL_METHOD_QUOTA_UNITS: dict[str, int] = {
    "users.getProfile": 1,
    "users.messages.list": 5,
    "users.messages.get": 20,
    "users.history.list": 2,
    "users.watch": 100,
    "users.stop": 50,
    "users.messages.attachments.get": 5,
}


class OAuthServiceProtocol(Protocol):
    async def refresh_access_token(self, mailbox_id: UUID) -> OAuthToken: ...

    def invalidate_access_token(self, mailbox_id: UUID) -> None: ...


class GmailApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        reason: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason
        self.retryable = retryable


class GmailApiUnauthorizedError(GmailApiError):
    pass


class GmailApiPermissionError(GmailApiError):
    pass


class GmailApiNotFoundError(GmailApiError):
    pass


class GmailApiRateLimitError(GmailApiError):
    pass


class GmailApiServerError(GmailApiError):
    pass


@dataclass(frozen=True)
class GmailPage:
    items: tuple[dict[str, Any], ...]
    next_page_token: str | None
    result_size_estimate: int | None = None
    raw: Mapping[str, Any] | None = None


class GmailApiClient:
    def __init__(
        self,
        oauth_service: OAuthServiceProtocol,
        settings: Settings | None = None,
        *,
        http_client: Any | None = None,
        quota_limiter: QuotaLimiter | None = None,
        sleep: Any = asyncio.sleep,
        random_fn: Any = random.random,
    ) -> None:
        self.oauth_service = oauth_service
        self.settings = settings or get_settings()
        self.http_client = http_client
        self.quota_limiter = quota_limiter
        self.sleep = sleep
        self.random_fn = random_fn

    async def get_profile(self, mailbox_id: UUID, *, user_id: str = "me") -> dict[str, Any]:
        return await self._request(
            "users.getProfile", "GET", f"users/{user_id}/profile", mailbox_id
        )

    async def list_messages(
        self,
        mailbox_id: UUID,
        *,
        user_id: str = "me",
        page_token: str | None = None,
        max_results: int | None = None,
        q: str | None = None,
        label_ids: list[str] | None = None,
    ) -> GmailPage:
        params: dict[str, Any] = {}
        if page_token:
            params["pageToken"] = page_token
        if max_results is not None:
            params["maxResults"] = max_results
        if q:
            params["q"] = q
        if label_ids:
            params["labelIds"] = label_ids
        payload = await self._request(
            "users.messages.list", "GET", f"users/{user_id}/messages", mailbox_id, params=params
        )
        return self._page(payload, "messages")

    async def iter_messages(self, mailbox_id: UUID, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        page_token = kwargs.pop("page_token", None)
        while True:
            page = await self.list_messages(mailbox_id, page_token=page_token, **kwargs)
            for item in page.items:
                yield item
            if not page.next_page_token:
                return
            page_token = page.next_page_token

    async def list_all_messages(self, mailbox_id: UUID, **kwargs: Any) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        async for item in self.iter_messages(mailbox_id, **kwargs):
            result.append(item)
        return result

    async def get_message(
        self, mailbox_id: UUID, message_id: str, *, user_id: str = "me", format: str | None = None
    ) -> dict[str, Any]:
        params = {"format": format} if format else None
        return await self._request(
            "users.messages.get",
            "GET",
            f"users/{user_id}/messages/{message_id}",
            mailbox_id,
            params=params,
        )

    async def get_attachment(
        self,
        mailbox_id: UUID,
        message_id: str,
        attachment_id: str,
        *,
        user_id: str = "me",
    ) -> dict[str, Any]:
        """获取单个附件的 base64url 数据。

        调用方应先依据 MIME 元数据检查大小和文件名；该方法本身不落盘、不解压，
        并复用与其他 Gmail 请求相同的 quota limiter、超时和重试策略。
        """

        return await self._request(
            "users.messages.attachments.get",
            "GET",
            f"users/{user_id}/messages/{message_id}/attachments/{attachment_id}",
            mailbox_id,
        )

    async def list_history(
        self,
        mailbox_id: UUID,
        *,
        start_history_id: str,
        user_id: str = "me",
        page_token: str | None = None,
        max_results: int | None = None,
        history_types: list[str] | None = None,
    ) -> GmailPage:
        params: dict[str, Any] = {"startHistoryId": start_history_id}
        if page_token:
            params["pageToken"] = page_token
        if max_results is not None:
            params["maxResults"] = max_results
        if history_types:
            params["historyTypes"] = history_types
        payload = await self._request(
            "users.history.list", "GET", f"users/{user_id}/history", mailbox_id, params=params
        )
        return self._page(payload, "history")

    async def iter_history(self, mailbox_id: UUID, **kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        page_token = kwargs.pop("page_token", None)
        while True:
            page = await self.list_history(mailbox_id, page_token=page_token, **kwargs)
            for item in page.items:
                yield item
            if not page.next_page_token:
                return
            page_token = page.next_page_token

    async def watch(
        self,
        mailbox_id: UUID,
        *,
        topic_name: str,
        user_id: str = "me",
        label_ids: list[str] | None = None,
        label_filter_behavior: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"topicName": topic_name}
        if label_ids:
            body["labelIds"] = label_ids
        if label_filter_behavior:
            body["labelFilterBehavior"] = label_filter_behavior
        return await self._request(
            "users.watch", "POST", f"users/{user_id}/watch", mailbox_id, json=body
        )

    async def stop(self, mailbox_id: UUID, *, user_id: str = "me") -> dict[str, Any]:
        return await self._request("users.stop", "POST", f"users/{user_id}/stop", mailbox_id)

    async def _request(
        self,
        method_name: str,
        method: str,
        path: str,
        mailbox_id: UUID,
        **kwargs: Any,
    ) -> dict[str, Any]:
        quota_units = GMAIL_METHOD_QUOTA_UNITS[method_name]
        if method_name == "users.messages.get" and (kwargs.get("params") or {}).get("format") in {
            "full",
            "raw",
            "html",
        }:
            quota_units = 60
        attempts = self.settings.gmail_max_retries + 1
        unauthorized_retried = False
        for attempt in range(attempts):
            if self.quota_limiter is not None:
                await self.quota_limiter.acquire(mailbox_id, quota_units)
            try:
                token = await self.oauth_service.refresh_access_token(mailbox_id)
                response = await self._send(method, path, token, **kwargs)
            except httpx.HTTPError as exc:
                if attempt + 1 >= attempts:
                    raise GmailApiError("Gmail API network error", retryable=True) from exc
                await self._backoff(attempt)
                continue

            status_code = int(getattr(response, "status_code", 200))
            if status_code == 401 and not unauthorized_retried:
                unauthorized_retried = True
                invalidate = getattr(self.oauth_service, "invalidate_access_token", None)
                if invalidate is not None:
                    invalidate(mailbox_id)
                continue
            if 200 <= status_code < 300:
                if status_code == 204:
                    return {}
                try:
                    data = response.json()
                except (TypeError, ValueError) as exc:
                    raise GmailApiError(
                        "Gmail API returned invalid JSON", status_code=status_code
                    ) from exc
                if not isinstance(data, dict):
                    raise GmailApiError(
                        "Gmail API returned an invalid object", status_code=status_code
                    )
                return data

            error = self._error_from_response(response)
            if error.retryable and attempt + 1 < attempts:
                await self._backoff(attempt)
                continue
            raise error
        raise GmailApiError("Gmail API request exhausted retries", retryable=True)

    async def _send(self, method: str, path: str, token: OAuthToken, **kwargs: Any) -> Any:
        own_client = self.http_client is None
        client = self.http_client or httpx.AsyncClient(
            timeout=self.settings.gmail_http_timeout_seconds
        )
        try:
            headers = dict(kwargs.pop("headers", {}) or {})
            headers["Authorization"] = f"Bearer {token.access_token}"
            return await getattr(client, method.lower())(
                f"{self.settings.gmail_api_base_url.rstrip('/')}/{path.lstrip('/')}",
                headers=headers,
                **kwargs,
            )
        finally:
            if own_client:
                await client.aclose()

    async def _backoff(self, attempt: int) -> None:
        cap = min(
            self.settings.gmail_retry_max_seconds,
            self.settings.gmail_retry_base_seconds * (2**attempt),
        )
        jitter_seconds = min(1.0, max(0.0, float(self.random_fn())))
        await self.sleep(min(self.settings.gmail_retry_max_seconds, cap + jitter_seconds))

    @staticmethod
    def _page(payload: Mapping[str, Any], key: str) -> GmailPage:
        values = payload.get(key, [])
        items = (
            tuple(item for item in values if isinstance(item, dict))
            if isinstance(values, list)
            else ()
        )
        estimate = payload.get("resultSizeEstimate")
        return GmailPage(
            items=items,
            next_page_token=str(payload["nextPageToken"]) if payload.get("nextPageToken") else None,
            result_size_estimate=int(estimate) if isinstance(estimate, int) else None,
            raw=payload,
        )

    @classmethod
    def _error_from_response(cls, response: Any) -> GmailApiError:
        status_code = int(getattr(response, "status_code", 0))
        try:
            payload = response.json()
        except (TypeError, ValueError):
            payload = {}
        error_data = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error_data, dict):
            reason = str(error_data.get("status") or error_data.get("reason") or "")
            message = str(error_data.get("message") or "Gmail API request failed")
            errors = error_data.get("errors", [])
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                reason = str(errors[0].get("reason") or reason)
        else:
            reason, message = str(error_data), "Gmail API request failed"
        retryable_limit = status_code == 429 or (
            status_code == 403
            and reason.lower() in {"ratelimitexceeded", "userratelimitexceeded", "quotaexceeded"}
        )
        if status_code == 401:
            return GmailApiUnauthorizedError(message, status_code=status_code, reason=reason)
        if status_code == 403 and not retryable_limit:
            return GmailApiPermissionError(message, status_code=status_code, reason=reason)
        if status_code == 404:
            return GmailApiNotFoundError(message, status_code=status_code, reason=reason)
        if retryable_limit:
            return GmailApiRateLimitError(
                message, status_code=status_code, reason=reason, retryable=True
            )
        if status_code in {500, 502, 503, 504}:
            return GmailApiServerError(
                message, status_code=status_code, reason=reason, retryable=True
            )
        return GmailApiError(message, status_code=status_code, reason=reason)
