"""Groq-backed classifier that buckets a user's question into a context
category, returning a confidence score alongside it.

This deliberately replaces the Engineering doc's original "deterministic
keyword routing, no LLM call" design (see CLAUDE.md, "Architecture", for
the updated pipeline description and the cost/latency trade-off this
implies — classification is no longer free, since it's a small model call
on every question). Originally implemented against Anthropic's Claude
Haiku; migrated to Groq alongside app/router.py so the whole pipeline
depends on one model provider instead of two.

Because this is now a model call, it must never see raw PII. app/main.py
anonymises the question BEFORE calling classify_question, not after, so
the classifier only ever sees anonymised text — see app/main.py's
docstring for the corrected pipeline order.

classify_question never raises: a failed or malformed model call, an
unrecognized intent, or a low confidence score all fall back to GENERAL
rather than taking down the whole /ask request over a classification
hiccup.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from typing import Any

from groq import Groq

logger = logging.getLogger(__name__)


class QuestionCategory(str, Enum):
    SPENDING = "spending"
    INCOME = "income"
    TAX = "tax"
    COMPARISON = "comparison"
    MERCHANT = "merchant"
    GENERAL = "general"
    BUDGET_TRACK = "budget_track"
    BUDGET_RECOMMEND = "budget_recommend"
    OUT_OF_SCOPE_ADVICE = "out_of_scope_advice"


@dataclass
class ClassificationResult:
    category: QuestionCategory
    confidence: float


# Deliberately its own env var rather than reusing router.GROQ_HAIKU_MODEL_ID:
# router.py imports QuestionCategory from this module, so importing router
# here would create a circular import — and keeping classification
# independently configurable from answer generation is also just a
# reasonable thing to want (a different model may suit structured
# tool-call classification better than freeform chat answers).
_CLASSIFIER_MODEL_ID = os.environ.get("GROQ_CLASSIFIER_MODEL_ID", "llama-3.1-8b-instant")

# Below this, the classification is treated as too uncertain to route on
# and is downgraded to GENERAL. The model's own reported confidence is
# still returned to the caller unchanged, so logs/observability can see
# that a downgrade happened rather than the model genuinely picking
# general.
_CONFIDENCE_THRESHOLD = 0.5

_MAX_TOOL_TOKENS = 200
_TOOL_NAME = "classify_question"

_CLASSIFIER_SYSTEM_PROMPT = """\
You classify a single question from a user of Moniger, a Nigerian fintech \
app, into exactly one intent so it can be routed to the right handler. \
Call the classify_question tool exactly once with your answer.

Intents:
- spending: questions about money going out — totals, category \
breakdowns, "how much did I spend on X". \
Examples: "How much did I spend on transport last month?", "What's my \
biggest spending category?"
- income: questions about money coming in — salary, freelance income, \
deposits, averages. \
Examples: "What was my income in March?", "How much freelance income did \
I earn?"
- tax: questions about personal income tax, PAYE, rent relief, or how \
much tax is owed. \
Examples: "How much tax do I owe this year?", "How much rent relief can I \
claim?"
- comparison: explicit period-over-period comparisons of spending or \
income. \
Examples: "How does this month compare to last month?", "Did my income go \
up or down?"
- merchant: questions about a specific merchant, vendor, or recurring \
charge/subscription. \
Examples: "How much have I spent at Uber?", "Has Netflix charged me every \
month?"
- budget_track: the user is asking their STATUS against a budget they \
already have — how much is used, left, or whether they're over. \
Examples: "How am I tracking against my food budget?", "Am I over budget \
on transport?", "How much do I have left in my transport budget?"
- budget_recommend: the user is asking HOW to allocate, plan, or set a \
budget — guidance, not status. \
Examples: "How should I budget my salary?", "What's a reasonable amount \
to spend on food?", "How much could I set aside each month?"
- out_of_scope_advice: the user is asking about investment products, \
savings products, specific financial providers, or securities — this \
must always be caught here, never answered directly, so it can be \
cleanly deflected. \
Examples: "Should I put my savings in mutual funds or a fixed deposit?", \
"What crypto should I invest in?", "Which fintech should I move my \
savings to?", "Should I buy stocks or bonds?"
- general: anything else — greetings, capability questions, or topics \
unrelated to the user's finances.

Rules:
- Pick exactly one intent, even if a question could plausibly fit more \
than one — pick the closest match.
- budget_track and budget_recommend are both about budgets but differ on \
status ("how am I doing") versus planning ("how should I") — read the \
question's verb carefully.
- Any mention of a specific investment or savings PRODUCT, PROVIDER, or \
SECURITY (mutual funds, fixed deposits, stocks, crypto, bonds, specific \
fintech apps, "where should I invest/save") is out_of_scope_advice, even \
if phrased as a budgeting question — budgeting math ("how much could I \
save") is in scope, but product or provider recommendations are not.
- Set confidence honestly: use a low score (below 0.5) if the question is \
genuinely ambiguous or you are guessing, rather than picking a category \
you are not confident in.\
"""

# OpenAI-style function-calling shape (Groq's chat completions API is
# OpenAI-compatible) — note "parameters" here vs. Anthropic's "input_schema".
_CLASSIFY_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _TOOL_NAME,
        "description": (
            "Classify a Moniger Copilot user's question into exactly one "
            "intent, with a confidence score."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "enum": [category.value for category in QuestionCategory],
                    "description": "The single best-matching intent category.",
                },
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "Confidence in this classification, from 0 (a guess) to 1 (certain).",
                },
            },
            "required": ["intent", "confidence"],
        },
    },
}

_TOOL_CHOICE = {"type": "function", "function": {"name": _TOOL_NAME}}


@lru_cache(maxsize=1)
def _client() -> Groq:
    return Groq()


def _call_classifier_model(question: str) -> dict[str, Any]:
    """Call the classifier model with a forced tool call and return its raw
    {"intent": ..., "confidence": ...} tool arguments.

    Split out from classify_question so tests can mock this one call
    boundary instead of the Groq SDK itself.
    """
    response = _client().chat.completions.create(
        model=_CLASSIFIER_MODEL_ID,
        max_tokens=_MAX_TOOL_TOKENS,
        messages=[
            {"role": "system", "content": _CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        tools=[_CLASSIFY_TOOL],
        tool_choice=_TOOL_CHOICE,
    )

    tool_calls = response.choices[0].message.tool_calls or []
    for tool_call in tool_calls:
        if tool_call.function.name == _TOOL_NAME:
            return json.loads(tool_call.function.arguments)

    raise ValueError("Model response did not include the expected tool call.")


def classify_question(question: str) -> ClassificationResult:
    """Classify `question` into one of the nine QuestionCategory values.

    Calls the classifier model with a forced tool call so it must return a
    single intent plus a confidence score (see _CLASSIFY_TOOL). Falls back
    to GENERAL with confidence 0.0 if the call fails or the response is
    malformed, and downgrades to GENERAL (keeping the model's own reported
    confidence) if that confidence is below _CONFIDENCE_THRESHOLD — see
    the module docstring for why this never raises instead.
    """
    try:
        raw = _call_classifier_model(question)
        category = QuestionCategory(raw["intent"])
        confidence = float(raw["confidence"])
    except Exception:
        logger.warning(
            "Classifier call failed or returned malformed output; defaulting to general.",
            exc_info=True,
        )
        return ClassificationResult(category=QuestionCategory.GENERAL, confidence=0.0)

    if confidence < _CONFIDENCE_THRESHOLD:
        logger.info(
            "Low-confidence classification (%.2f) for intent %r; defaulting to general.",
            confidence,
            category.value,
        )
        return ClassificationResult(category=QuestionCategory.GENERAL, confidence=confidence)

    return ClassificationResult(category=category, confidence=confidence)
