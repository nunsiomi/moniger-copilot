from datetime import datetime, timezone

import pytest

from app.gating import QuotaExceeded, check_and_increment, remaining_quota


class FakeRedis:
    """Minimal in-memory stand-in for the two redis.Redis methods gating uses."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expirations: dict[str, int] = {}

    def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str, seconds: int) -> None:
        self.expirations[key] = seconds


def test_first_question_of_the_day_increments_to_one():
    client = FakeRedis()
    count = check_and_increment(client, "user-1", daily_limit=5)
    assert count == 1


def test_sets_ttl_only_on_first_increment():
    client = FakeRedis()
    check_and_increment(client, "user-1", daily_limit=5)
    check_and_increment(client, "user-1", daily_limit=5)
    assert len(client.expirations) == 1


def test_raises_once_limit_is_exceeded():
    client = FakeRedis()
    for _ in range(3):
        check_and_increment(client, "user-1", daily_limit=3)

    with pytest.raises(QuotaExceeded):
        check_and_increment(client, "user-1", daily_limit=3)


def test_quota_exceeded_carries_user_and_limit():
    client = FakeRedis()
    for _ in range(2):
        check_and_increment(client, "user-1", daily_limit=2)

    with pytest.raises(QuotaExceeded) as exc_info:
        check_and_increment(client, "user-1", daily_limit=2)

    assert exc_info.value.user_id == "user-1"
    assert exc_info.value.limit == 2


def test_different_users_have_independent_quotas():
    client = FakeRedis()
    for _ in range(3):
        check_and_increment(client, "user-1", daily_limit=3)

    # user-2 should still be able to ask, despite user-1 being at their limit
    count = check_and_increment(client, "user-2", daily_limit=3)
    assert count == 1


def test_quota_key_is_scoped_per_day():
    client = FakeRedis()
    day_one = datetime(2026, 1, 1, tzinfo=timezone.utc)
    day_two = datetime(2026, 1, 2, tzinfo=timezone.utc)

    for _ in range(3):
        check_and_increment(client, "user-1", daily_limit=3, now=day_one)

    # A new day should reset the counter even though it's the same user.
    count = check_and_increment(client, "user-1", daily_limit=3, now=day_two)
    assert count == 1


def test_remaining_quota_is_floored_at_zero():
    assert remaining_quota(FakeRedis(), "user-1", daily_limit=5, current_count=7) == 0
    assert remaining_quota(FakeRedis(), "user-1", daily_limit=5, current_count=2) == 3
