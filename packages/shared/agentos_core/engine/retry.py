"""Retry classification and exponential backoff with full jitter."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from agentos_core.domain.errors import AgentOSError


def backoff_seconds(
    failure_number: int,
    *,
    base: float,
    cap: float,
    jitter: bool = True,
    rng: random.Random | None = None,
) -> float:
    """Delay before retry ``failure_number`` (1-based): min(cap, base * 2**(n-1)), full jitter."""
    raw = min(cap, base * (2 ** max(0, failure_number - 1)))
    if not jitter:
        return float(raw)
    return float((rng or random).uniform(0, raw))


def should_retry(
    error: AgentOSError,
    *,
    failure_count: int,
    max_attempts: int,
    now: datetime,
    retry_deadline_at: datetime | None,
    next_delay: float,
) -> bool:
    """``failure_count`` includes the failure being evaluated."""
    if not error.retryable:
        return False
    if failure_count >= max_attempts:
        return False
    return not (retry_deadline_at is not None and now + timedelta(seconds=next_delay) > retry_deadline_at)
