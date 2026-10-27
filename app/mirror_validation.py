"""Non-mutating, fail-closed checks for the already anonymised mirror.

Bare merchant names require an independently maintained trusted label catalog.
The demo supplies a catalog from its clean synthetic fixtures. Production must
supply a backend-reviewed catalog; unknown names are never guessed to be safe.
"""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.anonymiser import _RETAIN_FIELDS


class MirrorValidationError(ValueError):
    """The mirror failed its privacy/schema contract. Never include raw values."""


_PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|(?<!\d)\d{10,19}(?!\d)")
_TOKEN = re.compile(r"usr_anon_[a-z0-9_]+", re.ASCII)
_PERSON = re.compile(r"(?:(?:TRF|TRANSFER|TO|FROM|FRM)\s+)*PERSON_[A-Z]+")
_TXN_FIELDS = set(_RETAIN_FIELDS) | {"_id", "srcId", "userToken", "merchantLabel"}
_BUDGET_FIELDS = {"_id", "srcId", "userToken", "budgetMonth", "periodStart", "periodEnd",
                  "status", "applyToFutureMonths", "totalMonthlyIncome", "totalMonthlyExpense",
                  "spentAmount", "remainingAmount", "expenseBreakdown", "incomeBreakdown"}
_CATEGORY_FIELDS = {"category_id", "category_name", "total_allocated", "subcategories"}
_SUB_FIELDS = {"_id", "name", "icon", "allocatedAmount", "spentAmount", "earnedAmount"}
_ID_FIELDS = {"_id", "srcId", "categoryId", "category_id"}


@lru_cache(maxsize=8)
def _catalog(path: str) -> frozenset[str]:
    if not path:
        return frozenset()
    labels = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(labels, list) or any(not isinstance(s, str) or _PII.search(s) for s in labels):
        raise MirrorValidationError("Invalid trusted merchant catalog.")
    return frozenset(labels)


def _object(doc: Any, allowed: set[str]) -> None:
    if not isinstance(doc, dict) or set(doc) - allowed:
        raise MirrorValidationError("Mirror contains unexpected fields or invalid structure.")
    for key, value in doc.items():
        if key in _ID_FIELDS:
            continue
        if isinstance(value, (dict, list)) and key not in {"expenseBreakdown", "incomeBreakdown", "subcategories"}:
            raise MirrorValidationError("Mirror contains an unexpected nested value.")
        if isinstance(value, str) and _PII.search(value):
            raise MirrorValidationError("Mirror contains a disallowed identity pattern.")
        if key in {"amountKobo", "totalMonthlyIncome", "totalMonthlyExpense", "spentAmount",
                   "remainingAmount", "total_allocated", "allocatedAmount", "earnedAmount"}:
            if isinstance(value, bool) or not isinstance(value, int):
                raise MirrorValidationError("Mirror money must be integer kobo.")
            if value < 0 and key != "remainingAmount":
                raise MirrorValidationError("Mirror money must be nonnegative.")


def _identity(doc: dict, expected_user: str) -> None:
    token = doc.get("userToken")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token) or token != expected_user:
        raise MirrorValidationError("Mirror user identity failed validation.")


def validate_transactions(documents: list[dict], expected_user: str) -> None:
    trusted = _catalog(os.environ.get("MERCHANT_LABELS_FILE", ""))
    for doc in documents:
        _object(doc, _TXN_FIELDS)
        _identity(doc, expected_user)
        label = doc.get("merchantLabel")
        if label is not None and (not isinstance(label, str) or
                                  (label not in trusted and not _PERSON.fullmatch(label))):
            raise MirrorValidationError("Mirror merchant label is not verified.")


def validate_budgets(documents: list[dict], expected_user: str) -> None:
    for doc in documents:
        _object(doc, _BUDGET_FIELDS)
        _identity(doc, expected_user)
        for field in ("expenseBreakdown", "incomeBreakdown"):
            rows = doc.get(field, [])
            if not isinstance(rows, list):
                raise MirrorValidationError("Invalid mirror budget breakdown.")
            for row in rows:
                _object(row, _CATEGORY_FIELDS)
                subs = row.get("subcategories", [])
                if not isinstance(subs, list):
                    raise MirrorValidationError("Invalid mirror subcategory breakdown.")
                for sub in subs:
                    _object(sub, _SUB_FIELDS)
