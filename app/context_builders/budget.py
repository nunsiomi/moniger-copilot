"""Builds context for the two budget layers defined in
docs/Moniger_Copilot_Scope_Addendum_Budget.docx:

- Layer 1 (BUDGET_TRACK): describes where the user stands against an
  existing budget. `percent_used`, `percent_of_period_elapsed`,
  `projected_end_of_period_spend`, and `status`
  (on_track/at_risk/over_budget) are computed in code, never by the LLM —
  a hard requirement, not a style preference — at both the category and
  subcategory level, plus an income-budget status (earned vs planned).
- Layer 2 (BUDGET_RECOMMEND): grounds allocation guidance (e.g. 50/30/20)
  in the user's actual income and spending.

Budget documents in the mirror schema (see app/data_access.py) are nested,
not flat: `expenseBreakdown[].subcategories[]` carries its own
`allocatedAmount`/`spentAmount`, and `incomeBreakdown[].subcategories[]`
carries `allocatedAmount`/`earnedAmount`. Critically, `spentAmount` is
*trusted directly from the budget document* rather than re-derived by
summing this user's transactions — moniger_copilot_fixtures_v2's README is
explicit that this figure is live in production ("any discrepancy the
intern's code produces is a bug in their code"). This also means budget
tracking never needs to join `categoryId` against transactions at all —
sidestepping the #1 silent-failure mode CLAUDE.md warns about (conflating
`categoryId` with `fiCategoryCode`) by construction, not by care.

Most personas show ~0% earned against their CURRENT month's income budget
because payday (day 25-28) falls after the mirror's most recent date — so
`build_tracking` also returns the previous complete month's category,
subcategory, and income budget status, not just the current month's.

Both `build_tracking` and `build_recommend` take `as_of` (a `datetime.date`)
as an explicit parameter, sourced from `app.data_access.get_as_of_date()` —
see that function's docstring for why "now" is the mirror's global most
recent date rather than this one user's own most recent transaction (a
quiet user can trail the real snapshot point by a day or more just by
chance, which would understate how far into the budget period "now"
actually is).
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from app.context_builders.income import build as build_income
from app.context_builders.spending import build as build_spending

_STATUS_ON_TRACK = "on_track"
_STATUS_AT_RISK = "at_risk"
_STATUS_OVER_BUDGET = "over_budget"


def _to_naira(kobo: float) -> float:
    return round(kobo / 100, 2)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO datetime string (already unwrapped from Mongo's
    `{"$date": ...}` by app/data_access.py) into a naive `datetime`."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _budget_for_month(budgets: list[dict[str, Any]], month: str | None) -> dict[str, Any] | None:
    if month is None:
        return None
    for budget in budgets:
        budget_month = budget.get("budgetMonth")
        if budget_month and budget_month[:7] == month:
            return budget
    return None


def _status_for(percent_used: float | None, percent_elapsed: float | None) -> str:
    if percent_used is not None and percent_used > 100:
        return _STATUS_OVER_BUDGET
    if percent_used is not None and percent_elapsed is not None and percent_used > percent_elapsed:
        return _STATUS_AT_RISK
    return _STATUS_ON_TRACK


def _percent_elapsed(budget: dict[str, Any], as_of: date | None) -> float:
    """Return how far through the budget's period `as_of` falls, as a
    percentage. `as_of=None` means the period is already complete (a past
    month), so elapsed is 100% by definition.

    `as_of` is a calendar date, treated as running through its END
    (23:59:59) rather than its start — "as of July 20" means the day has
    happened, not that it's just beginning. This is what makes the figure
    match moniger_copilot_fixtures_v2's own ground_truth.py exactly (AS_OF
    2026-07-20, 64.5% of July elapsed); treating it as midnight-at-the-start
    would understate elapsed by almost a full day.
    """
    if as_of is None:
        return 100.0
    period_start = _parse_dt(budget["periodStart"])
    period_end = _parse_dt(budget["periodEnd"])
    total_seconds = (period_end - period_start).total_seconds()
    if total_seconds <= 0:
        return 100.0
    as_of_instant = datetime.combine(as_of, time.max)
    elapsed_seconds = (as_of_instant - period_start).total_seconds()
    elapsed_seconds = max(0.0, min(elapsed_seconds, total_seconds))
    return round((elapsed_seconds / total_seconds) * 100, 1)


def _projected_spend(spent_kobo: int, percent_elapsed: float) -> float:
    """Project full-period spend at the user's current pace: spend-so-far
    divided by the fraction of the period elapsed. Computed in code per
    CLAUDE.md's Budget feature requirement — the model must never derive
    this itself."""
    if percent_elapsed <= 0:
        return _to_naira(spent_kobo)
    return _to_naira(round(spent_kobo / (percent_elapsed / 100)))


# Categories normally paid in one lump each month (rent). Projecting them at
# a daily pace is meaningless — ₦280k of rent paid on day 1 is not heading
# for ₦434k — so their projection is what has already been paid, and
# `is_fixed_cost` lets the prompt explain a high percentage as a fixed cost
# rather than overspending. Status is left as computed (ground truth).
_FIXED_COST_CATEGORIES = frozenset({"Housing"})


def _projection(spent_kobo: int, percent_elapsed: float, fixed_cost: bool) -> float:
    return _to_naira(spent_kobo) if fixed_cost else _projected_spend(spent_kobo, percent_elapsed)


def _category_and_subcategory_status(
    budget: dict[str, Any], percent_elapsed: float
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    category_status = []
    subcategory_status = []
    budgeted_categories = []

    for entry in budget.get("expenseBreakdown", []):
        category_name = entry.get("category_name", "Unknown")
        budgeted_categories.append(category_name)
        allocated_kobo = int(entry.get("total_allocated", 0))
        subcategories = entry.get("subcategories", [])
        spent_kobo = sum(int(s.get("spentAmount", 0)) for s in subcategories)
        percent_used = round(spent_kobo / allocated_kobo * 100, 1) if allocated_kobo else None
        status = (_STATUS_OVER_BUDGET if spent_kobo > allocated_kobo
                  else _status_for(percent_used, percent_elapsed))
        fixed_cost = category_name in _FIXED_COST_CATEGORIES

        category_status.append({
            "category_id": entry.get("category_id"),
            "category_name": category_name,
            "allocated_naira": _to_naira(allocated_kobo),
            "spent_naira": _to_naira(spent_kobo),
            "remaining_naira": _to_naira(allocated_kobo - spent_kobo),
            "percent_used": percent_used,
            "projected_end_of_period_spend_naira": _projection(spent_kobo, percent_elapsed, fixed_cost),
            "is_fixed_cost": fixed_cost,
            "status": status,
        })

        for sub in subcategories:
            sub_allocated = int(sub.get("allocatedAmount", 0))
            sub_spent = int(sub.get("spentAmount", 0))
            sub_percent_used = round(sub_spent / sub_allocated * 100, 1) if sub_allocated else None
            subcategory_status.append({
                "category_name": category_name,
                "subcategory": sub.get("name", "Unknown"),
                "allocated_naira": _to_naira(sub_allocated),
                "spent_naira": _to_naira(sub_spent),
                "remaining_naira": _to_naira(sub_allocated - sub_spent),
                "percent_used": sub_percent_used,
                "projected_end_of_period_spend_naira": _projection(sub_spent, percent_elapsed, fixed_cost),
                "is_fixed_cost": fixed_cost,
                "status": (_STATUS_OVER_BUDGET if sub_spent > sub_allocated
                           else _status_for(sub_percent_used, percent_elapsed)),
            })

    return category_status, subcategory_status, budgeted_categories


def _income_budget_status(budget: dict[str, Any]) -> dict[str, Any] | None:
    income_breakdown = budget.get("incomeBreakdown") or []
    subcategories = [sub for entry in income_breakdown for sub in entry.get("subcategories", [])]
    if not subcategories:
        return None

    allocated_kobo = sum(int(sub.get("allocatedAmount", 0)) for sub in subcategories)
    earned_kobo = sum(int(sub.get("earnedAmount", 0)) for sub in subcategories)
    percent_earned = round(earned_kobo / allocated_kobo * 100, 1) if allocated_kobo else None

    return {
        "planned_naira": _to_naira(allocated_kobo),
        "earned_so_far_naira": _to_naira(earned_kobo),
        "percent_earned": percent_earned,
        "shortfall_naira": _to_naira(max(allocated_kobo - earned_kobo, 0)),
    }


def build_tracking(transactions: list[dict[str, Any]], budgets: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Return this user's budget status for the current month AND the
    previous complete month (see module docstring for why both — most
    personas show ~0% current-month income earned since payday falls after
    the mirror's most recent date). `as_of` is the mirror's global most
    recent date — see app.data_access.get_as_of_date.
    """
    months = sorted({b["budgetMonth"][:7] for b in budgets if b.get("budgetMonth")})
    current_month = as_of.strftime("%Y-%m")
    complete_months = [m for m in months if m < current_month]
    previous_month = complete_months[-1] if complete_months else None

    current_budget = _budget_for_month(budgets, current_month)
    previous_budget = _budget_for_month(budgets, previous_month)

    if current_budget is not None:
        percent_elapsed = _percent_elapsed(current_budget, as_of)
        current_category, current_subcategory, budgeted_categories = _category_and_subcategory_status(
            current_budget, percent_elapsed
        )
        current_income_budget = _income_budget_status(current_budget)
    else:
        percent_elapsed = None
        current_category, current_subcategory, budgeted_categories = [], [], []
        current_income_budget = None

    if previous_budget is not None:
        previous_category, previous_subcategory, _ = _category_and_subcategory_status(previous_budget, 100.0)
        previous_income_budget = _income_budget_status(previous_budget)
    else:
        previous_category, previous_subcategory, previous_income_budget = [], [], None

    return {
        "as_of_date": as_of.isoformat(),
        "current_month": current_month,
        "previous_month": previous_month,
        "has_budget": current_budget is not None,
        "budgeted_categories": budgeted_categories,
        "percent_of_current_month_elapsed": percent_elapsed,
        "current_month_category_status": current_category,
        "current_month_subcategory_status": current_subcategory,
        "current_month_income_budget": current_income_budget,
        "previous_month_category_status": previous_category,
        "previous_month_subcategory_status": previous_subcategory,
        "previous_month_income_budget": previous_income_budget,
    }


def build_recommend(transactions: list[dict[str, Any]], budgets: list[dict[str, Any]], as_of: date) -> dict[str, Any]:
    """Ground allocation guidance in the user's real income and spend.

    Returns the income figure being used, the current category split (as
    percentages of income), and how much is left after planned spending —
    the inputs a framework-based (50/30/20, pay-yourself-first,
    zero-based) recommendation is computed from. `as_of` is the mirror's
    global most recent date — see app.data_access.get_as_of_date.
    """
    income_context = build_income(transactions)
    spending_context = build_spending(transactions)

    current_month = as_of.strftime("%Y-%m")
    current_budget = _budget_for_month(budgets, current_month)
    declared_kobo = current_budget.get("totalMonthlyIncome") if current_budget else None
    if declared_kobo is None and current_budget:
        income_rows = [s for c in current_budget.get("incomeBreakdown", [])
                       for s in c.get("subcategories", [])]
        if income_rows:
            declared_kobo = sum(int(s.get("allocatedAmount", 0)) for s in income_rows)
    monthly_income = (_to_naira(int(declared_kobo)) if declared_kobo is not None
                      else income_context["average_monthly_income_naira"])
    distinct_spend_months = spending_context["distinct_months"] or 1

    category_split = []
    for entry in spending_context["category_breakdown"]:
        average_monthly_spend = entry["total_naira"] / distinct_spend_months
        percent_of_income = (
            round((average_monthly_spend / monthly_income) * 100, 1) if monthly_income else None
        )
        category_split.append({
            "category_name": entry["category_name"],
            "average_monthly_spend_naira": round(average_monthly_spend, 2),
            "percent_of_income": percent_of_income,
        })

    current_month = as_of.strftime("%Y-%m")
    current_budget = _budget_for_month(budgets, current_month)
    total_budgeted_naira = (
        _to_naira(sum(int(e.get("total_allocated", 0)) for e in current_budget.get("expenseBreakdown", [])))
        if current_budget is not None else 0.0
    )

    return {
        "average_monthly_income_naira": income_context["average_monthly_income_naira"],
        "budget_income_naira": monthly_income,
        "income_basis": "declared_budget" if declared_kobo is not None else "historical_average",
        "framework_50_30_20": _framework_allocations(round(monthly_income * 100)),
        "estimated_annual_income_naira": income_context["estimated_annual_income_naira"],
        "category_split": category_split,
        "total_budgeted_naira": total_budgeted_naira,
        "left_after_planned_spending_naira": round(monthly_income - total_budgeted_naira, 2),
    }


def _framework_allocations(income_kobo: int) -> dict[str, float]:
    """Allocate integer kobo; the final bucket retains the rounding remainder."""
    needs = income_kobo * 50 // 100
    wants = income_kobo * 30 // 100
    return {"needs_naira": _to_naira(needs), "wants_naira": _to_naira(wants),
            "remaining_for_savings_or_debt_naira": _to_naira(income_kobo - needs - wants)}
