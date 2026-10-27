"""Redis-backed response cache.

Caching is keyed on `copilot:{user_id}:{question_type}:{hash}`, where hash
is a fingerprint of the inputs that determine the answer (category,
anonymised question, context) — never raw PII — so a cache hit can never
leak one user's data to another, and identical questions over identical
underlying data are served from cache without a model call.

TTL varies by question category, since some answers go stale faster than
others: spending/budget_track figures move every time a new transaction
posts, income/budget_recommend guidance is stable for longer, and tax
estimates barely change day to day. comparison/merchant share spending's
short TTL (also transaction-driven); general/out_of_scope_advice share
income's long TTL (answers don't depend on the user's current data). Those
four categories aren't specified by the caching spec — this is a
documented, easily-adjustable default, not a firm requirement.

If Redis is unreachable, both read and write fail OPEN: a read failure is
treated as a cache miss (falls through to a live model call), and a write
failure is silently swallowed — a broken cache must never break a response.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Protocol

import redis

from app.classifier import QuestionCategory

logger = logging.getLogger(__name__)

# Prompt changes invalidate entries by themselves (fingerprint covers the
# prompt as sent); bump this only if the cached value's shape changes.
_KEY_PREFIX = "copilot:v3"

_TTL_SECONDS: dict[QuestionCategory, int] = {
    QuestionCategory.SPENDING: 6 * 3600,
    QuestionCategory.INCOME: 24 * 3600,
    QuestionCategory.TAX: 7 * 86_400,
    QuestionCategory.BUDGET_TRACK: 6 * 3600,
    QuestionCategory.BUDGET_RECOMMEND: 24 * 3600,
    # Not specified by the caching spec — defaulted to the nearest sibling
    # category (see module docstring).
    QuestionCategory.COMPARISON: 6 * 3600,
    QuestionCategory.MERCHANT: 6 * 3600,
    QuestionCategory.GENERAL: 24 * 3600,
    QuestionCategory.OUT_OF_SCOPE_ADVICE: 24 * 3600,
}


class SupportsGetSet(Protocol):
    """Minimal interface cache depends on — satisfied by redis.Redis and fakes alike."""

    def get(self, key: str) -> str | bytes | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> None: ...


def ttl_for_category(category: QuestionCategory) -> int:
    """Return the cache TTL, in seconds, for `category` — see module docstring."""
    return _TTL_SECONDS[category]


def fingerprint(
    category: QuestionCategory, anonymised_question: str, context: dict[str, Any], *, prompt: str = ""
) -> str:
    """Hash the inputs that determine the answer, for the key's `{hash}` segment.

    `user_id` and `question_type` are deliberately NOT part of this hash —
    they're their own segments in the Redis key (see build_key), so
    identical questions from different users, or in different categories,
    never collide.
    """
    payload = json.dumps(
        {
            "category": category.value,
            "question": anonymised_question.strip().lower(),
            "context": context,
            # The full prompt as sent (model, system prompt, trimmed
            # messages), so prompt or trimming changes never serve answers
            # generated under the old instructions.
            "prompt": prompt,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_key(user_id: str, category: QuestionCategory, fingerprint_hash: str) -> str:
    """Build the full Redis key: copilot:{user_id}:{question_type}:{hash}."""
    return f"{_KEY_PREFIX}:{user_id}:{category.value}:{fingerprint_hash}"


def read(client: SupportsGetSet, key: str) -> dict[str, Any] | None:
    """Return the cached response dict for `key`, or None on a miss —
    including when Redis is unreachable, which fails open to a miss.
    """
    try:
        raw = client.get(key)
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        logger.warning("Redis unavailable, treating cache read as a miss for key %s", key)
        return None

    if raw is None:
        return None

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    return json.loads(raw)


def write(client: SupportsGetSet, key: str, value: dict[str, Any], *, category: QuestionCategory) -> None:
    """Store `value` under `key` with the TTL for `category`.

    Silently no-ops if Redis is unreachable — a cache-write failure must
    never break a response that's already been computed.
    """
    try:
        client.set(key, json.dumps(value, default=str), ex=ttl_for_category(category))
    except (redis.exceptions.ConnectionError, redis.exceptions.TimeoutError):
        logger.warning("Redis unavailable, skipping cache write for key %s", key)
