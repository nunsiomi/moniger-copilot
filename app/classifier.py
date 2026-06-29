"""Rule-based classifier that buckets a user's question into a context category.

Kept deterministic and dependency-free (no LLM call) so classification is
free, instant, and unit-testable without mocking the Anthropic client.
"""
from __future__ import annotations

from enum import Enum


class QuestionCategory(str, Enum):
    SPENDING = "spending"
    INCOME = "income"
    TAX = "tax"
    COMPARISON = "comparison"
    MERCHANT = "merchant"
    GENERAL = "general"


# Ordered by specificity — tax and comparison keywords are checked before the
# broader spending/income buckets so e.g. "compare my spending" lands on
# COMPARISON rather than SPENDING.
_CATEGORY_KEYWORDS: dict[QuestionCategory, tuple[str, ...]] = {
    QuestionCategory.TAX: (
        "tax", "paye", "vat", "wht", "withholding", "tin", "firs",
        "tax act", "relief", "levy", "remit",
    ),
    QuestionCategory.COMPARISON: (
        "compare", "versus", "vs ", "than last", "last month", "last year",
        "previous month", "previous year", "difference between", "trend",
    ),
    QuestionCategory.MERCHANT: (
        "merchant", "vendor", "subscription", "recurring", "biller",
        "where do i spend", "spent at", "spend at", "charged me",
        "charge me", "billed me", "bills me",
    ),
    QuestionCategory.INCOME: (
        "income", "salary", "earned", "earnings", "revenue", "deposit",
        "inflow", "received", "wage", "payroll",
    ),
    QuestionCategory.SPENDING: (
        "spend", "spent", "spending", "expense", "expenses", "outflow",
        "budget", "transaction", "purchase", "bought", "withdrew",
    ),
}


def classify_question(question: str) -> QuestionCategory:
    """Return the best-matching category for a free-text question.

    Falls back to GENERAL when no keyword matches, which routes to the
    base (non-specialized) context builder and prompt.
    """
    normalized = f" {question.lower().strip()} "

    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return category

    return QuestionCategory.GENERAL
