#!/usr/bin/env python3
"""Load moniger_copilot_fixtures_v2 into local MongoDB — a pure-Python
equivalent of the fixtures README's `mongoimport` instructions, so there's
no separate CLI tool to install. Uses bson.json_util (ships with pymongo)
to parse MongoDB Extended JSON exactly like mongoimport does — every
`{"$numberLong": ...}`/`{"$date": ...}`/`{"$oid": ...}` wrapper becomes a
real Int64/datetime/ObjectId in the stored document, not a plain string.

Run once after `docker compose up -d`:
    python scripts/load_fixtures.py

Re-running is safe: each collection is dropped and reloaded from scratch
(idempotent, matches a fresh mongoimport). copilot_transactions_POISONED.jsonl
is intentionally NOT loaded here — it's a standalone fixture for the PII
tests (see tests/test_anonymiser.py), never meant to sit in the main
collection a real user could query.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from bson import json_util
from urllib.parse import urlsplit, urlunsplit

from pymongo import MongoClient

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "moniger_copilot_fixtures_v2"

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017")
MONGODB_DB = os.environ.get("MONGODB_DB", "copilot_test")


def _load(collection_name: str, filename: str) -> int:
    path = FIXTURES_DIR / filename
    if not path.exists():
        print(f"  SKIP {filename}: not found at {path}", file=sys.stderr)
        return 0

    with open(path, encoding="utf-8") as f:
        docs = [json_util.loads(line) for line in f if line.strip()]

    client = MongoClient(MONGODB_URI)
    db = client[MONGODB_DB]
    db[collection_name].drop()
    if docs:
        db[collection_name].insert_many(docs)
    return len(docs)


def _redacted(uri: str) -> str:
    """The URI with any password removed, safe to print."""
    parts = urlsplit(uri)
    if parts.password is None:
        return uri
    host = parts.netloc.rsplit("@", 1)[-1]
    return urlunsplit(parts._replace(netloc=f"{parts.username}:***@{host}"))


def main() -> None:
    print(f"Loading fixtures from {FIXTURES_DIR} into {_redacted(MONGODB_URI)}/{MONGODB_DB} ...")
    txn_count = _load("copilot_transactions", "copilot_transactions.jsonl")
    print(f"  copilot_transactions: {txn_count} documents")
    budget_count = _load("copilot_budgets", "copilot_budgets.jsonl")
    print(f"  copilot_budgets: {budget_count} documents")

    sys.path.insert(0, str(REPO_ROOT))
    from app.data_access import ensure_indexes

    ensure_indexes()
    print("Indexes created.")
    print("Done.")


if __name__ == "__main__":
    main()
