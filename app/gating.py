"""Authentication + quota gating for the /ask endpoint.

Validates the bearer JWT on the Authorization header, confirms the
authenticated user matches the user_id in the request body, and enforces a
per-tier monthly question quota in Redis. All three failure modes raise a
GatingError subclass carrying the HTTP status and ERR_ code the API
contract defines (see CLAUDE.md, "Error schema" — the Contract doc itself
does not define one, so this is the schema agreed with the project owner):

    ERR_JWT_INVALID     401   token missing/malformed/expired/bad signature
    ERR_USER_MISMATCH   403   token's user_id != request body's user_id
    ERR_QUOTA_EXCEEDED  429   monthly quota used up for the token's tier

Quota is scoped per calendar month (quota:{user_id}:{YYYY-MM}) rather than
the Engineering doc's original per-day scheme, and the limit depends on the
tier claim in the token (free=5, premium=50). If Redis is unreachable,
gating fails OPEN — a user is never blocked from asking a question just
because the quota store happens to be down.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import jwt
import redis

logger = logging.getLogger(__name__)

_TIER_LIMITS: dict[str, int] = {"free": 5, "premium": 50}
_JWT_ALGORITHM = "HS256"
_QUOTA_KEY_TTL_SECONDS = 35 * 86_400  # comfortably outlives any calendar month


class GatingError(Exception):
    """Base class for /ask auth+quota failures.

    Carries the ERR_ code and HTTP status app.main's exception handler maps
    directly to a response — see the module docstring for the three
    concrete subclasses.
    """

    error_code: str
    status_code: int

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidTokenError(GatingError):
    error_code = "ERR_JWT_INVALID"
    status_code = 401


class UserMismatchError(GatingError):
    error_code = "ERR_USER_MISMATCH"
    status_code = 403


class QuotaExceededError(GatingError):
    error_code = "ERR_QUOTA_EXCEEDED"
    status_code = 429


class SupportsIncrExpire(Protocol):
    """Minimal interface the quota check depends on — satisfied by redis.Redis and fakes alike."""

    def incr(self, key: str) -> int: ...
    def expire(self, key: str, seconds: int) -> None: ...


@dataclass
class AuthenticatedUser:
    user_id: str
    tier: str


def extract_claims(authorization_header: str | None, *, secret: str) -> AuthenticatedUser:
    """Verify the bearer JWT and return its user_id (`sub`) and `tier` claims.

    Raises InvalidTokenError for anything that makes the token unusable:
    missing/malformed header, bad signature, expired token, or a missing
    `sub` claim / unrecognized `tier` claim.
    """
    if not authorization_header:
        raise InvalidTokenError("Missing Authorization header.")

    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise InvalidTokenError("Authorization header must be in the form 'Bearer <token>'.")

    try:
        claims = jwt.decode(token, secret, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise InvalidTokenError(f"Token failed verification: {exc}") from exc

    user_id = claims.get("sub")
    tier = claims.get("tier")

    if not user_id or not isinstance(user_id, str):
        raise InvalidTokenError("Token is missing a valid 'sub' claim.")
    if tier not in _TIER_LIMITS:
        raise InvalidTokenError(f"Token has an unrecognized 'tier' claim: {tier!r}")

    return AuthenticatedUser(user_id=user_id, tier=tier)


def _quota_key(user_id: str, month: str) -> str:
    return f"quota:{user_id}:{month}"


def check_and_increment(
    client: SupportsIncrExpire,
    user_id: str,
    tier: str,
    *,
    now: datetime | None = None,
) -> int | None:
    """Increment this month's question count for `user_id` and enforce the
    tier's limit.

    Returns the new count, or None if Redis was unreachable and the check
    was skipped (fail-open — see module docstring). Raises
    QuotaExceededError if the increment pushes the user over their tier's
    limit; the increment itself still happened (Redis INCR has no
    rollback), so a raised QuotaExceededError means "this request is
    rejected", not "nothing happened".
    """
    limit = _TIER_LIMITS[tier]
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    key = _quota_key(user_id, month)

    try:
        count = client.incr(key)
        if count == 1:
            # First increment for this key this month — set the TTL so the
            # counter self-cleans instead of growing the keyspace forever.
            client.expire(key, _QUOTA_KEY_TTL_SECONDS)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        logger.warning("Redis unavailable, failing open on quota check for user %s", user_id)
        return None

    if count > limit:
        raise QuotaExceededError(f"Monthly quota of {limit} questions exceeded for tier '{tier}'.")

    return count


def remaining_quota(tier: str, *, current_count: int) -> int:
    """Return how many questions are left this month for `tier`, floored at 0."""
    return max(0, _TIER_LIMITS[tier] - current_count)


def authenticate_and_gate(
    *,
    authorization_header: str | None,
    request_user_id: str,
    redis_client: SupportsIncrExpire,
    jwt_secret: str,
    now: datetime | None = None,
) -> AuthenticatedUser:
    """Full auth+quota gate for one /ask request.

    Validates the token, confirms it matches the request body's user_id,
    then enforces the monthly quota for the token's tier. Raises
    InvalidTokenError, UserMismatchError, or QuotaExceededError on failure
    — see the module docstring for the HTTP status each maps to.
    """
    user = extract_claims(authorization_header, secret=jwt_secret)

    if user.user_id != request_user_id:
        raise UserMismatchError(
            f"Authenticated user '{user.user_id}' does not match request user_id '{request_user_id}'."
        )

    check_and_increment(redis_client, user.user_id, user.tier, now=now)
    return user
