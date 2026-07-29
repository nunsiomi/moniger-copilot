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
    confidence: float
    sources: list[str]
    question_type: str
    cache_hit: bool


class ResponseParseError(Exception):
    """Raised when the model's output cannot be coerced into ParsedResponse."""


def parse(raw_text: str, *, question_type: QuestionCategory, cache_hit: bool) -> ParsedResponse:
    """Extract and validate the JSON object the model was instructed to return.

    Raises ResponseParseError if the output isn't valid JSON or is missing
    a required field — better to surface a 500 than show the user a
    fabricated or garbled answer about their own money. `confidence` and
    `sources` are treated as soft fields and default rather than fail, since
    losing them doesn't put a wrong figure in front of the user.
    """
    data = _extract_json_object(raw_text)

    answer = _require_str(data, "answer")
    explanation = _require_str(data, "explanation")
    confidence = _coerce_confidence(data.get("confidence"))
    sources = _coerce_sources(data.get("sources"))

    if question_type == QuestionCategory.BUDGET_RECOMMEND:
        answer, explanation = _enforce_disclaimer_once(answer, explanation)

    return ParsedResponse(
        answer=answer,
        explanation=explanation,
        confidence=confidence,
        sources=sources,
        question_type=question_type.value,
        cache_hit=cache_hit,
    )


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
