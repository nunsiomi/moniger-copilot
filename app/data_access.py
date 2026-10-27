"""Read-only access to the already anonymised Mongo mirror.

Decode BSON/Extended JSON, then validate without cleaning or discarding records.
Contaminated input fails closed before any model call. Bare merchant labels must
appear in MERCHANT_LABELS_FILE, a separately trusted catalog (the development
catalog covers synthetic fixtures only). Provision indexes separately with
ensure_indexes; production API credentials should remain read-only.
"""
from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from typing import Any

from bson import ObjectId
from bson.int64 import Int64
from pymongo import MongoClient
from pymongo.collection import Collection

from app.mirror_validation import validate_transactions, validate_budgets

_TRANSACTIONS_COLLECTION = "copilot_transactions"
_BUDGETS_COLLECTION = "copilot_budgets"


@lru_cache(maxsize=1)
def _client() -> MongoClient:
    return MongoClient(os.environ.get("MONGODB_URI", "mongodb://localhost:27017"),
                       serverSelectionTimeoutMS=5000, connectTimeoutMS=5000)


def _db():
    return _client()[os.environ.get("MONGODB_DB", "copilot_test")]


def _decode_value(value: Any) -> Any:
    """Normalize one field's value into a plain Python type, regardless of
    whether it arrived as a native BSON type (from a live pymongo query) or
    a raw Extended-JSON wrapper dict (from reading a .jsonl fixture file
    directly): `Int64` / `{"$numberLong": ...}` -> `int`; `datetime` /
    `{"$date": ...}` -> an ISO string (stripping a trailing "Z" from the
    wrapper form so `datetime.fromisoformat` can parse either shape
    identically downstream); `ObjectId` / `{"$oid": ...}` -> the hex string
    itself. Any other dict/list is recursed into; any other scalar
    (str/bool/None/plain int/float) is returned unchanged.
    """
    if isinstance(value, Int64):
        return int(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        if set(value.keys()) == {"$numberLong"}:
            return int(value["$numberLong"])
        if set(value.keys()) == {"$date"}:
            raw = value["$date"]
            return raw[:-1] if isinstance(raw, str) and raw.endswith("Z") else raw
        if set(value.keys()) == {"$oid"}:
            return str(value["$oid"])
        return {key: _decode_value(v) for key, v in value.items()}
    if isinstance(value, list):
        return [_decode_value(v) for v in value]
    return value


def _decode_document(doc: dict[str, Any]) -> dict[str, Any]:
    """Decode one Mongo document, dropping the internal `_id`/`srcId`
    identifiers — nothing downstream needs Mongo's own primary keys."""
    return {
        key: _decode_value(value)
        for key, value in doc.items()
        if key not in ("_id", "srcId")
    }


def get_transactions(user_id: str) -> list[dict[str, Any]]:
    """Return `user_id`'s successful transactions, decoded and validated.

    Only `status == "success"` transactions are returned — pending/failed
    transactions never enter any answer (the same convention
    moniger_copilot_fixtures_v2's own ground_truth.py uses).
    """
    collection: Collection = _db()[_TRANSACTIONS_COLLECTION]
    cursor = collection.find({"userToken": user_id, "status": "success"})
    decoded = [_decode_document(doc) for doc in cursor]
    validate_transactions(decoded, user_id)
    return decoded


def get_budgets(user_id: str) -> list[dict[str, Any]]:
    """Return `user_id`'s budget documents, decoded from Mongo extended JSON.

    Budget documents carry no counterparty PII (no narration/merchant/
    account fields — see moniger_copilot_fixtures_v2's schema), so they are
    decoded but not passed through anonymise_transactions.
    """
    collection: Collection = _db()[_BUDGETS_COLLECTION]
    cursor = collection.find({"userToken": user_id})
    decoded = [_decode_document(doc) for doc in cursor]
    validate_budgets(decoded, user_id)
    return decoded


def get_as_of_date() -> Any:
    """Return the most recent calendar date present anywhere in the mirror
    (across ALL users, not just one) — the stand-in for "today" that
    app.context_builders.budget uses for period-elapsed math.

    Deliberately global, not per-user: an individual user's own most recent
    transaction can trail the mirror's actual snapshot point by a day or
    two just by chance (they simply may not have transacted today), which
    would understate how far into the budget period "now" actually is.
    The mirror's global most-recent date is a much better stand-in for "now"
    in both directions — it degrades gracefully on live production data
    (new transactions land across the whole user base essentially
    continuously, so the global max is always close to the real clock) and
    reproduces a frozen test snapshot exactly (moniger_copilot_fixtures_v2's
    AS_OF is a single global cutoff applied when generating every persona,
    not a per-persona one).

    Returns a `datetime.date` — the calendar day itself, not a specific
    timestamp within it. Callers that need an instant (e.g. for a
    period-elapsed fraction) should treat this date as running through its
    end (23:59:59), not its start — see budget.py's _percent_elapsed.
    """
    from datetime import date, datetime

    collection: Collection = _db()[_TRANSACTIONS_COLLECTION]
    latest = collection.find_one(
        {"status": "success"}, sort=[("date", -1)], projection={"date": 1}
    )
    if latest is None:
        return date.today()
    return datetime.fromisoformat(_decode_value(latest["date"])[:10]).date()


def ensure_indexes() -> None:
    """Create the indexes moniger_copilot_fixtures_v2's README specifies,
    plus one supporting get_as_of_date's global most-recent-date lookup.

    Idempotent — safe to call on every app startup (see app/main.py).
    """
    db = _db()
    db[_TRANSACTIONS_COLLECTION].create_index([("userToken", 1), ("date", -1)])
    db[_TRANSACTIONS_COLLECTION].create_index([("userToken", 1), ("fiCategoryCode", 1), ("date", -1)])
    db[_TRANSACTIONS_COLLECTION].create_index([("date", -1)])
    db[_BUDGETS_COLLECTION].create_index([("userToken", 1), ("budgetMonth", 1)])
