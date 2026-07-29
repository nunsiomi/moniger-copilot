"""Builds context for COMPARISON questions: current month vs. the prior
month, for both spending and income.

"Current" and "previous" should be derived from the data itself (the two
most recent distinct months present in `transactions`), not from the
wall-clock date, so this works against historical/synthetic data too.
"""
from __future__ import annotations

from typing import Any


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return current vs. previous month totals and the absolute/percent change."""
    months = sorted({t["date"][:7] for t in transactions if t.get("date")})
    current_month = months[-1] if months else None
    previous_month = months[-2] if len(months) >= 2 else None

    current_totals = _totals_for_month(transactions, current_month)
    previous_totals = _totals_for_month(transactions, previous_month)

    return {
        "current_month": current_month,
        "previous_month": previous_month,
        "current_totals": current_totals,
        "previous_totals": previous_totals,
        "spend_change": _change(current_totals["spend"], previous_totals["spend"]),
        "income_change": _change(current_totals["income"], previous_totals["income"]),
        "category_deltas": _category_deltas(transactions, current_month, previous_month),
    }


def _totals_for_month(transactions: list[dict[str, Any]], month: str | None) -> dict[str, float]:
    """Return {"spend": ..., "income": ...} totals for the given YYYY-MM month."""
    if month is None:
        return {"spend": 0.0, "income": 0.0}

    spend = sum(
        t["amount"] for t in transactions
        if t.get("type") == "debit" and "amount" in t and (t.get("date") or "").startswith(month)
    )
    income = sum(
        t["amount"] for t in transactions
        if t.get("type") == "credit" and "amount" in t and (t.get("date") or "").startswith(month)
    )
    return {"spend": round(spend, 2), "income": round(income, 2)}


def _change(current_value: float, previous_value: float) -> dict[str, float | None]:
    """Return the absolute and percent change from `previous_value` to `current_value`."""
    absolute = round(current_value - previous_value, 2)
    percent = round((absolute / previous_value) * 100, 2) if previous_value else None
    return {"absolute": absolute, "percent": percent}


def _category_deltas(
    transactions: list[dict[str, Any]], current_month: str | None, previous_month: str | None
) -> list[dict[str, Any]]:
    """Return per-category spend deltas between `current_month` and `previous_month`,
    sorted by the largest absolute swing first."""
    current_by_category = _spend_by_category(transactions, current_month)
    previous_by_category = _spend_by_category(transactions, previous_month)

    categories = set(current_by_category) | set(previous_by_category)
    deltas = []
    for category in categories:
        current_value = current_by_category.get(category, 0.0)
        previous_value = previous_by_category.get(category, 0.0)
        deltas.append({
            "category": category,
            "current": round(current_value, 2),
            "previous": round(previous_value, 2),
            **_change(current_value, previous_value),
        })

    deltas.sort(key=lambda entry: abs(entry["absolute"]), reverse=True)
    return deltas


def _spend_by_category(transactions: list[dict[str, Any]], month: str | None) -> dict[str, float]:
    if month is None:
        return {}
    totals: dict[str, float] = {}
    for t in transactions:
        if t.get("type") != "debit" or "amount" not in t or not (t.get("date") or "").startswith(month):
            continue
        category = t.get("category") or "Uncategorized"
        totals[category] = totals.get(category, 0.0) + t["amount"]
    return totals
