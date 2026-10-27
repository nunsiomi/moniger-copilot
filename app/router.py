"""Haiku vs Sonnet routing — picks the cheaper model for simple lookups and
the stronger model for anything requiring multi-step reasoning (tax,
period-over-period comparisons), then makes the actual Groq API call.

See docs/Moniger_Copilot_Engineering.docx ("Model routing") for which
categories go to which model and why. "Haiku"/"Sonnet" name the two
routing tiers (small/fast vs. large/strong), not Anthropic models
specifically — both tiers are served by Groq-hosted models (currently
OpenAI's open-weight gpt-oss family; see HAIKU_MODEL_ID/SONNET_MODEL_ID
below — Groq periodically retires older model IDs, so treat these
defaults as the current known-good pair, not a permanent pin).
app.classifier's own intent-classification call also runs on Groq now
(see its own module docstring), but reads a separate GROQ_CLASSIFIER_MODEL_ID
env var rather than GROQ_HAIKU_MODEL_ID — partly to avoid a circular
import (classifier.py can't import this module back, since this module
already imports QuestionCategory from classifier.py), partly to keep
classification independently tunable from answer generation.

call_model requests Structured Outputs (`response_format={"type":
"json_schema", ...}`) matching the exact {answer, explanation, confidence,
sources} shape BASE_SYSTEM_PROMPT already describes in prose — this
started as a reliability fix, not a style choice: gpt-oss (a reasoning
model family) occasionally spent enough of its hidden reasoning on a
question that the visible content it returned wasn't valid JSON at all,
which app.response_parser correctly refused to guess at and surfaced as
ERR_INTERNAL. Constraining the schema at the API level, rather than
trusting the prompt's prose instructions alone, eliminated that failure
mode in testing across both tiers. response_parser still validates/coerces
every field independently rather than trusting the schema blindly — a
model honoring its OWN declared schema is not the same guarantee as the
response actually being trustworthy content.
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

HAIKU_MODEL_ID = os.environ.get("GROQ_HAIKU_MODEL_ID", "openai/gpt-oss-20b")
SONNET_MODEL_ID = os.environ.get("GROQ_SONNET_MODEL_ID", "openai/gpt-oss-120b")

# Sonnet is reserved for questions that require multi-step arithmetic where
# a stronger model materially reduces error rate (progressive tax bands,
# period-over-period deltas, framework-based allocation reasoning), plus
# OUT_OF_SCOPE_ADVICE — not for arithmetic, but because the guardrail it
# enforces (never repeat back a specific investment/savings product or
# asset-class term, even while declining — see
# app.prompts.budget_system_prompt.OUT_OF_SCOPE_DEFLECTION_PROMPT) is
# non-negotiable per CLAUDE.md, and Haiku-tier compliance with that
# mechanical wording rule tested meaningfully worse than Sonnet-tier's in
# practice (verified against tests/eval/test_set.json's adversarial
# cases). app.response_parser also backstops this in code, the same way
# it backstops the BUDGET_RECOMMEND disclaimer — routing tier alone isn't
# treated as a sufficient guarantee for a non-negotiable guardrail.
# Everything else (lookups/summarizations) defaults to Haiku.
_SONNET_CATEGORIES: frozenset[QuestionCategory] = frozenset({
    QuestionCategory.TAX,
    QuestionCategory.COMPARISON,
    QuestionCategory.BUDGET_RECOMMEND,
    QuestionCategory.OUT_OF_SCOPE_ADVICE,
})

# gpt-oss is a reasoning model — it spends part of this budget on hidden
# reasoning tokens before it ever emits the final JSON document, and with
# response_format enforcing a strict schema (see _RESPONSE_FORMAT below),
# Groq validates server-side: if reasoning eats the whole budget before a
# complete document exists, the call now fails outright
# (groq.BadRequestError: "json_validate_failed... max completion tokens
# reached before generating a valid document") instead of quietly
# returning truncated text for app.response_parser to reject. Bumped from
# 1024 after a harder dual-topic question (decline an investment question
# while also answering its embedded budgeting question — see
# tests/eval/test_set.json eval-28) hit exactly that wall in testing.
_MAX_RESPONSE_TOKENS = 2048
_RATE_LIMIT_MAX_RETRIES = 1
_RATE_LIMIT_BACKOFF_SECONDS = 2

# Mirrors the {answer, explanation, confidence, sources} shape
# BASE_SYSTEM_PROMPT instructs in prose (app/prompts/base_system_prompt.py)
# — passed as response_format so the API itself rejects/repairs a
# malformed completion instead of app.response_parser having to guess at
# prose-wrapped or truncated JSON. See the module docstring for why this
# was added.
_RESPONSE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "explanation": {"type": "string"},
        "period": {"type": "string"},
        "confidence": {"type": "number"},
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "explanation", "period", "confidence", "sources"],
    "additionalProperties": False,
}
_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "copilot_response", "schema": _RESPONSE_JSON_SCHEMA},
}


@lru_cache(maxsize=1)
def _client() -> Groq:
    return Groq(max_retries=0, timeout=25)


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
                response_format=_RESPONSE_FORMAT,
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
