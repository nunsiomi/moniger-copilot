"""Haiku vs Sonnet routing — picks the cheaper model for simple lookups and
the stronger model for anything requiring multi-step reasoning (tax,
period-over-period comparisons), then makes the actual Groq API call.

See docs/Moniger_Copilot_Engineering.docx ("Model routing") for which
categories go to which model and why. "Haiku"/"Sonnet" name the two
routing tiers (small/fast vs. large/strong), not Anthropic models
specifically — both tiers are served by Groq-hosted Llama models.
app.classifier's own intent-classification call also runs on Groq now
(see its own module docstring), but reads a separate GROQ_CLASSIFIER_MODEL_ID
env var rather than GROQ_HAIKU_MODEL_ID — partly to avoid a circular
import (classifier.py can't import this module back, since this module
already imports QuestionCategory from classifier.py), partly to keep
classification independently tunable from answer generation.
"""
from __future__ import annotations

import logging
import os
import time
from functools import lru_cache
from typing import Any

from groq import Groq, RateLimitError

from app.classifier import QuestionCategory

logger = logging.getLogger(__name__)

HAIKU_MODEL_ID = os.environ.get("GROQ_HAIKU_MODEL_ID", "llama-3.1-8b-instant")
SONNET_MODEL_ID = os.environ.get("GROQ_SONNET_MODEL_ID", "llama-3.3-70b-versatile")

# Sonnet is reserved for questions that require multi-step arithmetic where
# a stronger model materially reduces error rate: progressive tax bands,
# period-over-period deltas, and framework-based allocation reasoning.
# Everything else (lookups/summarizations) defaults to Haiku.
_SONNET_CATEGORIES: frozenset[QuestionCategory] = frozenset({
    QuestionCategory.TAX,
    QuestionCategory.COMPARISON,
    QuestionCategory.BUDGET_RECOMMEND,
})

_MAX_RESPONSE_TOKENS = 1024
_RATE_LIMIT_MAX_RETRIES = 1
_RATE_LIMIT_BACKOFF_SECONDS = 2


@lru_cache(maxsize=1)
def _client() -> Groq:
    return Groq()


def select_model(category: QuestionCategory) -> str:
    """Return the model ID to use for a question in `category`."""
    return SONNET_MODEL_ID if category in _SONNET_CATEGORIES else HAIKU_MODEL_ID


def call_model(model_id: str, system_prompt: str, messages: list[dict[str, Any]]) -> str:
    """Call the Groq chat completions API and return the response's text content.

    Retries exactly once, after a fixed 2s backoff, if the call is rate
    limited (HTTP 429). Any other error (auth, bad request, server error)
    is raised immediately — a rate limit is the one failure mode worth a
    blind retry, since it's transient and self-resolving. Groq's API is
    OpenAI-style (no separate top-level `system` param like Anthropic's),
    so `system_prompt` is folded into `messages` as the leading system
    turn here rather than in prompt_builder, keeping build_messages'
    output provider-agnostic.
    """
    full_messages = [{"role": "system", "content": system_prompt}, *messages]

    last_error: RateLimitError | None = None
    for attempt in range(_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            response = _client().chat.completions.create(
                model=model_id,
                max_tokens=_MAX_RESPONSE_TOKENS,
                messages=full_messages,
            )
            return _extract_text(response)
        except RateLimitError as exc:
            last_error = exc
            if attempt >= _RATE_LIMIT_MAX_RETRIES:
                break
            logger.warning(
                "Rate limited calling %s, retrying once after %ss backoff.",
                model_id,
                _RATE_LIMIT_BACKOFF_SECONDS,
            )
            time.sleep(_RATE_LIMIT_BACKOFF_SECONDS)

    raise last_error


def _extract_text(response: Any) -> str:
    """Return the completion's text content."""
    return response.choices[0].message.content
