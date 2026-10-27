"""Builds context for TAX questions: annualized income estimate, rent paid,
and the current tax bands/relief figures the LLM must use (see
app/prompts/tax_system_prompt.py for the source of truth — the 2025 Nigeria
Tax Act figures there are already filled in, not TODO placeholders; only
this builder's *inputs* changed with the mirror-schema migration).

Consumes app.context_builders.income.build's `estimated_annual_income_naira`
directly (trailing-12-month real sum when a full year of history exists,
average * 12 otherwise — see income.py's docstring for why this matters:
naively annualizing a single recent month misreads a mid-year raise). Rent
is identified by `fiCategoryCode == "HOU"` (Housing) — never the old
free-text `category == "Rent"` — since the mirror has no separate "Rent"
category of its own.
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

_HOUSING_CATEGORY_CODE = "HOU"


def _to_naira(kobo: float) -> float:
    return round(kobo / 100, 2)


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Return annualized income/rent estimates, rent relief, and tax bands."""
    income_context = build_income(transactions)
    annual_income = income_context["estimated_annual_income_naira"]

    rent_debits = [
        t for t in transactions
        if t.get("status", "success") == "success"
        and t.get("direction") == "debit"
        and t.get("fiCategoryCode") == _HOUSING_CATEGORY_CODE
        and "amountKobo" in t
    ]
    rent_dates = [t["date"] for t in rent_debits if t.get("date")]
    rent_months = len({date[:7] for date in rent_dates})
    average_monthly_rent = (
        _to_naira(sum(int(t["amountKobo"]) for t in rent_debits) / rent_months) if rent_months else 0.0
    )
    annual_rent_paid = round(average_monthly_rent * 12, 2)

    rent_relief_estimate = _estimate_rent_relief(annual_rent_paid)
    chargeable_income = max(0.0, round(annual_income - (rent_relief_estimate or 0), 2))

    return {
        "estimated_annual_income_naira": annual_income,
        "income_estimate_method": (
            "trailing_12_months" if income_context["has_full_year_of_history"] else "average_monthly_x12"
        ),
        "income_date_range": income_context.get("date_range"),
        "has_full_year_of_history": income_context["has_full_year_of_history"],
        "annual_rent_paid_naira": annual_rent_paid,
        "rent_relief_estimate_naira": rent_relief_estimate,
        "rent_relief_configured": RENT_RELIEF_RATE is not None and RENT_RELIEF_CAP_NGN is not None,
        "chargeable_income_naira": chargeable_income,
        "tax_bands": PERSONAL_INCOME_TAX_BANDS_NGN,
        "tax_bands_configured": bool(PERSONAL_INCOME_TAX_BANDS_NGN),
        "income_tier": _determine_tier(chargeable_income, PERSONAL_INCOME_TAX_BANDS_NGN),
        "low_income_exemption_threshold_naira": LOW_INCOME_EXEMPTION_THRESHOLD_NGN,
        "is_below_exemption_threshold": (
            LOW_INCOME_EXEMPTION_THRESHOLD_NGN is not None
            and chargeable_income <= LOW_INCOME_EXEMPTION_THRESHOLD_NGN
        ),
        "tax_computation": compute_tax(chargeable_income),
    }


def compute_tax(chargeable_income_naira: float) -> dict[str, Any]:
    """Walk PERSONAL_INCOME_TAX_BANDS_NGN in order, taxing only the slice of
    chargeable income inside each band. Bands above the income are omitted;
    the first (0%) band is always listed so an exempt user still sees why."""
    income_kobo = max(0, round(chargeable_income_naira * 100))
    bands: list[dict[str, Any]] = []
    total_kobo = 0
    for index, band in enumerate(PERSONAL_INCOME_TAX_BANDS_NGN):
        lower = round(band["min"] * 100)
        upper = None if band["max"] is None else round(band["max"] * 100)
        if index > 0 and income_kobo <= lower:
            break
        slice_kobo = max(0, (income_kobo if upper is None else min(income_kobo, upper)) - lower)
        tax_kobo = round(slice_kobo * band["rate"])
        total_kobo += tax_kobo
        bands.append({
            "band": _band_label(index, band),
            "rate_percent": round(band["rate"] * 100, 2),
            "taxable_naira": _to_naira(slice_kobo),
            "tax_naira": _to_naira(tax_kobo),
        })
    return {
        "bands": bands,
        "total_annual_tax_naira": _to_naira(total_kobo),
        "monthly_tax_naira": _to_naira(round(total_kobo / 12)),
        "effective_rate_percent": round(total_kobo / income_kobo * 100, 2) if income_kobo else 0.0,
    }


def _band_label(index: int, band: dict[str, Any]) -> str:
    if band["max"] is None:
        return f"Above ₦{band['min']:,.0f}"
    width = band["max"] - band["min"]
    return f"{'First' if index == 0 else 'Next'} ₦{width:,.0f}"


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
