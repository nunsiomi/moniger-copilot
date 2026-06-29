"""Assembles the system prompt and message list sent to Claude.

Keeps the actual Anthropic SDK call out of this module — prompt_builder
only knows how to turn (category, anonymised question, context) into the
`system` string and `messages` list that router.call_model expects.
"""
from __future__ import annotations

import json
from typing import Any

from app.classifier import QuestionCategory
from app.prompts.base_system_prompt import BASE_SYSTEM_PROMPT
from app.prompts.tax_system_prompt import TAX_SYSTEM_PROMPT

_CATEGORY_PROMPT_ADDENDA: dict[QuestionCategory, str] = {
    QuestionCategory.TAX: TAX_SYSTEM_PROMPT,
}


def build_system_prompt(category: QuestionCategory) -> str:
    """Return the base prompt, plus a category addendum if one exists."""
    addendum = _CATEGORY_PROMPT_ADDENDA.get(category)
    if addendum is None:
        return BASE_SYSTEM_PROMPT
    return f"{BASE_SYSTEM_PROMPT}\n\n{addendum}"


def build_messages(anonymised_question: str, context: dict[str, Any]) -> list[dict[str, str]]:
    """Return the `messages` array: the question plus its supporting context.

    The context is rendered as a fenced JSON block under a <context> tag so
    Claude can clearly distinguish "data to reason over" from "the user's
    actual words" in the same turn.
    """
    context_block = json.dumps(context, indent=2, sort_keys=True, default=str)
    user_content = (
        f"<context>\n{context_block}\n</context>\n\n"
        f"Question: {anonymised_question}"
    )
    return [{"role": "user", "content": user_content}]
