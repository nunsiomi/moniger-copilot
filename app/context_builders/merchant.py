"""Builds context for MERCHANT questions: per-merchant spend totals and a
recurring-charge signal.

Reads `merchantLabel` (already checked by app.mirror_validation before this
ever runs — see app/data_access.py) and `amountKobo`/`fiIsRecurring` from
the mirror schema. Both `moneySource` values are summed together, never
filtered to one (see app.context_builders.spending's docstring).

`top_merchants_naira` is an all-time ranking; `top_merchants_current_year_naira`
restricts to the most recent calendar year present in *this user's own*
transactions (derived from the data itself, not the wall clock — the same
principle app.context_builders.comparison uses, though app.context_builders.budget
uses the mirror's *global* most-recent date instead, via
app.data_access.get_as_of_date — see that builder's docstring for why the
distinction matters there) — this is what a "how much have I spent at X
this year" question needs, since an all-time total would over-count for a
merchant seen across multiple years.

`fi_is_recurring` is the mirror's own signal and is reported as-is rather
than second-guessed: it is genuinely `False` for every transaction in the
"realistic" categorisation regime (AutoTrack isn't populating in
production yet — see CLAUDE.md and moniger_copilot_fixtures_v2's README),
which is an honest data gap, not a bug to work around. `looks_recurring_by_pattern`
is a separate, clearly-labelled heuristic (amount similarity across 2+
distinct months) so the two signals are never conflated.
"""
from __future__ import annotations

from typing import Any

_RECURRING_MIN_MONTHS = 2
_RECURRING_AMOUNT_TOLERANCE = 0.1  # up to 10% variation still counts as "similar"


def _to_naira(kobo: int) -> float:
    return round(kobo / 100, 2)


def _debits(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        t for t in transactions
        if t.get("direction") == "debit" and t.get("status", "success") == "success" and "amountKobo" in t
    ]


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return per-merchant totals (all-time and current-year), the top
    merchant, and likely-recurring merchants — all amounts in naira."""
    debits = _debits(transactions)
    dates = [t["date"] for t in debits if t.get("date")]
    current_year = max(d[:4] for d in dates) if dates else None

    by_merchant: dict[str, list[dict[str, Any]]] = {}
    for txn in debits:
        by_merchant.setdefault(_merchant_label(txn), []).append(txn)

    merchant_breakdown = []
    # Confirmed = the mirror's own fiIsRecurring flag (what a "what
    # subscriptions do I have?" answer should list). The amount-pattern
    # heuristic also matches regular-but-not-subscription spend such as
    # ride-hailing, so those are reported separately as only "possibly".
    recurring_merchants = []
    possibly_recurring_merchants = []
    for label, txns in by_merchant.items():
        amounts = [int(t["amountKobo"]) for t in txns]
        months = {t["date"][:7] for t in txns if t.get("date")}
        total = sum(amounts)
        fi_is_recurring = any(t.get("fiIsRecurring") for t in txns)
        looks_recurring_by_pattern = _looks_recurring(amounts, months)
        if fi_is_recurring:
            recurring_merchants.append(label)
        elif looks_recurring_by_pattern:
            possibly_recurring_merchants.append(label)
        merchant_breakdown.append({
            "merchant": label,
            "total_naira": _to_naira(total),
            "total_current_year_naira": _to_naira(
                sum(int(t["amountKobo"]) for t in txns if (t.get("date") or "")[:4] == current_year)
            ) if current_year else 0.0,
            "transaction_count": len(txns),
            "average_amount_naira": _to_naira(round(total / len(txns))),
            "months_seen": len(months),
            "fi_is_recurring": fi_is_recurring,
            "looks_recurring_by_pattern": looks_recurring_by_pattern,
        })

    merchant_breakdown.sort(key=lambda entry: entry["total_naira"], reverse=True)
    top_current_year = sorted(
        (m for m in merchant_breakdown if m["total_current_year_naira"] > 0),
        key=lambda entry: entry["total_current_year_naira"],
        reverse=True,
    )

    return {
        "current_year": current_year,
        "merchant_breakdown": merchant_breakdown,
        "top_merchants_current_year": [
            {"merchant": m["merchant"], "total_naira": m["total_current_year_naira"]}
            for m in top_current_year[:10]
        ],
        "top_merchant": merchant_breakdown[0]["merchant"] if merchant_breakdown else None,
        "recurring_merchants": sorted(recurring_merchants),
        "possibly_recurring_merchants": sorted(possibly_recurring_merchants),
        "transaction_count": len(debits),
        "date_range": {"start": min(dates)[:10], "end": max(dates)[:10]} if dates else None,
    }


def _merchant_label(txn: dict[str, Any]) -> str:
    return txn.get("merchantLabel") or "Unknown"


def _looks_recurring(amounts: list[int], months: set[str]) -> bool:
    """Return True if `amounts` look like a recurring charge across `months`."""
    if len(months) < _RECURRING_MIN_MONTHS or len(amounts) < _RECURRING_MIN_MONTHS:
        return False
    mean_amount = sum(amounts) / len(amounts)
    if mean_amount == 0:
        return False
    spread = (max(amounts) - min(amounts)) / mean_amount
    return spread <= _RECURRING_AMOUNT_TOLERANCE
