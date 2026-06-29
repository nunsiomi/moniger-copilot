"""Builds context for TAX questions: annualized income estimate, rent paid,
and the current tax bands/relief figures the LLM must use (see
app/prompts/tax_system_prompt.py for the source of truth and the
compliance TODO on those figures).
"""
from __future__ import annotations

from typing import Any

from app.prompts.tax_system_prompt import (
    PERSONAL_INCOME_TAX_BANDS_NGN,
    RENT_RELIEF_CAP_NGN,
    RENT_RELIEF_RATE,
)


def build(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    credits = [t for t in transactions if t.get("type") == "credit"]
    rent_debits = [t for t in transactions if t.get("type") == "debit" and t.get("category") == "Rent"]

    total_income = sum(t["amount"] for t in credits)
    dates = [t["date"] for t in credits if "date" in t]
    months_spanned = len({d[:7] for d in dates}) or 1
    annualized_income = round(total_income / months_spanned * 12, 2)

    annual_rent_paid = round(sum(t["amount"] for t in rent_debits) / months_spanned * 12, 2)
    estimated_rent_relief = round(min(annual_rent_paid * RENT_RELIEF_RATE, RENT_RELIEF_CAP_NGN), 2)

    return {
        "annualized_income_estimate": annualized_income,
        "annual_rent_paid_estimate": annual_rent_paid,
        "rent_relief_cap": RENT_RELIEF_CAP_NGN,
        "rent_relief_rate": RENT_RELIEF_RATE,
        "estimated_rent_relief": estimated_rent_relief,
        "tax_bands": PERSONAL_INCOME_TAX_BANDS_NGN,
        "months_of_data": months_spanned,
        "disclaimer": (
            "Figures are estimates derived from the user's transaction history, "
            "not a filed tax return. Confirm with FIRS or a tax professional "
            "before relying on these numbers."
        ),
    }
