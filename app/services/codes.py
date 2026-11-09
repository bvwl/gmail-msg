"""验证码提取。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CodeCandidate:
    value: str
    code_type: str
    confidence: float


_PATTERNS = [
    ("numeric", re.compile(r"(?<!\d)(\d{4,8})(?!\d)")),
    ("alphanumeric", re.compile(r"(?<![A-Z0-9])([A-Z0-9]{6,10})(?![A-Z0-9])", re.I)),
]


def extract_codes(subject: str | None, text: str | None, html: str | None) -> list[CodeCandidate]:
    source = "\n".join(x for x in (subject, text, html) if x)
    candidates: list[CodeCandidate] = []
    seen: set[str] = set()
    for code_type, pattern in _PATTERNS:
        for match in pattern.finditer(source):
            value = match.group(1)
            if value.lower() in {"message", "verify", "code", "number"} or value in seen:
                continue
            seen.add(value)
            confidence = 0.95 if code_type == "numeric" else 0.75
            candidates.append(CodeCandidate(value, code_type, confidence))
    return candidates[:10]
