"""Builds context for MERCHANT questions: per-merchant spend totals and
recurring-charge detection (a crude proxy for subscriptions).

A merchant is flagged as "recurring" when it appears in 2+ distinct months
with a similar amount each time — exact subscription detection needs more
signal than transaction data alone, so this is a heuristic, not a fact.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

_RECURRING_MIN_MONTHS = 2
_RECURRING_AMOUNT_TOLERANCE = 0.05  # 5% variance allowed between charges


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    debits = [t for t in transactions if t.get("type") == "debit" and t.get("merchant")]

    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    amounts_by_merchant: dict[str, list[float]] = defaultdict(list)
    months_by_merchant: dict[str, set[str]] = defaultdict(set)

    for t in debits:
        merchant = t["merchant"]
        totals[merchant] += t["amount"]
        counts[merchant] += 1
        amounts_by_merchant[merchant].append(t["amount"])
        if "date" in t:
            months_by_merchant[merchant].add(t["date"][:7])

    by_merchant = [
        {
            "merchant": merchant,
            "total": round(totals[merchant], 2),
            "transaction_count": counts[merchant],
            "average_transaction": round(totals[merchant] / counts[merchant], 2),
        }
        for merchant in totals
    ]
    by_merchant.sort(key=lambda row: row["total"], reverse=True)

    recurring = sorted(
        merchant
        for merchant in totals
        if _looks_recurring(amounts_by_merchant[merchant], months_by_merchant[merchant])
    )

    return {
        "by_merchant": by_merchant,
        "top_merchant": by_merchant[0]["merchant"] if by_merchant else None,
        "likely_recurring_merchants": recurring,
    }


def _looks_recurring(amounts: list[float], months: set[str]) -> bool:
    if len(months) < _RECURRING_MIN_MONTHS:
        return False

    average = sum(amounts) / len(amounts)
    if average == 0:
        return False

    return all(abs(amount - average) / average <= _RECURRING_AMOUNT_TOLERANCE for amount in amounts)
