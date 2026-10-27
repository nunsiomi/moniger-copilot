"""Builds context for SPENDING questions: totals, category, and subcategory
breakdowns.

Reads the mirror schema (see app/data_access.py): each transaction carries
`direction` ("debit"/"credit"), `amountKobo` (int), and `fiCategoryCode`/
`fiSubcategory` — the dimensions a spending question is answered against.
`categoryId` is deliberately never used here; it exists only for budgets to
join on (see app/context_builders/budget.py and CLAUDE.md's "fail silently"
note — the two must never be conflated). Both `moneySource` values
("moniger" and "observed_external") are summed together without
distinction; filtering to one would silently halve every figure (see
test_money_source_not_filtered_away in the intern test suite).

All arithmetic happens in kobo (integers) and is converted to naira only at
the point a figure enters the returned context dict, via `_to_naira` —
never before, and never as an LLM-side computation.

`total_spend_naira`/`category_breakdown` are all-time aggregates across
whatever slice of `transactions` was handed in — they cannot answer a
month-scoped question ("last month", "in June") on their own. `as_of_month`
(the most recent month present) plus `monthly_breakdown` (one entry per
distinct month, each with its own total, category split, AND subcategory
split) exist specifically so the model can resolve a relative or named
month itself — see BASE_SYSTEM_PROMPT's month-resolution rule.
"""
from __future__ import annotations

from typing import Any

_UNCATEGORIZED = "UNCATEGORIZED"


def _to_naira(kobo: int) -> float:
    return round(kobo / 100, 2)


def _debits(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        t for t in transactions
        if t.get("direction") == "debit" and t.get("status", "success") == "success" and "amountKobo" in t
    ]


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return total spend, category/subcategory breakdowns, a per-month
    breakdown, top category, and date range — all amounts in naira."""
    debits = _debits(transactions)

    dates = [t["date"] for t in debits if t.get("date")]
    months = _distinct_month_labels(dates)
    category_breakdown, subcategory_breakdown, total_spend_kobo = _breakdowns(debits)

    return {
        "total_spend_naira": _to_naira(total_spend_kobo),
        "transaction_count": len(debits),
        "distinct_months": len(months),
        "category_breakdown": category_breakdown,
        "subcategory_breakdown": subcategory_breakdown,
        "top_category_code": category_breakdown[0]["category_code"] if category_breakdown else None,
        "date_range": {"start": min(dates)[:10], "end": max(dates)[:10]} if dates else None,
        "as_of_month": max(months) if months else None,
        "monthly_breakdown": _monthly_breakdown(debits, months),
    }


def _breakdowns(debits: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Return (category_breakdown, subcategory_breakdown, total_kobo) for `debits`.

    Each entry carries both `category_code` (fiCategoryCode, e.g. "FND") and
    `category_name` (the human-readable label, e.g. "Food & Drinks") — the
    model needs the readable name to phrase an answer naturally; the code
    alone would push it toward guessing or literally saying "FND".
    """
    totals_by_category: dict[str, int] = {}
    totals_by_subcategory: dict[tuple[str, str], int] = {}
    category_names: dict[str, str] = {}

    for txn in debits:
        code = txn.get("fiCategoryCode") or _UNCATEGORIZED
        amount = int(txn["amountKobo"])
        totals_by_category[code] = totals_by_category.get(code, 0) + amount
        category_names.setdefault(code, txn.get("categoryName") or txn.get("fiCategory") or code)

        subcategory = txn.get("fiSubcategory")
        if subcategory:
            key = (code, subcategory)
            totals_by_subcategory[key] = totals_by_subcategory.get(key, 0) + amount

    category_breakdown = [
        {"category_code": code, "category_name": category_names[code], "total_naira": _to_naira(total)}
        for code, total in sorted(totals_by_category.items(), key=lambda item: item[1], reverse=True)
    ]
    subcategory_breakdown = [
        {
            "category_code": code,
            "category_name": category_names[code],
            "subcategory": subcategory,
            "total_naira": _to_naira(total),
        }
        for (code, subcategory), total in sorted(totals_by_subcategory.items(), key=lambda item: item[1], reverse=True)
    ]
    return category_breakdown, subcategory_breakdown, sum(totals_by_category.values())


def _distinct_month_labels(dates: list[str]) -> set[str]:
    """Return the distinct YYYY-MM month labels present in `dates`."""
    return {date[:7] for date in dates if date}


def _monthly_breakdown(debits: list[dict[str, Any]], months: set[str]) -> list[dict[str, Any]]:
    """Return one entry per distinct month (oldest first), each with its own
    total spend, category split, and subcategory split — lets the model
    answer a question about a specific or relative month without
    re-deriving it from raw transactions.
    """
    entries = []
    for month in sorted(months):
        month_debits = [t for t in debits if (t.get("date") or "").startswith(month)]
        category_breakdown, subcategory_breakdown, total_kobo = _breakdowns(month_debits)
        entries.append({
            "month": month,
            "total_spend_naira": _to_naira(total_kobo),
            "category_breakdown": category_breakdown,
            "subcategory_breakdown": subcategory_breakdown,
        })
    return entries
