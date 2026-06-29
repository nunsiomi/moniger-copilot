"""PII stripping pipeline.

Strips personally-identifiable and financially-sensitive data out of free
text before it is sent to the LLM, and can restore the original values into
the LLM's response afterwards (so the user still sees their real numbers).

Order matters: more specific patterns (card numbers, BVN/NIN) must run
before the generic long-digit-run pattern, otherwise the generic pattern
would mangle them first and the specific ones would never match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Nigerian mobile numbers are exactly 11 digits as "0" + 10 digits, or "+234"
# + 10 digits. Kept exact (not a range) so it doesn't overlap with the
# 10-digit NUBAN account pattern below.
_PHONE_RE = re.compile(r"\b(?:\+234\d{10}|0\d{10})\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_NUBAN_RE = re.compile(r"\b\d{10}\b")  # Nigerian bank account number
_BVN_NIN_RE = re.compile(r"\b\d{11}\b")  # BVN / NIN are both 11 digits
_GENERIC_DIGIT_RUN_RE = re.compile(r"\b\d{6,}\b")

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", _EMAIL_RE),
    ("PHONE", _PHONE_RE),
    ("CARD", _CARD_RE),
    ("BVN_NIN", _BVN_NIN_RE),
    ("ACCOUNT", _NUBAN_RE),
    ("NUMBER", _GENERIC_DIGIT_RUN_RE),
)


class _PlaceholderAllocator:
    """Assigns stable placeholders to matched PII values across one or more texts."""

    def __init__(self) -> None:
        self.mapping: dict[str, str] = {}
        self._value_to_placeholder: dict[str, str] = {}
        self._counters: dict[str, int] = {}

    def substitute(self, text: str) -> str:
        for label, pattern in _PATTERNS:
            text = pattern.sub(self._make_replacer(label), text)
        return text

    def _make_replacer(self, label: str):
        def _sub(match: re.Match[str]) -> str:
            value = match.group(0)
            if value in self._value_to_placeholder:
                return self._value_to_placeholder[value]

            self._counters[label] = self._counters.get(label, 0) + 1
            placeholder = f"[{label}_{self._counters[label]}]"
            self._value_to_placeholder[value] = placeholder
            self.mapping[placeholder] = value
            return placeholder

        return _sub


@dataclass
class AnonymisationResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)

    def restore(self, text: str) -> str:
        """Swap placeholder tokens in `text` back to their original values."""
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


@dataclass
class BatchAnonymisationResult:
    texts: list[str]
    mapping: dict[str, str] = field(default_factory=dict)

    def restore(self, text: str) -> str:
        for placeholder, original in self.mapping.items():
            text = text.replace(placeholder, original)
        return text


def anonymise(text: str) -> AnonymisationResult:
    """Replace PII substrings in `text` with stable, typed placeholders.

    Each distinct matched value gets its own placeholder (e.g. `[CARD_1]`),
    so repeated mentions of the same number map to the same token and the
    LLM can still reason about "the same account" across the conversation.
    """
    allocator = _PlaceholderAllocator()
    return AnonymisationResult(text=allocator.substitute(text), mapping=allocator.mapping)


def anonymise_many(texts: list[str]) -> BatchAnonymisationResult:
    """Anonymise several strings together, sharing one placeholder mapping.

    Useful for anonymising a question and its supporting context (e.g.
    transaction narrations) in one pass so the same account number always
    maps to the same placeholder across both.
    """
    allocator = _PlaceholderAllocator()
    anonymised_texts = [allocator.substitute(text) for text in texts]
    return BatchAnonymisationResult(texts=anonymised_texts, mapping=allocator.mapping)
