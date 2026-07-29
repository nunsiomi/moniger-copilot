"""Builds context for TAX questions: annualized income estimate, rent paid,
and the current tax bands/relief figures the LLM must use (see
app/prompts/tax_system_prompt.py for the source of truth and the
compliance TODO on those figures).
"""
from __future__ import annotations

from typing import Any

from app.context_builders.income import build as build_income
from app.prompts.tax_system_prompt import (
    LOW_INCOME_EXEMPTION_THRESHOLD_NGN,
    PERSONAL_INCOME_TAX_BANDS_NGN,
    RENT_RELIEF_CAP_NGN,
    RENT_RELIEF_RATE,
)


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return annualized income/rent estimates, rent relief, and tax bands.

    PERSONAL_INCOME_TAX_BANDS_NGN and the rent-relief rate/cap are still
    TODO placeholders (see tax_system_prompt.py) pending sign-off against
    the gazetted Nigeria Tax Act 2025 text, so `income_tier` and
    `rent_relief_estimate` come back None until those are filled in -- the
    `*_configured` flags let the prompt/model react to that honestly
    instead of silently pretending a figure exists.
    """
    income_context = build_income(transactions)
    annual_income = income_context["estimated_annual_income"]

    rent_debits = [
        t for t in transactions
        if t.get("type") == "debit" and t.get("category") == "Rent" and "amount" in t
    ]
    rent_dates = [t["date"] for t in rent_debits if t.get("date")]
    rent_months = len({date[:7] for date in rent_dates})
    average_monthly_rent = sum(t["amount"] for t in rent_debits) / rent_months if rent_months else 0.0
    annual_rent_paid = round(average_monthly_rent * 12, 2)

    rent_relief_estimate = _estimate_rent_relief(annual_rent_paid)
    chargeable_income = round(annual_income - (rent_relief_estimate or 0), 2)

    return {
        "estimated_annual_income": annual_income,
        "annual_rent_paid": annual_rent_paid,
        "rent_relief_estimate": rent_relief_estimate,
        "rent_relief_configured": RENT_RELIEF_RATE is not None and RENT_RELIEF_CAP_NGN is not None,
        "chargeable_income": chargeable_income,
        "tax_bands": PERSONAL_INCOME_TAX_BANDS_NGN,
        "tax_bands_configured": bool(PERSONAL_INCOME_TAX_BANDS_NGN),
        "income_tier": _determine_tier(chargeable_income, PERSONAL_INCOME_TAX_BANDS_NGN),
        "low_income_exemption_threshold": LOW_INCOME_EXEMPTION_THRESHOLD_NGN,
        "is_below_exemption_threshold": (
            LOW_INCOME_EXEMPTION_THRESHOLD_NGN is not None
            and chargeable_income <= LOW_INCOME_EXEMPTION_THRESHOLD_NGN
        ),
    }


def _estimate_rent_relief(annual_rent_paid: float) -> float | None:
    """Return the rent relief amount, or None if the rate/cap aren't configured yet."""
    if RENT_RELIEF_RATE is None or RENT_RELIEF_CAP_NGN is None:
        return None
    return round(min(annual_rent_paid * RENT_RELIEF_RATE, RENT_RELIEF_CAP_NGN), 2)


def _determine_tier(
    chargeable_income: float, bands: list[dict[str, float | str]]
) -> dict[str, float | str] | None:
    """Return the band `chargeable_income` falls into, or None if bands aren't configured yet.

    Expects each band as {"min": float, "max": float | None, "rate": float,
    "label": str}, with `max: None` meaning an open-ended top band.
    """
    for band in bands:
        lower = band.get("min", 0)
        upper = band.get("max")
        if chargeable_income >= lower and (upper is None or chargeable_income < upper):
            return band
    return None
