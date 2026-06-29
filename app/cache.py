"""Redis-backed response cache.

Caching is keyed on a fingerprint of the anonymised question plus whatever
financial context fed the prompt — never on raw PII — so a cache hit can
never leak one user's data to another and identical questions over
identical data skip the LLM call entirely.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol


class SupportsGetSet(Protocol):
    """Minimal interface cache depends on — satisfied by redis.Redis and fakes alike."""

    def get(self, key: str) -> str | bytes | None: ...
    def set(self, key: str, value: str, ex: int | None = None) -> None: ...


_KEY_PREFIX = "ask_cache"


def fingerprint(user_id: str, category: str, anonymised_question: str, context: dict[str, Any]) -> str:
    """Derive a stable cache key from the inputs that determine the answer.

    `context` is serialized with sorted keys so equivalent dicts built in a
    different key order still produce the same fingerprint.
    """
    payload = json.dumps(
        {
            "user_id": user_id,
            "category": category,
            "question": anonymised_question.strip().lower(),
            "context": context,
        },
        sort_keys=True,
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


def read(client: SupportsGetSet, key: str) -> dict[str, Any] | None:
    """Return the cached response dict for `key`, or None on a miss."""
    raw = client.get(key)
    if raw is None:
        return None

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")

    return json.loads(raw)


def write(client: SupportsGetSet, key: str, value: dict[str, Any], *, ttl_seconds: int) -> None:
    """Store `value` under `key` with an expiry of `ttl_seconds`."""
    client.set(key, json.dumps(value, default=str), ex=ttl_seconds)
