"""Context builders turn a user's raw transactions into the small, structured
`context` dict that gets embedded in the LLM prompt for a given question
category. Each builder takes the same `transactions` shape (see
data/synthetic_transactions.json) and returns a JSON-serializable dict.
"""
from app.classifier import QuestionCategory
from app.context_builders import comparison, income, merchant, spending, tax

_BUILDERS = {
    QuestionCategory.SPENDING: spending.build,
    QuestionCategory.INCOME: income.build,
    QuestionCategory.TAX: tax.build,
    QuestionCategory.COMPARISON: comparison.build,
    QuestionCategory.MERCHANT: merchant.build,
}


def build_context(category: QuestionCategory, transactions: list[dict]) -> dict:
    """Dispatch to the context builder for `category`.

    GENERAL questions get a lightweight default context (just the raw
    transaction count and date range) since there's no specialized builder
    for them — the base prompt is expected to carry the conversation.
    """
    builder = _BUILDERS.get(category)
    if builder is None:
        return _default_context(transactions)
    return builder(transactions)


def _default_context(transactions: list[dict]) -> dict:
    dates = [t["date"] for t in transactions if "date" in t]
    return {
        "transaction_count": len(transactions),
        "date_range": {"start": min(dates), "end": max(dates)} if dates else None,
    }
