"""Builds context for INCOME questions: totals and source breakdowns.

Expects each transaction dict to have: date (YYYY-MM-DD), amount (positive
float), type ("debit" or "credit"), category (str, e.g. "Salary",
"Freelance"), merchant (str, used as the income source label).
"""
from __future__ import annotations

from typing import Any

# A salary pattern needs at least this many monthly credits before its
# confidence is meaningful -- one data point can't establish "recurring".
_MIN_SALARY_OCCURRENCES = 2


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return total income, per-source breakdown, and average monthly income."""
    credits = [t for t in transactions if t.get("type") == "credit" and "amount" in t]

    totals_by_source: dict[str, float] = {}
    for txn in credits:
        source = txn.get("merchant") or txn.get("category") or "Other"
        totals_by_source[source] = totals_by_source.get(source, 0.0) + txn["amount"]

    source_breakdown = [
        {"source": source, "total": round(total, 2)}
        for source, total in sorted(totals_by_source.items(), key=lambda item: item[1], reverse=True)
    ]

    total_income = sum(totals_by_source.values())
    dates = [txn["date"] for txn in credits if txn.get("date")]
    months = _distinct_months(dates)
    average_monthly_income = total_income / months if months else 0.0

    salary_credits = [txn for txn in credits if txn.get("category") == "Salary"]
    salary_pattern = _detect_salary_pattern(salary_credits, total_distinct_months=months)

    return {
        "total_income": round(total_income, 2),
        "transaction_count": len(credits),
        "source_breakdown": source_breakdown,
        "distinct_months": months,
        "average_monthly_income": round(average_monthly_income, 2),
        "estimated_annual_income": round(average_monthly_income * 12, 2),
        "salary_pattern": salary_pattern,
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }


def _distinct_months(dates: list[str]) -> int:
    """Count distinct YYYY-MM months present in `dates`."""
    return len({date[:7] for date in dates if date})


def _detect_salary_pattern(salary_credits: list[dict[str, Any]], *, total_distinct_months: int) -> dict[str, Any]:
    """Score how strongly `salary_credits` look like a recurring monthly salary.

    Combines three signals, each in [0, 1], averaged into one confidence
    score computed in code (never left to the LLM to eyeball):
    - coverage: fraction of the user's active months that had a salary credit
    - amount consistency: how close the amounts are to each other
    - day consistency: how close together in the month they land
    """
    months = sorted({txn["date"][:7] for txn in salary_credits if txn.get("date")})
    if len(salary_credits) < _MIN_SALARY_OCCURRENCES or not months:
        return {
            "detected": False,
            "confidence": 0.0,
            "estimated_amount": None,
            "day_range": None,
            "occurrences": len(salary_credits),
        }

    amounts = [txn["amount"] for txn in salary_credits if "amount" in txn]
    days = [int(txn["date"][8:10]) for txn in salary_credits if txn.get("date")]

    mean_amount = sum(amounts) / len(amounts)
    amount_consistency = 1.0 - ((max(amounts) - min(amounts)) / mean_amount if mean_amount else 1.0)
    amount_consistency = max(0.0, min(1.0, amount_consistency))

    day_consistency = 1.0 - (max(days) - min(days)) / 30
    day_consistency = max(0.0, min(1.0, day_consistency))

    coverage = len(months) / total_distinct_months if total_distinct_months else 0.0
    coverage = max(0.0, min(1.0, coverage))

    confidence = round((amount_consistency + day_consistency + coverage) / 3, 2)

    return {
        "detected": confidence >= 0.5,
        "confidence": confidence,
        "estimated_amount": round(mean_amount, 2),
        "day_range": [min(days), max(days)],
        "occurrences": len(salary_credits),
    }
