"""Gmail API 客户端、共享配额限流和 Watch 管理。"""

from app.gmail.client import (
    GMAIL_METHOD_QUOTA_UNITS,
    GmailApiClient,
    GmailApiError,
    GmailApiNotFoundError,
    GmailApiPermissionError,
    GmailApiRateLimitError,
    GmailApiServerError,
    GmailApiUnauthorizedError,
    GmailPage,
)
from app.gmail.message_service import MessageContentService
from app.gmail.mime import GmailMimeParser, ParsedEmail, parse_gmail_full
from app.gmail.quota import (
    GmailQuotaConfig,
    InMemoryQuotaLimiter,
    QuotaExceededError,
    RedisQuotaLimiter,
)
from app.gmail.watch import GmailWatchManager, WatchRegistration, WatchStatus

__all__ = [
    "GmailApiClient",
    "GMAIL_METHOD_QUOTA_UNITS",
    "GmailApiError",
    "GmailApiNotFoundError",
    "GmailApiPermissionError",
    "GmailApiRateLimitError",
    "GmailApiServerError",
    "GmailApiUnauthorizedError",
    "GmailPage",
    "GmailQuotaConfig",
    "InMemoryQuotaLimiter",
    "RedisQuotaLimiter",
    "QuotaExceededError",
    "GmailWatchManager",
    "WatchRegistration",
    "WatchStatus",
    "GmailMimeParser",
    "ParsedEmail",
    "parse_gmail_full",
    "MessageContentService",
]
