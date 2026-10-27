"""Resolve the calendar months a question names ("June", "May 2025", "last
month") into YYYY-MM strings, deterministically and without a model call.

Used by app.context_builders.comparison so the months compared — and the
breakdown table built from them — match what the user actually asked, rather
than always defaulting to the two most recent months in the data.

Relative references resolve against `anchor` (the most recent month present
in the user's data — the same "now" BASE_SYSTEM_PROMPT tells the model to
use), never the wall clock. A month named without a year resolves to its most
recent occurrence at or before the anchor ("December" in July 2026 is
2025-12).
"""
from __future__ import annotations

import re

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}
_NAMED_RE = re.compile(
    r"\b(" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\b\.?(?:,?\s+(\d{4}))?",
    re.IGNORECASE,
)
_RELATIVE_RE = re.compile(r"\b(this|current|last|previous|past)\s+month\b", re.IGNORECASE)
# "may" is also a verb ("How may I...", "I may be over budget"): skip it when
# the next word reads as a pronoun or verb rather than a year/preposition.
_MAY_VERB_NEXT = re.compile(
    r"\s+(?:i|we|you|they|he|she|it|be|have|need|want|get|know|ask|see|not)\b", re.IGNORECASE
)


def shift_month(month: str, delta: int) -> str:
    """Return YYYY-MM shifted by `delta` calendar months."""
    year, mon = int(month[:4]), int(month[5:7])
    index = year * 12 + (mon - 1) + delta
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def resolve_months(question: str | None, anchor: str | None) -> list[str]:
    """Return the YYYY-MM months `question` refers to, in order of first
    mention, de-duplicated. Empty if none are named or there's no anchor."""
    if not question or not anchor:
        return []
    anchor_year, anchor_month = int(anchor[:4]), int(anchor[5:7])

    found: list[tuple[int, str]] = []
    for match in _NAMED_RE.finditer(question):
        word = match.group(1)
        if word.lower() == "may" and not match.group(2) and _MAY_VERB_NEXT.match(question, match.end(1)):
            continue
        month = _MONTHS[word.lower()]
        if match.group(2):
            year = int(match.group(2))
        else:
            year = anchor_year if month <= anchor_month else anchor_year - 1
        found.append((match.start(), f"{year:04d}-{month:02d}"))
    for match in _RELATIVE_RE.finditer(question):
        offset = 0 if match.group(1).lower() in ("this", "current") else -1
        found.append((match.start(), shift_month(anchor, offset)))

    months: list[str] = []
    for _, month in sorted(found):
        if month not in months:
            months.append(month)
    return months
