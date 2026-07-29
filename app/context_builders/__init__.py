"""Context builders turn a user's raw transactions (and, for budget
questions, budget definitions) into the small, structured `context` dict
that gets embedded in the LLM prompt for a given question category. Each
builder returns a JSON-serializable dict — see the individual modules for
the expected input/output shape.
"""
from typing import Callable

from app.classifier import QuestionCategory
from app.context_builders import budget, comparison, income, merchant, spending, tax

# BUDGET_TRACK and BUDGET_RECOMMEND both route into
# app/context_builders/budget.py, one function each. GENERAL and
# OUT_OF_SCOPE_ADVICE have no entry here on purpose — they fall through to
# _default_context below, since OUT_OF_SCOPE_ADVICE is a deflection handled
# entirely by app/prompts/budget_system_prompt.py and GENERAL needs no
# transaction-grounded context at all.
_BUILDERS: dict[QuestionCategory, Callable[..., dict]] = {
    QuestionCategory.SPENDING: lambda transactions, budgets: spending.build(transactions),
    QuestionCategory.INCOME: lambda transactions, budgets: income.build(transactions),
    QuestionCategory.TAX: lambda transactions, budgets: tax.build(transactions),
    QuestionCategory.COMPARISON: lambda transactions, budgets: comparison.build(transactions),
    QuestionCategory.MERCHANT: lambda transactions, budgets: merchant.build(transactions),
    QuestionCategory.BUDGET_TRACK: lambda transactions, budgets: budget.build_tracking(transactions, budgets or []),
    QuestionCategory.BUDGET_RECOMMEND: lambda transactions, budgets: budget.build_recommend(transactions, budgets or []),
}


def build_context(category: QuestionCategory, transactions: list[dict], budgets: list[dict] | None = None) -> dict:
    """Dispatch to the context builder for `category`.

    GENERAL and OUT_OF_SCOPE_ADVICE questions get a lightweight default
    context since there's no specialized builder for them.
    """
    builder = _BUILDERS.get(category)
    if builder is None:
        return _default_context(transactions)
    return builder(transactions, budgets)


def _default_context(transactions: list[dict]) -> dict:
    """Fallback context: just the transaction count and date range."""
    dates = [t["date"] for t in transactions if isinstance(t, dict) and t.get("date")]
    return {
        "transaction_count": len(transactions),
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }
