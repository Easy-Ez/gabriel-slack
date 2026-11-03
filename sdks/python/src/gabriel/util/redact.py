"""Redaction helpers.

Credentials must never appear in logs, errors or events: only a short prefix and the length
are kept, e.g. ``xoxc-12345...[REDACTED, len=109]``.
"""

from __future__ import annotations

import re


def redact_secret(value: str | None, visible_prefix: int = 10) -> str:
    if not value:
        return "<empty>"
    return f"{value[: min(visible_prefix, len(value) // 4)]}...[REDACTED, len={len(value)}]"


_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"xox[a-z]-[A-Za-z0-9%\-_.]+"), "xox?-[REDACTED]"),
    (re.compile(r"(\b(?:token|frt)=)[^&\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(\bd=)[^;\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(Bearer\s+)[^\s\"']+", re.IGNORECASE), r"\1[REDACTED]"),
]


def scrub_secrets(text: str) -> str:
    """Scrubs anything that looks like a Slack token, cookie, or frt ticket from free text."""
    out = text
    for pattern, replacement in _SECRET_PATTERNS:
        out = pattern.sub(replacement, out)
    return out
