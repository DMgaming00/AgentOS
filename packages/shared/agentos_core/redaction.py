"""Secret redaction applied before anything is logged, persisted or streamed."""

from __future__ import annotations

import re
from typing import Any

SENSITIVE_KEYS = re.compile(
    r"(api[_-]?key|secret|password|passwd|token|authorization|cookie|credential|private[_-]?key)",
    re.IGNORECASE,
)
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"postgres(?:ql)?(?:\+\w+)?://[^:\s]+:[^@\s]+@"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
REDACTED = "[REDACTED]"


def strip_nul(value: Any) -> Any:
    """Remove NUL characters (U+0000) from every string, including dict keys.

    PostgreSQL text and JSONB cannot store them, and a provider (or a corrupted fixture) can return them: found by
    the Chaos Lab, where one such response failed the step permanently with an unclassified storage error."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [strip_nul(v) for v in value]
    return value


def redact_text(value: str) -> str:
    value = value.replace("\x00", "")
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(REDACTED, value)
    return value


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return a copy of ``value`` with secret-looking keys and strings masked."""
    if _depth > 20:
        return REDACTED
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and SENSITIVE_KEYS.search(k) and not k.endswith("_tokens"):
                out[k] = REDACTED
            else:
                out[k] = redact(v, _depth=_depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [redact(v, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
