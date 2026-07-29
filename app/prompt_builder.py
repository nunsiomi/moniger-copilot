"""Assembles the system prompt and message list sent to Groq.

Keeps the actual Groq SDK call out of this module — prompt_builder
only knows how to turn (category, anonymised question, context) into the
`system` string and `messages` list that router.call_model expects. Token
counting is therefore a rough ~4-chars-per-token estimate rather than a
real tokenizer call: an approximation is fine for a truncation safety net,
and it keeps this module free of any network dependency.
"""
from __future__ import annotations

import json
from typing import Any

from app import router
from app.classifier import QuestionCategory
from app.prompts.base_system_prompt import BASE_SYSTEM_PROMPT
from app.prompts.budget_system_prompt import (
    BUDGET_RECOMMEND_SYSTEM_PROMPT,
    BUDGET_TRACK_SYSTEM_PROMPT,
    OUT_OF_SCOPE_DEFLECTION_PROMPT,
)
from app.prompts.tax_system_prompt import TAX_SYSTEM_PROMPT

_CATEGORY_ADDENDA: dict[QuestionCategory, str] = {
    QuestionCategory.TAX: TAX_SYSTEM_PROMPT,
    QuestionCategory.BUDGET_TRACK: BUDGET_TRACK_SYSTEM_PROMPT,
    QuestionCategory.BUDGET_RECOMMEND: BUDGET_RECOMMEND_SYSTEM_PROMPT,
    QuestionCategory.OUT_OF_SCOPE_ADVICE: OUT_OF_SCOPE_DEFLECTION_PROMPT,
}

# Context token budget, enforced in code rather than left to the model to
# self-limit. Sonnet gets a larger allowance since it's only routed
# tax/comparison/budget_recommend questions, which tend to carry more
# context (bands, two time windows, multiple budgets) than a Haiku lookup.
_HAIKU_CONTEXT_TOKEN_CAP = 4000
_SONNET_CONTEXT_TOKEN_CAP = 8000
_CHARS_PER_TOKEN_ESTIMATE = 4


def build_system_prompt(category: QuestionCategory) -> str:
    """Return the base prompt, plus a category addendum if one exists.

    See app/prompts/base_system_prompt.py, app/prompts/tax_system_prompt.py,
    and app/prompts/budget_system_prompt.py for the prompt content this
    should assemble.
    """
    addendum = _CATEGORY_ADDENDA.get(category)
    if addendum is None:
        return BASE_SYSTEM_PROMPT
    return f"{BASE_SYSTEM_PROMPT}\n\n{addendum}"


def build_messages(category: QuestionCategory, anonymised_question: str, context: dict[str, Any]) -> list[dict[str, str]]:
    """Return the `messages` array: the question plus its supporting context.

    Context is trimmed to fit the token cap for whichever model
    router.select_model would pick for `category`, before being embedded
    in the user turn.
    """
    token_cap = _SONNET_CONTEXT_TOKEN_CAP if router.select_model(category) == router.SONNET_MODEL_ID else _HAIKU_CONTEXT_TOKEN_CAP
    bounded_context = _within_token_budget(context, token_cap)

    content = (
        f"<context>\n{json.dumps(bounded_context, indent=2, default=str)}\n</context>\n\n"
        f"<question>\n{anonymised_question}\n</question>"
    )
    return [{"role": "user", "content": content}]


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE)


_MAX_TRUNCATION_PASSES = 5


def _within_token_budget(context: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Trim `context`'s largest list field(s) until its serialized size
    fits `max_tokens`, marking the result if anything was dropped.

    Generic on purpose — every context builder puts its "many items" data
    in a list-valued field (category_breakdown, merchant_breakdown,
    budgets, etc.), so shrinking the largest list repeatedly works
    uniformly across categories without category-specific logic here.
    Each pass estimates how many items would fit and slices straight to
    that count, rather than dropping one item at a time -- with a
    pathologically large list, removing a single item per pass would mean
    a pass count proportional to the list's length, so this instead
    converges in a small, fixed number of passes regardless of input size.
    """
    trimmed = context
    for _ in range(_MAX_TRUNCATION_PASSES):
        current_tokens = _estimate_tokens(json.dumps(trimmed, indent=2, default=str))
        if current_tokens <= max_tokens:
            break

        list_field = _largest_list_field(trimmed)
        if list_field is None:
            break

        items = trimmed[list_field]
        if len(items) <= 1:
            break

        tokens_without_list = _estimate_tokens(json.dumps({**trimmed, list_field: []}, indent=2, default=str))
        budget_for_list = max(0, max_tokens - tokens_without_list)
        tokens_per_item = max(1, (current_tokens - tokens_without_list) / len(items))
        target_count = max(1, min(len(items) - 1, int(budget_for_list / tokens_per_item)))

        trimmed = {**trimmed, list_field: items[:target_count], "_context_truncated": True}

    return trimmed


def _largest_list_field(context: dict[str, Any]) -> str | None:
    list_fields = [key for key, value in context.items() if isinstance(value, list) and value]
    if not list_fields:
        return None
    return max(list_fields, key=lambda key: len(context[key]))
