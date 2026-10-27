"""Builds context for INCOME questions: totals, source breakdown, salary
detection, and an annual income estimate for tax purposes.

There is exactly one credit-side category in the mirror's taxonomy (`INCM`
— see moniger_copilot_fixtures_v2), so income is simply every successful
`direction == "credit"` transaction; `fiSubcategory` distinguishes salary
from freelance/side-hustle/gift/other income within that.

`estimated_annual_income_naira` is the figure app.context_builders.tax
depends on, and is deliberately NOT always `average_monthly * 12`: when at
least 12 complete (non-current) months of transaction history exist, it
sums the trailing 12 months directly — a real total, not an extrapolation.
Only when fewer than 12 complete months exist does it fall back to
`average_monthly_income * 12`. This distinction is what separates a
defensible estimate from an arbitrary one — see persona T05 (a mid-year
raise from ~NGN120k/mo to ~NGN480k/mo): naively annualising the latest
month alone gives ~NGN5.76m, while the real trailing-12 sum is ~NGN2.52m.
"""
from __future__ import annotations

from typing import Any

from app.periods import shift_month

# A salary pattern needs at least this many monthly credits before its
# confidence is meaningful -- one data point can't establish "recurring".
_MIN_SALARY_OCCURRENCES = 2
_SALARY_SUBCATEGORY = "Salary & Wages"
_TRAILING_MONTHS = 12


def _to_naira(kobo: float) -> float:
    return round(kobo / 100, 2)


def _successful(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in transactions if t.get("status", "success") == "success" and "amountKobo" in t]


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return total income, source breakdown, average/trailing-12/estimated
    annual income, and salary-pattern detection — all amounts in naira."""
    successful = _successful(transactions)
    credits = [t for t in successful if t.get("direction") == "credit"]

    # "Complete" months are derived from the user's WHOLE transaction
    # history (both directions), not just months with income — a month
    # with spend but zero income (see persona I10's income gap) must still
    # count as a $0 month when averaging, exactly like
    # moniger_copilot_fixtures_v2's own ground_truth.py does.
    all_months = _distinct_months(successful)
    current_month = max(all_months) if all_months else None
    complete_months = sorted(m for m in all_months if m != current_month)

    income_by_month = _sum_by_month(credits)
    total_income_kobo = sum(income_by_month.values())

    average_monthly_income_kobo = (
        sum(income_by_month.get(m, 0) for m in complete_months) / len(complete_months)
        if complete_months else 0.0
    )

    trailing_12_months = complete_months[-_TRAILING_MONTHS:]
    has_full_year = len(trailing_12_months) == _TRAILING_MONTHS
    trailing_12_income_kobo = sum(income_by_month.get(m, 0) for m in trailing_12_months) if has_full_year else None
    estimated_annual_income_kobo = (
        trailing_12_income_kobo if has_full_year else average_monthly_income_kobo * 12
    )

    salary_credits = [t for t in credits if t.get("fiSubcategory") == _SALARY_SUBCATEGORY]
    salary_pattern = _detect_salary_pattern(salary_credits, total_distinct_months=len(all_months))

    dates = [t["date"] for t in credits if t.get("date")]

    return {
        "total_income_naira": _to_naira(total_income_kobo),
        "transaction_count": len(credits),
        "source_breakdown": _source_breakdown(credits),
        "distinct_months": len(all_months),
        "average_monthly_income_naira": _to_naira(average_monthly_income_kobo),
        "has_full_year_of_history": has_full_year,
        "trailing_12_month_income_naira": _to_naira(trailing_12_income_kobo) if has_full_year else None,
        "estimated_annual_income_naira": _to_naira(estimated_annual_income_kobo),
        "salary_pattern": salary_pattern,
        "date_range": {"start": min(dates)[:10], "end": max(dates)[:10]} if dates else None,
        # Every calendar month of history, so "which month didn't I get
        # paid?" or "what did I earn in March?" is answered from figures,
        # not guessed. The latest month is still in progress (payday may not
        # have landed yet), so it is excluded from the no-income list.
        "monthly_income": [
            {"month": month, "income_naira": _to_naira(income_by_month.get(month, 0))}
            for month in _calendar_months(all_months)
        ],
        "complete_months_without_income": [
            month for month in _calendar_months(all_months)
            if month != current_month and not income_by_month.get(month)
        ],
        "current_month_in_progress": current_month,
    }


def _calendar_months(months: set[str]) -> list[str]:
    """Every YYYY-MM from the earliest to the latest of `months`, gaps included."""
    if not months:
        return []
    first, last = min(months), max(months)
    span = (int(last[:4]) - int(first[:4])) * 12 + int(last[5:7]) - int(first[5:7])
    return [shift_month(first, offset) for offset in range(span + 1)]


def _distinct_months(transactions: list[dict[str, Any]]) -> set[str]:
    return {t["date"][:7] for t in transactions if t.get("date")}


def _sum_by_month(credits: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for txn in credits:
        month = (txn.get("date") or "")[:7]
        if not month:
            continue
        totals[month] = totals.get(month, 0) + int(txn["amountKobo"])
    return totals


def _source_breakdown(credits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, int] = {}
    for txn in credits:
        source = txn.get("fiSubcategory") or "Other Income (Unclassified Credit)"
        totals[source] = totals.get(source, 0) + int(txn["amountKobo"])
    return [
        {"source": source, "total_naira": _to_naira(total)}
        for source, total in sorted(totals.items(), key=lambda item: item[1], reverse=True)
    ]


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
            "estimated_amount_naira": None,
            "day_range": None,
            "occurrences": len(salary_credits),
        }

    amounts = [int(txn["amountKobo"]) for txn in salary_credits if "amountKobo" in txn]
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
        "estimated_amount_naira": _to_naira(mean_amount),
        "day_range": [min(days), max(days)],
        "occurrences": len(salary_credits),
    }
