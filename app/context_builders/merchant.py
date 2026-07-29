"""Builds context for MERCHANT questions: per-merchant spend totals and
recurring-charge detection (a crude proxy for subscriptions).

A merchant should be flagged as "recurring" when it appears in 2+ distinct
months with a similar amount each time — exact subscription detection needs
more signal than transaction data alone, so this is a heuristic, not a fact.

Transactions are labeled by their `merchant` field; when that's missing,
the narration text is used as a fallback label so a transaction never gets
silently dropped from the breakdown just because `merchant` wasn't set.
"""
from __future__ import annotations

from typing import Any

_RECURRING_MIN_MONTHS = 2
_RECURRING_AMOUNT_TOLERANCE = 0.1  # up to 10% variation still counts as "similar"


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return per-merchant totals, the top merchant, and likely-recurring merchants."""
    debits = [t for t in transactions if t.get("type") == "debit" and "amount" in t]

    by_merchant: dict[str, list[dict[str, Any]]] = {}
    for txn in debits:
        by_merchant.setdefault(_merchant_label(txn), []).append(txn)

    merchant_breakdown = []
    recurring_merchants = []
    for label, txns in by_merchant.items():
        amounts = [t["amount"] for t in txns]
        months = {t["date"][:7] for t in txns if t.get("date")}
        total = sum(amounts)
        recurring = _looks_recurring(amounts, months)
        if recurring:
            recurring_merchants.append(label)
        merchant_breakdown.append({
            "merchant": label,
            "total": round(total, 2),
            "transaction_count": len(txns),
            "average_amount": round(total / len(txns), 2),
            "months_seen": len(months),
            "is_recurring": recurring,
        })

    merchant_breakdown.sort(key=lambda entry: entry["total"], reverse=True)
    dates = [t["date"] for t in debits if t.get("date")]

    return {
        "merchant_breakdown": merchant_breakdown,
        "top_merchant": merchant_breakdown[0]["merchant"] if merchant_breakdown else None,
        "recurring_merchants": recurring_merchants,
        "transaction_count": len(debits),
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }


def _merchant_label(txn: dict[str, Any]) -> str:
    """Return the transaction's merchant label, falling back to its narration
    when `merchant` isn't set."""
    merchant = txn.get("merchant")
    if merchant:
        return merchant
    narration = (txn.get("narration") or "").strip()
    return narration or "Unknown"


def _looks_recurring(amounts: list[float], months: set[str]) -> bool:
    """Return True if `amounts` look like a recurring charge across `months`."""
    if len(months) < _RECURRING_MIN_MONTHS or len(amounts) < _RECURRING_MIN_MONTHS:
        return False
    mean_amount = sum(amounts) / len(amounts)
    if mean_amount == 0:
        return False
    spread = (max(amounts) - min(amounts)) / mean_amount
    return spread <= _RECURRING_AMOUNT_TOLERANCE
