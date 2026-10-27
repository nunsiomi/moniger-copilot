"""Builds context for COMPARISON questions: current month vs. the prior
month, for both spending and income, plus every distinct month's totals so
a specifically-named month pair (e.g. "December 2025 vs December 2024")
can be resolved directly.

"Current" and "previous" are derived from the data itself (the two most
recent distinct months present in `transactions`), not from the wall-clock
date, so this works against historical/synthetic data too. That
convenience pair only covers a generic "this month vs last month" phrasing
though — `monthly_totals` (every distinct month's spend/income, oldest
first) is included so the model can still resolve a comparison between two
specific named months, including a year-over-year pair, directly instead
of falling back to current/previous and answering the wrong months.

Reads `direction`/`amountKobo`/`fiCategoryCode` from the mirror schema (see
app.context_builders.spending's docstring for why `fiCategoryCode`, never
`categoryId`, and why both `moneySource` values are always summed
together). All arithmetic happens in kobo; conversion to naira happens only
at the point a figure enters the returned context dict.
"""
from __future__ import annotations

from typing import Any

from app.periods import resolve_months, shift_month


def _to_naira(kobo: float) -> float:
    return round(kobo / 100, 2)


def _successful(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [t for t in transactions if t.get("status", "success") == "success" and "amountKobo" in t]


def build(transactions: list[dict[str, Any]], question: str | None = None) -> dict[str, Any]:
    """Return current vs. previous month totals, the absolute/percent change,
    per-category deltas, and every distinct month's totals for resolving a
    specific named-month (including year-over-year) comparison.

    When `question` names months (app.periods.resolve_months), those are the
    pair compared — two named months compare the later against the earlier;
    one named month compares it against the calendar month before it.
    Otherwise the two most recent months present in the data are used."""
    successful = _successful(transactions)
    months = sorted({t["date"][:7] for t in successful if t.get("date")})
    requested = resolve_months(question, months[-1] if months else None)
    if len(requested) >= 2:
        previous_month, current_month = sorted(requested[:2])
    elif len(requested) == 1:
        current_month, previous_month = requested[0], shift_month(requested[0], -1)
    else:
        current_month = months[-1] if months else None
        previous_month = months[-2] if len(months) >= 2 else None

    # The latest month in the data is usually still in progress; say how far
    # it runs so a partial month isn't presented as a full one.
    latest_date = max((t["date"][:10] for t in successful if t.get("date")), default=None)
    current_through = latest_date if latest_date and current_month == latest_date[:7] else None

    current_totals = _totals_for_month(successful, current_month)
    previous_totals = _totals_for_month(successful, previous_month)

    return {
        "current_month": current_month,
        "previous_month": previous_month,
        "months_from_question": bool(requested),
        "current_month_data_through": current_through,
        "current_totals_naira": current_totals,
        "previous_totals_naira": previous_totals,
        "spend_change": _change(current_totals["spend_naira"], previous_totals["spend_naira"]),
        "income_change": _change(current_totals["income_naira"], previous_totals["income_naira"]),
        "category_deltas": _category_deltas(successful, current_month, previous_month),
        "monthly_totals": [
            {"month": month, **_totals_for_month(successful, month)} for month in months
        ],
    }


def _totals_for_month(transactions: list[dict[str, Any]], month: str | None) -> dict[str, float]:
    """Return {"spend_naira": ..., "income_naira": ...} for the given YYYY-MM month."""
    if month is None:
        return {"spend_naira": 0.0, "income_naira": 0.0}

    spend = sum(
        int(t["amountKobo"]) for t in transactions
        if t.get("direction") == "debit" and (t.get("date") or "").startswith(month)
    )
    income = sum(
        int(t["amountKobo"]) for t in transactions
        if t.get("direction") == "credit" and (t.get("date") or "").startswith(month)
    )
    return {"spend_naira": _to_naira(spend), "income_naira": _to_naira(income)}


def _change(current_value: float, previous_value: float) -> dict[str, float | None]:
    """Return the absolute and percent change from `previous_value` to `current_value`."""
    absolute = round(current_value - previous_value, 2)
    percent = round((absolute / previous_value) * 100, 2) if previous_value else None
    return {"absolute": absolute, "percent": percent}


def _category_deltas(
    transactions: list[dict[str, Any]], current_month: str | None, previous_month: str | None
) -> list[dict[str, Any]]:
    """Return per-category spend deltas between `current_month` and `previous_month`,
    sorted by the largest absolute swing first. Carries `category_name`
    alongside `category_code` so the model can phrase the answer with a
    readable label rather than the bare fiCategoryCode."""
    names: dict[str, str] = {}
    current_by_category = _spend_by_category(transactions, current_month, names)
    previous_by_category = _spend_by_category(transactions, previous_month, names)

    categories = set(current_by_category) | set(previous_by_category)
    deltas = []
    for category_code in categories:
        current_value = current_by_category.get(category_code, 0.0)
        previous_value = previous_by_category.get(category_code, 0.0)
        deltas.append({
            "category_code": category_code,
            "category_name": names.get(category_code, category_code),
            "current_naira": current_value,
            "previous_naira": previous_value,
            **_change(current_value, previous_value),
        })

    deltas.sort(key=lambda entry: abs(entry["absolute"]), reverse=True)
    return deltas


def _spend_by_category(
    transactions: list[dict[str, Any]], month: str | None, names: dict[str, str]
) -> dict[str, float]:
    if month is None:
        return {}
    totals: dict[str, int] = {}
    for t in transactions:
        if t.get("direction") != "debit" or not (t.get("date") or "").startswith(month):
            continue
        category_code = t.get("fiCategoryCode") or "UNCATEGORIZED"
        totals[category_code] = totals.get(category_code, 0) + int(t["amountKobo"])
        names.setdefault(category_code, t.get("categoryName") or t.get("fiCategory") or category_code)
    return {code: _to_naira(total) for code, total in totals.items()}
