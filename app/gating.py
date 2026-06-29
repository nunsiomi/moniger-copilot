"""Quota enforcement — caps how many questions a user can ask per day.

Counts are kept in Redis (or any object implementing the same two methods),
keyed per user per UTC day, so the counter resets naturally at midnight
without a separate cleanup job.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class QuotaExceeded(Exception):
    """Raised when a user has used up their daily question allowance."""

    def __init__(self, user_id: str, limit: int) -> None:
        self.user_id = user_id
        self.limit = limit
        super().__init__(f"User {user_id} exceeded daily quota of {limit} questions")


class SupportsIncrExpire(Protocol):
    """Minimal interface gating depends on — satisfied by redis.Redis and fakes alike."""

    def incr(self, key: str) -> int: ...
    def expire(self, key: str, seconds: int) -> None: ...


_SECONDS_IN_DAY = 86_400


def _quota_key(user_id: str, day: str) -> str:
    return f"quota:{user_id}:{day}"


def check_and_increment(
    client: SupportsIncrExpire,
    user_id: str,
    *,
    daily_limit: int,
    now: datetime | None = None,
) -> int:
    """Increment today's question count for `user_id` and enforce `daily_limit`.

    Returns the new count on success. Raises QuotaExceeded if the increment
    would push the user over their limit — the increment still happened
    (Redis INCR has no rollback), so callers should treat a raised
    QuotaExceeded as "this request is rejected", not "nothing happened".
    """
    today = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    key = _quota_key(user_id, today)

    count = client.incr(key)
    if count == 1:
        # First increment for this key today — set the TTL so the counter
        # self-cleans instead of growing the keyspace forever.
        client.expire(key, _SECONDS_IN_DAY)

    if count > daily_limit:
        raise QuotaExceeded(user_id, daily_limit)

    return count


def remaining_quota(
    client: SupportsIncrExpire,
    user_id: str,
    *,
    daily_limit: int,
    current_count: int,
) -> int:
    """Return how many questions `user_id` has left today, floored at 0."""
    return max(0, daily_limit - current_count)
