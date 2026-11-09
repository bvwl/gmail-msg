"""业务 Webhook 的确定性 JSON 和 HMAC-SHA256 签名。"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """发送端和接收端共用的 JSON 字节格式。"""

    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def signature_input(timestamp: int | str, body: bytes) -> bytes:
    return b"v1." + str(timestamp).encode("ascii") + b"." + body


def build_signature(secret: str | bytes, timestamp: int | str, body: bytes) -> str:
    key = secret.encode("utf-8") if isinstance(secret, str) else secret
    digest = hmac.new(key, signature_input(timestamp, body), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def sign_payload(
    secret: str | bytes, payload: Mapping[str, Any], *, timestamp: int | None = None
) -> tuple[bytes, int, str]:
    ts = int(time.time()) if timestamp is None else int(timestamp)
    body = canonical_json_bytes(payload)
    return body, ts, build_signature(secret, ts, body)


def verify_signature(
    secret: str | bytes,
    timestamp: int | str,
    body: bytes,
    signature: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else int(now)
    if tolerance_seconds < 0 or abs(current - ts) > tolerance_seconds:
        return False
    expected = build_signature(secret, ts, body)
    return hmac.compare_digest(expected.encode("ascii"), str(signature).encode("ascii", "ignore"))


__all__ = [
    "build_signature",
    "canonical_json_bytes",
    "sign_payload",
    "signature_input",
    "verify_signature",
    "generate_signature",
    "verify_hmac_signature",
]

generate_signature = build_signature
verify_hmac_signature = verify_signature
