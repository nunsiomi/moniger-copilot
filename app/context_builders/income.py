"""Builds context for INCOME questions: totals and source breakdowns.

Expects each transaction dict to have: date (YYYY-MM-DD), amount (positive
float), type ("debit" or "credit"), category (str, e.g. "Salary",
"Freelance"), merchant (str, used as the income source label).
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    credits = [t for t in transactions if t.get("type") == "credit"]

    by_source: dict[str, float] = defaultdict(float)
    for t in credits:
        source = t.get("category") or t.get("merchant", "Unknown")
        by_source[source] += t["amount"]

    total_income = sum(by_source.values())
    breakdown = [
        {"source": source, "total": round(amount, 2)}
        for source, amount in sorted(by_source.items(), key=lambda kv: kv[1], reverse=True)
    ]

    dates = [t["date"] for t in credits if "date" in t]
    months_spanned = _distinct_months(dates)
    average_monthly_income = round(total_income / months_spanned, 2) if months_spanned else 0.0

    return {
        "total_income": round(total_income, 2),
        "transaction_count": len(credits),
        "by_source": breakdown,
        "average_monthly_income": average_monthly_income,
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }


def _distinct_months(dates: list[str]) -> int:
    return len({date[:7] for date in dates}) or 0
