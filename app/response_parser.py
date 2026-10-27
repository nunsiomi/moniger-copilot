"""Parses the LLM's raw text response into the structured shape the API
returns to clients, tolerating the common ways a model deviates from "raw
JSON only" (markdown code fences, leading/trailing prose).

`question_type` and `cache_hit` are not extracted from the model's text —
the model never sees the classified category or the cache status, so
these are passed in by the caller and stamped onto the result. This also
means `parse` is meant to run on every request, cache hit or miss alike
(see app/main.py): what's cached is the raw model text, not a pre-parsed
result, so `cache_hit` here always reflects the current request.
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

from app.classifier import QuestionCategory
from app.prompts.budget_system_prompt import BUDGET_RECOMMEND_DISCLAIMER

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class ParsedResponse(TypedDict):
    answer: str
    explanation: str
    period: str | None
    confidence: float
    sources: list[str]
    question_type: str
    cache_hit: bool


class ResponseParseError(Exception):
    """Raised when the model's output cannot be coerced into ParsedResponse."""


# Mirrors moniger_copilot_tests/test_copilot.py's BANNED_PRODUCTS list
# exactly (see CLAUDE.md's "Budget feature" section) — "invest" is
# deliberately a stem, not "investment"/"investing" spelled out separately;
# it already matches both as a substring, plus any other verb form, and
# also happens to catch "arm invest"/"i-invest" as a side effect. Checked
# as a case-insensitive substring, same as the intern test suite's own
# check.
_FORBIDDEN_TERMS: tuple[str, ...] = (
    "mutual fund", "fixed deposit", "treasury bill", "t-bill", "eurobond",
    "etf", "index fund", "money market fund", "invest", "stock", "crypto",
    "piggyvest", "cowrywise", "risevest", "rise vest", "bamboo", "trove",
    "chaka", "stanbic", "arm invest", "i-invest",
)
_OUT_OF_SCOPE_SAFE_ANSWER = (
    "That's outside what I can help with here — I'm not able to weigh in on "
    "specific financial products or providers. I can help with your spending, "
    "income, tax, or budgeting within Moniger instead."
)
_OUT_OF_SCOPE_SAFE_EXPLANATION = (
    "This question is about a specific financial product, provider, or asset "
    "class, which is outside Moniger Copilot's scope."
)

# BUDGET_RECOMMEND is different from OUT_OF_SCOPE_ADVICE here: the response
# is a legitimate, correct budgeting answer that just happens to echo back
# a forbidden word (most often because the user's OWN question mentioned
# investing — a question can be classified BUDGET_RECOMMEND rather than
# OUT_OF_SCOPE_ADVICE and still carry that framing; see
# tests/eval/test_set.json eval-28, which the classifier does not route
# consistently). Wholesale-replacing the field like
# _enforce_out_of_scope_guardrail does would throw away real, useful
# figures — so this substitutes just the offending word/phrase in place
# instead, word-boundary-matched so it doesn't clip inside an unrelated
# word. Not perfectly elegant prose afterward, but compliant beats
# eloquent for a non-negotiable guardrail.
_BUDGET_RECOMMEND_TERM_REPLACEMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bmutual funds?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\bfixed deposits?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\btreasury bills?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\bt-bills?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\beurobonds?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\betfs?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\bindex funds?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\bmoney market funds?\b", re.IGNORECASE), "that product"),
    (re.compile(r"\binvestments?\b", re.IGNORECASE), "savings"),
    (re.compile(r"\binvesting\b", re.IGNORECASE), "saving"),
    (re.compile(r"\binvest\b", re.IGNORECASE), "save"),
    (re.compile(r"\bstocks?\b", re.IGNORECASE), "assets"),
    (re.compile(r"\bcrypto(?:currency)?\b", re.IGNORECASE), "that asset class"),
    (re.compile(r"\bpiggyvest\b", re.IGNORECASE), "that provider"),
    (re.compile(r"\bcowrywise\b", re.IGNORECASE), "that provider"),
    (re.compile(r"\brise ?vest\b", re.IGNORECASE), "that provider"),
    (re.compile(r"\bbamboo\b", re.IGNORECASE), "that provider"),
    (re.compile(r"\btrove\b", re.IGNORECASE), "that provider"),
    (re.compile(r"\bchaka\b", re.IGNORECASE), "that provider"),
    (re.compile(r"\bstanbic\b", re.IGNORECASE), "that provider"),
)


def parse(raw_text: str, *, question_type: QuestionCategory, cache_hit: bool) -> ParsedResponse:
    """Extract and validate the JSON object the model was instructed to return.

    Raises ResponseParseError if the output isn't valid JSON or is missing
    a required field — better to surface a 500 than show the user a
    fabricated or garbled answer about their own money. `period`,
    `confidence` and `sources` are treated as soft fields and default rather than fail, since
    losing them doesn't put a wrong figure in front of the user.
    """
    data = _extract_json_object(raw_text)

    answer = _require_str(data, "answer")
    explanation = _require_str(data, "explanation")
    period = data.get("period")
    period = (period.strip() or None) if isinstance(period, str) else None
    confidence = _coerce_confidence(data.get("confidence"))
    sources = _coerce_sources(data.get("sources"))

    if question_type == QuestionCategory.BUDGET_RECOMMEND:
        answer, explanation = _enforce_disclaimer_once(answer, explanation)
        answer, explanation = _enforce_budget_recommend_guardrail(answer, explanation)

    if question_type == QuestionCategory.OUT_OF_SCOPE_ADVICE:
        answer, explanation = _enforce_out_of_scope_guardrail(answer, explanation)

    answer = _ensure_period_in_answer(answer, period)

    return ParsedResponse(
        answer=answer,
        explanation=explanation,
        period=period,
        confidence=confidence,
        sources=sources,
        question_type=question_type.value,
        cache_hit=cache_hit,
    )


# Anything that reads as a time reference: a year, a month name, or a
# relative period word.
_TIME_REFERENCE_RE = re.compile(
    r"\b(?:19|20)\d{2}\b"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(?:today|yesterday|daily|week\w*|month\w*|year\w*|annual\w*|since|between)\b",
    re.IGNORECASE,
)


def _ensure_period_in_answer(answer: str, period: str | None) -> str:
    """A naira amount must never reach the user without its time period.
    If the model gave figures but no time reference, append its own
    `period` to the answer."""
    if not period or "₦" not in answer or _TIME_REFERENCE_RE.search(answer):
        return answer
    # Keep the disclaimer last: the period belongs to the figures.
    body = answer.replace(BUDGET_RECOMMEND_DISCLAIMER, "").rstrip()
    tail = f" {BUDGET_RECOMMEND_DISCLAIMER}" if BUDGET_RECOMMEND_DISCLAIMER in answer else ""
    return f"{body} ({period}){tail}"


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    """Pull the JSON object out of `raw_text`, stripping code fences / prose."""
    text = raw_text.strip()

    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()

    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        raise ResponseParseError("Could not find a JSON object in the model's output.")

    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"Model output was not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ResponseParseError("Model output JSON was not an object.")
    return data


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResponseParseError(f"Model output missing required non-empty string field: {key!r}")
    return value.strip()


def _coerce_confidence(raw: Any) -> float:
    """Validate and clamp the `confidence` field into [0.0, 1.0], defaulting
    to 0.5 if it's missing or not a number."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, value))


def _coerce_sources(raw: Any) -> list[str]:
    """Validate and normalize the `sources` field into a list of strings."""
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if isinstance(item, (str, int, float)) and str(item).strip()]


def _enforce_disclaimer_once(answer: str, explanation: str) -> tuple[str, str]:
    """Guarantee BUDGET_RECOMMEND_DISCLAIMER appears exactly once across
    answer+explanation combined — enforced in code rather than trusted to
    the model, since guardrail integrity here is a non-negotiable metric
    (see app/prompts/budget_system_prompt.py's module docstring)."""
    occurrences = answer.count(BUDGET_RECOMMEND_DISCLAIMER) + explanation.count(BUDGET_RECOMMEND_DISCLAIMER)
    if occurrences == 1:
        return answer, explanation

    answer = answer.replace(BUDGET_RECOMMEND_DISCLAIMER, "").strip()
    explanation = explanation.replace(BUDGET_RECOMMEND_DISCLAIMER, "").strip()
    explanation = f"{explanation} {BUDGET_RECOMMEND_DISCLAIMER}".strip()
    return answer, explanation


def _enforce_out_of_scope_guardrail(answer: str, explanation: str) -> tuple[str, str]:
    """Guarantee an OUT_OF_SCOPE_ADVICE response never repeats a specific
    investment/savings product, provider, or asset-class term — enforced in
    code as a deterministic backstop on top of the prompt-level instruction
    (OUT_OF_SCOPE_DEFLECTION_PROMPT), the same philosophy
    _enforce_disclaimer_once uses for the BUDGET_RECOMMEND disclaimer.
    Prompting alone cannot *guarantee* compliance from a probabilistic
    model, and CLAUDE.md treats a leak here as a failed guardrail, not a
    style nitpick — routing this category to the stronger model tier
    (see router._SONNET_CATEGORIES) reduces how often this needs to fire,
    it doesn't replace the need for it.

    Whichever field(s) contain a forbidden term are replaced wholesale with
    a safe canonical sentence, mirroring _enforce_disclaimer_once's
    wholesale-replacement approach rather than attempting to surgically
    excise a term mid-sentence (which risks leaving broken grammar behind).
    An already-clean field's own wording is left untouched.
    """
    if _contains_forbidden_term(answer):
        answer = _OUT_OF_SCOPE_SAFE_ANSWER
    if _contains_forbidden_term(explanation):
        explanation = _OUT_OF_SCOPE_SAFE_EXPLANATION
    return answer, explanation


def _contains_forbidden_term(text: str) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in _FORBIDDEN_TERMS)


def _enforce_budget_recommend_guardrail(answer: str, explanation: str) -> tuple[str, str]:
    """Guarantee a BUDGET_RECOMMEND response never repeats a specific
    investment/savings product, provider, or asset-class term — the
    BUDGET_RECOMMEND counterpart to _enforce_out_of_scope_guardrail, but
    substituting the offending word/phrase in place (see
    _BUDGET_RECOMMEND_TERM_REPLACEMENTS) rather than replacing the whole
    field: unlike an OUT_OF_SCOPE_ADVICE response, a BUDGET_RECOMMEND
    response is legitimate content the user asked for and is grounded in
    their real numbers — wholesale-discarding it over one leaked word would
    be a worse outcome than a slightly less elegant sentence. Added after
    testing showed the classifier doesn't route every investment-flavored
    hybrid question to OUT_OF_SCOPE_ADVICE consistently (see
    tests/eval/test_set.json eval-28) — when it lands on BUDGET_RECOMMEND
    instead, the model still tends to echo the user's own "investing"
    wording while explaining a correct, in-scope allocation."""
    return _scrub_budget_recommend_terms(answer), _scrub_budget_recommend_terms(explanation)


_DUPLICATE_JOIN_RE = re.compile(
    r"\b(\w[\w ]*?)\s*(?:/|(?:,?\s+or)|(?:,?\s+and))\s*\1\b", re.IGNORECASE
)


def _scrub_budget_recommend_terms(text: str) -> str:
    for pattern, replacement in _BUDGET_RECOMMEND_TERM_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    # A phrase like "savings or investment" / "savings/investments" becomes
    # "savings or savings" / "savings/savings" once the second term is
    # substituted — collapse the resulting duplicate-word join down to one
    # occurrence rather than leaving an odd repetition behind.
    return _DUPLICATE_JOIN_RE.sub(r"\1", text)
