"""Builds context for the two budget layers defined in
docs/Moniger_Copilot_Scope_Addendum_Budget.docx:

- Layer 1 (BUDGET_TRACK, section 2): describes where the user stands
  against their existing budgets. `build_tracking` must compute
  percent_used, percent_of_period_elapsed, projected_end_of_period_spend,
  and a status of "on_track" | "at_risk" | "over_budget" — in code, never
  by the LLM (section 2.3), so the arithmetic is guaranteed correct.
- Layer 2 (BUDGET_RECOMMEND, section 3): grounds allocation guidance (e.g.
  50/30/20) in the user's actual income and spending. `build_recommend`
  must surface income, current category splits, and how much is left after
  planned spending — never a specific savings/investment recommendation,
  that boundary is enforced in the prompt (see
  app/prompts/budget_system_prompt.py), not here.

Expects each budget dict to match the schema in Addendum section 5.1:
budget_id, user_id, category, subcategory, amount, period_type,
period_start, period_end, created_at, status. Expects each transaction
dict to match the shape used by the other context builders (date, amount,
type, category, merchant).
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.context_builders.income import build as build_income
from app.context_builders.spending import build as build_spending

_STATUS_ON_TRACK = "on_track"
_STATUS_AT_RISK = "at_risk"
_STATUS_OVER_BUDGET = "over_budget"

_TOP_TRANSACTIONS_LIMIT = 3


def build_tracking(transactions: list[dict[str, Any]], budgets: list[dict[str, Any]]) -> dict[str, Any]:
    """Join active budgets with categorized spend for the same period.

    Returns one entry per active budget with the plan (amount), the actual
    (spend to date in that category/period), percent_used,
    percent_of_period_elapsed, projected_end_of_period_spend, status, and
    the largest contributing transactions — see Addendum section 2.2 for
    what a good tracking response includes.
    """
    as_of = _as_of_date(transactions)
    tracked = [_track_one(budget, transactions, as_of) for budget in _active_budgets(budgets)]
    return {"as_of_date": as_of.isoformat(), "budgets": tracked}


def _track_one(budget: dict[str, Any], transactions: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    period_start = date.fromisoformat(budget["period_start"])
    period_end = date.fromisoformat(budget["period_end"])
    category = budget.get("category")

    in_period = [
        t for t in transactions
        if t.get("type") == "debit"
        and "amount" in t
        and t.get("category") == category
        and t.get("date")
        and period_start <= date.fromisoformat(t["date"]) <= period_end
    ]

    actual_spend = sum(t["amount"] for t in in_period)
    budget_amount = budget["amount"]
    percent_used = round((actual_spend / budget_amount) * 100, 1) if budget_amount else None

    total_days = (period_end - period_start).days + 1
    elapsed_days = min(max((as_of - period_start).days + 1, 0), total_days)
    percent_of_period_elapsed = round((elapsed_days / total_days) * 100, 1) if total_days else 0.0
    elapsed_fraction = elapsed_days / total_days if total_days else 0.0

    if elapsed_fraction > 0:
        projected_end_of_period_spend = round(actual_spend / elapsed_fraction, 2)
    else:
        projected_end_of_period_spend = round(actual_spend, 2)

    status = _status_for(actual_spend, budget_amount, projected_end_of_period_spend)
    top_transactions = sorted(in_period, key=lambda t: t["amount"], reverse=True)[:_TOP_TRANSACTIONS_LIMIT]

    return {
        "budget_id": budget.get("budget_id"),
        "category": category,
        "subcategory": budget.get("subcategory"),
        "budget_amount": budget_amount,
        "period_start": budget["period_start"],
        "period_end": budget["period_end"],
        "actual_spend": round(actual_spend, 2),
        "remaining_amount": round(budget_amount - actual_spend, 2),
        "percent_used": percent_used,
        "percent_of_period_elapsed": percent_of_period_elapsed,
        "projected_end_of_period_spend": projected_end_of_period_spend,
        "status": status,
        "top_transactions": [
            {"date": t.get("date"), "merchant": t.get("merchant"), "amount": t["amount"]}
            for t in top_transactions
        ],
    }


def _status_for(actual_spend: float, budget_amount: float, projected_spend: float) -> str:
    if budget_amount and actual_spend > budget_amount:
        return _STATUS_OVER_BUDGET
    if budget_amount and projected_spend > budget_amount:
        return _STATUS_AT_RISK
    return _STATUS_ON_TRACK


def _as_of_date(transactions: list[dict[str, Any]]) -> date:
    """The most recent transaction date stands in for "today" for
    period-elapsed math, the same way comparison.py derives "current month"
    from the data instead of the wall clock — this keeps tracking numbers
    reproducible against fixed historical/synthetic data. Falls back to the
    real current date only when there's no transaction data at all to
    derive one from.
    """
    dates = [t["date"] for t in transactions if t.get("date")]
    if not dates:
        return date.today()
    return date.fromisoformat(max(dates))


def build_recommend(transactions: list[dict[str, Any]], budgets: list[dict[str, Any]]) -> dict[str, Any]:
    """Ground allocation guidance in the user's real income and spend.

    Returns the income figure being used, the current category split (as
    percentages of income), and how much is left after planned spending —
    the inputs a framework-based (50/30/20, pay-yourself-first,
    zero-based) recommendation is computed from. See Addendum section 3.2.
    """
    income_context = build_income(transactions)
    spending_context = build_spending(transactions)

    monthly_income = income_context["average_monthly_income"]
    distinct_spend_months = spending_context["distinct_months"] or 1

    category_split = []
    for entry in spending_context["category_breakdown"]:
        average_monthly_spend = entry["total"] / distinct_spend_months
        percent_of_income = (
            round((average_monthly_spend / monthly_income) * 100, 1) if monthly_income else None
        )
        category_split.append({
            "category": entry["category"],
            "average_monthly_spend": round(average_monthly_spend, 2),
            "percent_of_income": percent_of_income,
        })

    total_budgeted = sum(b["amount"] for b in _active_budgets(budgets))

    return {
        "average_monthly_income": monthly_income,
        "estimated_annual_income": income_context["estimated_annual_income"],
        "category_split": category_split,
        "total_budgeted_amount": round(total_budgeted, 2),
        "left_after_planned_spending": round(monthly_income - total_budgeted, 2),
    }


def _active_budgets(budgets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter to budgets with status == "active" (Addendum section 5.3)."""
    return [b for b in budgets if b.get("status") == "active"]
