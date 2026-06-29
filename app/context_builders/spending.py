"""Builds context for SPENDING questions: totals and category breakdowns.

Expects each transaction dict to have: date (YYYY-MM-DD), amount (positive
float), type ("debit" or "credit"), category (str), merchant (str).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    debits = [t for t in transactions if t.get("type") == "debit"]

    by_category: dict[str, float] = defaultdict(float)
    for t in debits:
        by_category[t.get("category", "Uncategorized")] += t["amount"]

    total_spend = sum(by_category.values())
    breakdown = [
        {
            "category": category,
            "total": round(amount, 2),
            "share_of_spend": round(amount / total_spend, 4) if total_spend else 0.0,
        }
        for category, amount in sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    ]

    dates = [t["date"] for t in debits if "date" in t]

    return {
        "total_spend": round(total_spend, 2),
        "transaction_count": len(debits),
        "by_category": breakdown,
        "top_category": breakdown[0]["category"] if breakdown else None,
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }
