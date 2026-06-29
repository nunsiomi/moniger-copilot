"""Haiku vs Sonnet routing — picks the cheaper model for simple lookups and
the stronger model for anything requiring multi-step reasoning (tax,
period-over-period comparisons), then makes the actual Anthropic API call.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import anthropic

from app.classifier import QuestionCategory

HAIKU_MODEL_ID = os.environ.get("HAIKU_MODEL_ID", "claude-haiku-4-5")
SONNET_MODEL_ID = os.environ.get("SONNET_MODEL_ID", "claude-sonnet-4-6")

# Tax and comparison questions involve multi-step arithmetic and bracket
# logic that Haiku is more prone to get subtly wrong — route those to
# Sonnet. Everything else is a lookup/summarization task Haiku handles well
# at a fraction of the cost and latency.
_SONNET_CATEGORIES = frozenset({QuestionCategory.TAX, QuestionCategory.COMPARISON})

_MAX_RESPONSE_TOKENS = 1024


@lru_cache(maxsize=1)
def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


def select_model(category: QuestionCategory) -> str:
    """Return the model ID to use for a question in `category`."""
    return SONNET_MODEL_ID if category in _SONNET_CATEGORIES else HAIKU_MODEL_ID


def call_model(model_id: str, system_prompt: str, messages: list[dict[str, Any]]) -> str:
    """Call the Messages API and return the response's text content.

    Effort is only set for Sonnet — the `output_config.effort` parameter
    errors on Haiku 4.5, which has no effort levels to choose between.
    """
    kwargs: dict[str, Any] = {
        "model": model_id,
        "max_tokens": _MAX_RESPONSE_TOKENS,
        "system": system_prompt,
        "messages": messages,
    }
    if model_id == SONNET_MODEL_ID:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "medium"}

    response = _client().messages.create(**kwargs)

    for block in response.content:
        if block.type == "text":
            return block.text

    return ""
