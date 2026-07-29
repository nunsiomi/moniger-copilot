"""Builds context for SPENDING questions: totals and category breakdowns.

Expects each transaction dict to have: date (YYYY-MM-DD), amount (positive
float), type ("debit" or "credit"), category (str), merchant (str).
"""
from __future__ import annotations

from typing import Any


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return total spend, per-category breakdown, top category, and date range."""
    debits = [t for t in transactions if t.get("type") == "debit" and "amount" in t]

    totals_by_category: dict[str, float] = {}
    for txn in debits:
        category = txn.get("category") or "Uncategorized"
        totals_by_category[category] = totals_by_category.get(category, 0.0) + txn["amount"]

    category_breakdown = [
        {"category": category, "total": round(total, 2)}
        for category, total in sorted(totals_by_category.items(), key=lambda item: item[1], reverse=True)
    ]
    total_spend = sum(totals_by_category.values())
    dates = [txn["date"] for txn in debits if txn.get("date")]

    return {
        "total_spend": round(total_spend, 2),
        "transaction_count": len(debits),
        "distinct_months": _distinct_months(dates),
        "category_breakdown": category_breakdown,
        "top_category": category_breakdown[0]["category"] if category_breakdown else None,
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }


def _distinct_months(dates: list[str]) -> int:
    """Count distinct YYYY-MM months present in `dates`."""
    return len({date[:7] for date in dates if date})
