"""Remove credentials from upstream error text before returning or storing it."""
from __future__ import annotations

import re
from collections.abc import Iterable


def redact_sensitive_text(value: object, *, secrets: Iterable[str] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[已隐藏]")
    patterns = [
        (r"(https?|socks5?)://[^/\s@]+@", r"\1://[已隐藏]@"),
        (r"\bBearer\s+[^\s\"'<>]+", "Bearer [已隐藏]"),
        (r"\b((?:access|refresh)(?:[_-]?token)?|api[_-]?key|authorization|password|secret|token|code_verifier)"
         r"([\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s&,\"'<>}\]]+)", r"\1\2[已隐藏]"),
        (r"([?&](?:code|state)=)[^&\s\"'<>]+", r"\1[已隐藏]"),
        (r"\b(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b", "[已隐藏]"),
        (r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[已隐藏]"),
    ]
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text
