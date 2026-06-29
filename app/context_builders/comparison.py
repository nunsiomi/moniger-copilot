"""Builds context for COMPARISON questions: current month vs. the prior
month, for both spending and income.

"Current" and "previous" are derived from the data itself (the two most
recent distinct months present in `transactions`), not from the wall-clock
date, so this works against historical/synthetic data too.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    months = sorted({t["date"][:7] for t in transactions if "date" in t}, reverse=True)
    current_month = months[0] if months else None
    previous_month = months[1] if len(months) > 1 else None

    current = _totals_for_month(transactions, current_month)
    previous = _totals_for_month(transactions, previous_month)

    return {
        "current_month": current_month,
        "previous_month": previous_month,
        "current": current,
        "previous": previous,
        "spend_change": _change(current["spend"], previous["spend"]),
        "income_change": _change(current["income"], previous["income"]),
    }


def _totals_for_month(transactions: list[dict[str, Any]], month: str | None) -> dict[str, float]:
    if month is None:
        return {"spend": 0.0, "income": 0.0}

    by_type: dict[str, float] = defaultdict(float)
    for t in transactions:
        if t.get("date", "")[:7] != month:
            continue
        key = "spend" if t.get("type") == "debit" else "income"
        by_type[key] += t["amount"]

    return {"spend": round(by_type["spend"], 2), "income": round(by_type["income"], 2)}


def _change(current_value: float, previous_value: float) -> dict[str, float | None]:
    absolute = round(current_value - previous_value, 2)
    percent = round(absolute / previous_value * 100, 2) if previous_value else None
    return {"absolute": absolute, "percent": percent}
