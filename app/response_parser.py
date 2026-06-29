"""Parses the LLM's raw text response into the structured shape the API
returns to clients, tolerating the common ways a model deviates from "raw
JSON only" (markdown code fences, leading/trailing prose).
"""
from __future__ import annotations

import json
import re
from typing import Any, TypedDict

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class ParsedResponse(TypedDict):
    answer: str
    key_figures: list[dict[str, str]]
    follow_up_suggestions: list[str]


class ResponseParseError(Exception):
    """Raised when the model's output cannot be coerced into ParsedResponse."""


def parse(raw_text: str) -> ParsedResponse:
    """Extract and validate the JSON object the model was instructed to return."""
    candidate = _extract_json_object(raw_text)

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise ResponseParseError(f"Model response was not valid JSON: {exc}") from exc

    if not isinstance(data, dict) or "answer" not in data:
        raise ResponseParseError("Model response JSON is missing the required 'answer' field")

    return ParsedResponse(
        answer=str(data["answer"]),
        key_figures=_coerce_key_figures(data.get("key_figures", [])),
        follow_up_suggestions=[str(s) for s in data.get("follow_up_suggestions", []) if s],
    )


def _extract_json_object(raw_text: str) -> str:
    text = raw_text.strip()

    fence_match = _CODE_FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()

    if text.startswith("{") and text.endswith("}"):
        return text

    # Last resort: grab the first {...} span in case the model added prose
    # before or after the JSON despite instructions not to.
    object_match = _JSON_OBJECT_RE.search(text)
    if object_match:
        return object_match.group(0)

    return text


def _coerce_key_figures(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []

    figures: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, dict) and "label" in item and "value" in item:
            figures.append({"label": str(item["label"]), "value": str(item["value"])})

    return figures
