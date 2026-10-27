"""Itemised tables returned alongside an answer, built in code from the same
context the model saw — never from the model's own output.

The model summarises; these tables carry every row. They exist for question
types where a single sentence hides the detail a user needs: comparisons
(category by category), tax estimates (band by band) and budget status
(budget by budget). Every cell is a
preformatted display string so clients render without re-deriving anything.

Shape: {"title", "columns", "rows", "footer", "note"}; `footer` rows are
totals, `note` is a one-line caveat or basis statement (may be None).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.classifier import QuestionCategory


def build_breakdown(category: QuestionCategory, context: dict[str, Any]) -> dict[str, Any] | None:
    if category == QuestionCategory.COMPARISON:
        return _comparison(context)
    if category == QuestionCategory.TAX:
        return _tax(context)
    if category == QuestionCategory.BUDGET_TRACK:
        return _budget(context)
    return None


_STATUS_LABELS = {"on_track": "On track", "at_risk": "At risk", "over_budget": "Over budget"}


def _budget(context: dict[str, Any]) -> dict[str, Any] | None:
    categories = context.get("current_month_category_status") or []
    if not context.get("has_budget") or not categories:
        return None
    month = _month_label(context["current_month"])
    rows = []
    for entry in categories:
        status = _STATUS_LABELS.get(entry["status"], entry["status"])
        if entry.get("is_fixed_cost"):
            status += " (fixed cost)"
        used = "-" if entry["percent_used"] is None else f"{entry['percent_used']:g}%"
        rows.append([entry["category_name"], _naira(entry["allocated_naira"]), _naira(entry["spent_naira"]),
                     used, status])
    allocated = round(sum(e["allocated_naira"] for e in categories), 2)
    spent = round(sum(e["spent_naira"] for e in categories), 2)
    elapsed = context.get("percent_of_current_month_elapsed")
    note = f"{elapsed:g}% of {month} has passed (data up to {context['as_of_date']})." if elapsed is not None else None
    if any(e.get("is_fixed_cost") for e in categories):
        note = (note + " " if note else "") + "Fixed costs such as rent are usually paid once a month."
    return {
        "title": f"Your budgets, {month}",
        "columns": ["Category", "Budget", "Spent", "Used", "Status"],
        "rows": rows,
        "footer": [["Total", _naira(allocated), _naira(spent),
                    f"{spent / allocated * 100:.1f}%" if allocated else "-", ""]],
        "note": note,
    }


def _naira(value: float | None) -> str:
    return f"₦{(value or 0):,.2f}"


def _signed_naira(value: float) -> str:
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f"{sign}₦{abs(value):,.2f}"


def _change(change: dict[str, Any]) -> str:
    text = _signed_naira(change["absolute"])
    if change.get("percent") is not None:
        text += f" ({change['percent']:+.1f}%)"
    return text


def _month_label(month: str) -> str:
    return date(int(month[:4]), int(month[5:7]), 1).strftime("%b %Y")


def _comparison(context: dict[str, Any]) -> dict[str, Any] | None:
    current, previous = context.get("current_month"), context.get("previous_month")
    if not current or not previous:
        return None
    current_label, previous_label = _month_label(current), _month_label(previous)
    rows = [
        [entry["category_name"], _naira(entry["previous_naira"]), _naira(entry["current_naira"]), _change(entry)]
        for entry in context.get("category_deltas", [])
    ]
    prev_totals, cur_totals = context["previous_totals_naira"], context["current_totals_naira"]
    footer = [
        ["Total spending", _naira(prev_totals["spend_naira"]), _naira(cur_totals["spend_naira"]),
         _change(context["spend_change"])],
        ["Total income", _naira(prev_totals["income_naira"]), _naira(cur_totals["income_naira"]),
         _change(context["income_change"])],
    ]
    note = None
    through = context.get("current_month_data_through")
    if through:
        day = date.fromisoformat(through)
        note = f"{current_label} covers 1-{day.day} {day.strftime('%b %Y')} only; the month isn't complete yet."
    return {
        "title": f"Spending by category: {previous_label} vs {current_label}",
        "columns": ["Category", previous_label, current_label, "Change"],
        "rows": rows,
        "footer": footer,
        "note": note,
    }


def _tax(context: dict[str, Any]) -> dict[str, Any] | None:
    computation = context.get("tax_computation")
    if not computation or not context.get("tax_bands_configured"):
        return None
    rows = [
        [band["band"], f"{band['rate_percent']:g}%", _naira(band["taxable_naira"]), _naira(band["tax_naira"])]
        for band in computation["bands"]
    ]
    footer = [
        ["Estimated tax per year", "", _naira(context["chargeable_income_naira"]),
         _naira(computation["total_annual_tax_naira"])],
        ["Per month", "", "", _naira(computation["monthly_tax_naira"])],
    ]
    basis = (
        "your last 12 months of income" if context.get("income_estimate_method") == "trailing_12_months"
        else "your average monthly income x 12"
    )
    relief = context.get("rent_relief_estimate_naira") or 0
    note = (
        f"Estimated annual income {_naira(context['estimated_annual_income_naira'])} (from {basis})"
        + (f", less rent relief {_naira(relief)}" if relief else "")
        + f" = chargeable income {_naira(context['chargeable_income_naira'])}. "
        f"Effective rate {computation['effective_rate_percent']:g}%. An estimate, not a filed return."
    )
    return {
        "title": "Estimated income tax, Nigeria Tax Act 2025",
        "columns": ["Band", "Rate", "Income in band", "Tax"],
        "rows": rows,
        "footer": footer,
        "note": note,
    }
