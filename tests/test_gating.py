from datetime import datetime, timedelta, timezone

import jwt
import pytest
import redis

from app.gating import (
    GatingError,
    InvalidTokenError,
    QuotaExceededError,
    UserMismatchError,
    authenticate_and_gate,
    check_and_increment,
    extract_claims,
    remaining_quota,
)

_SECRET = "test-secret"


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


class UnavailableRedis:
    """Simulates Redis being unreachable -- every call raises ConnectionError."""

    def incr(self, key: str) -> int:
        raise redis.exceptions.ConnectionError("connection refused")

    def expire(self, key: str, seconds: int) -> None:
        raise redis.exceptions.ConnectionError("connection refused")


def _make_token(sub="usr_anon_t01wbl", tier="free", secret=_SECRET, exp_delta=None, **extra_claims):
    claims = {"sub": sub, "tier": tier, **extra_claims}
    if exp_delta is not None:
        claims["exp"] = datetime.now(timezone.utc) + exp_delta
    return jwt.encode(claims, secret, algorithm="HS256")


def _bearer(token: str) -> str:
    return f"Bearer {token}"


def test_valid_token_extracts_user_id_and_tier():
    """A validly-signed JWT with sub/tier claims should decode to an AuthenticatedUser."""
    token = _make_token(sub="usr_anon_t01wbl", tier="premium")
    user = extract_claims(_bearer(token), secret=_SECRET)
    assert user.user_id == "usr_anon_t01wbl"
    assert user.tier == "premium"


def test_missing_authorization_header_raises_invalid_token_error():
    """No Authorization header at all should raise InvalidTokenError (-> 401 ERR_JWT_INVALID)."""
    with pytest.raises(InvalidTokenError):
        extract_claims(None, secret=_SECRET)


def test_malformed_bearer_scheme_raises_invalid_token_error():
    """An Authorization header without a 'Bearer ' prefix should raise InvalidTokenError."""
    token = _make_token()
    with pytest.raises(InvalidTokenError):
        extract_claims(f"Basic {token}", secret=_SECRET)


def test_bad_signature_raises_invalid_token_error():
    """A token signed with the wrong secret should raise InvalidTokenError."""
    token = _make_token(secret="wrong-secret")
    with pytest.raises(InvalidTokenError):
        extract_claims(_bearer(token), secret=_SECRET)


def test_expired_token_raises_invalid_token_error():
    """A token past its exp claim should raise InvalidTokenError."""
    token = _make_token(exp_delta=timedelta(hours=-1))
    with pytest.raises(InvalidTokenError):
        extract_claims(_bearer(token), secret=_SECRET)


def test_unrecognized_tier_claim_raises_invalid_token_error():
    """A tier claim outside {free, premium} should raise InvalidTokenError."""
    token = _make_token(tier="enterprise")
    with pytest.raises(InvalidTokenError):
        extract_claims(_bearer(token), secret=_SECRET)


def test_first_question_of_the_month_increments_to_one():
    """The first check_and_increment call of the month should return 1."""
    client = FakeRedis()
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    count = check_and_increment(client, "usr_anon_t01wbl", "free", now=now)
    assert count == 1


def test_sets_ttl_only_on_first_increment():
    """expire() should only be called once per user per month, on the first increment."""
    client = FakeRedis()
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    check_and_increment(client, "usr_anon_t01wbl", "free", now=now)
    check_and_increment(client, "usr_anon_t01wbl", "free", now=now)
    assert len(client.expirations) == 1


def test_free_tier_raises_once_five_questions_exceeded():
    """check_and_increment should raise QuotaExceededError once a free-tier user passes 5."""
    client = FakeRedis()
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    for _ in range(5):
        check_and_increment(client, "usr_anon_t01wbl", "free", now=now)
    with pytest.raises(QuotaExceededError):
        check_and_increment(client, "usr_anon_t01wbl", "free", now=now)


def test_premium_tier_allows_fifty_questions():
    """A premium-tier user should not be gated until the 51st question in a month."""
    client = FakeRedis()
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    for _ in range(50):
        check_and_increment(client, "usr_anon_i07sal", "premium", now=now)
    with pytest.raises(QuotaExceededError):
        check_and_increment(client, "usr_anon_i07sal", "premium", now=now)


def test_different_users_have_independent_quotas():
    """One user's quota should not affect another user's remaining quota."""
    client = FakeRedis()
    now = datetime(2026, 6, 15, tzinfo=timezone.utc)
    for _ in range(5):
        check_and_increment(client, "usr_anon_t01wbl", "free", now=now)
    # t01wbl is now at quota; t02jbl should be unaffected.
    count = check_and_increment(client, "usr_anon_t02jbl", "free", now=now)
    assert count == 1


def test_quota_key_is_scoped_per_calendar_month():
    """A new calendar month should reset the counter even for the same user."""
    client = FakeRedis()
    june = datetime(2026, 6, 30, tzinfo=timezone.utc)
    july = datetime(2026, 7, 1, tzinfo=timezone.utc)
    for _ in range(5):
        check_and_increment(client, "usr_anon_t01wbl", "free", now=june)
    # A new month's first increment should succeed at count 1, not raise.
    count = check_and_increment(client, "usr_anon_t01wbl", "free", now=july)
    assert count == 1


def test_remaining_quota_is_floored_at_zero():
    """remaining_quota should never return a negative number."""
    assert remaining_quota("free", current_count=5) == 0
    assert remaining_quota("free", current_count=9999) == 0
    assert remaining_quota("premium", current_count=10) == 40


def test_quota_check_fails_open_when_redis_unavailable():
    """A Redis ConnectionError/TimeoutError during check_and_increment should not raise QuotaExceededError."""
    client = UnavailableRedis()
    result = check_and_increment(client, "usr_anon_t01wbl", "free", now=datetime.now(timezone.utc))
    assert result is None


def test_authenticate_and_gate_raises_user_mismatch_error():
    """A valid token whose sub doesn't match the request body's user_id should raise UserMismatchError (-> 403)."""
    token = _make_token(sub="usr_anon_t01wbl", tier="premium")
    client = FakeRedis()
    with pytest.raises(UserMismatchError):
        authenticate_and_gate(
            authorization_header=_bearer(token),
            request_user_id="usr_anon_t02jbl",
            redis_client=client,
            jwt_secret=_SECRET,
        )


def test_authenticate_and_gate_happy_path_returns_authenticated_user():
    """A valid, matching token under quota should return the AuthenticatedUser and increment the quota."""
    token = _make_token(sub="usr_anon_t01wbl", tier="premium")
    client = FakeRedis()
    user = authenticate_and_gate(
        authorization_header=_bearer(token),
        request_user_id="usr_anon_t01wbl",
        redis_client=client,
        jwt_secret=_SECRET,
        now=datetime(2026, 6, 15, tzinfo=timezone.utc),
    )
    assert user.user_id == "usr_anon_t01wbl"
    assert user.tier == "premium"
    assert sum(client.counts.values()) == 1


def test_authenticate_and_gate_raises_gating_error_subclass_on_bad_token():
    """An invalid token should surface as a GatingError (the base class app.main's
    exception handler catches) even before quota is ever checked."""
    client = FakeRedis()
    with pytest.raises(GatingError):
        authenticate_and_gate(
            authorization_header=None,
            request_user_id="usr_anon_t01wbl",
            redis_client=client,
            jwt_secret=_SECRET,
        )
    assert client.counts == {}
