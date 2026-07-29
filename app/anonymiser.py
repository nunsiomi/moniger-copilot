"""PII stripping pipeline.

Two independent concerns live in this module:

1. Free-text question anonymisation (`anonymise` / `anonymise_many`) —
   strips emails, phone numbers, card numbers, NUBAN account numbers, and
   BVN/NIN numbers out of whatever the user actually typed, replacing each
   distinct value with a stable `[TYPE_n]` placeholder, and can restore the
   original values into the LLM's response afterwards (so the user still
   sees their real numbers). See docs/Moniger_Copilot_Engineering.docx
   ("PII handling"). Used by app/main.py on `payload.question`.

2. Raw transaction record anonymisation (`anonymise_transactions`) — strips
   PII out of wallet transaction objects (a richer, nested schema than the
   flat one context_builders reads: NIP transfer identity fields nested
   under `paymentDetails`, plus a `fi` categorization block) before any of
   it is used to build LLM context. This is a strict allowlist, not a
   restore-able placeholder scheme: only explicitly safe fields survive,
   narration text is tokenised (never restored), and there is no `restore`
   step, since raw transaction PII is never reconstructed from a model
   response the way a question's PII is.
"""
from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Free-text question anonymisation
# ---------------------------------------------------------------------------

# Order matters: more specific patterns (card numbers, BVN/NIN) must run
# before the generic long-digit-run pattern, otherwise the generic pattern
# would mangle them first and the specific ones would never match.
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# Nigerian mobile numbers are exactly 11 digits as "0" + 10 digits, or "+234"
# + 10 digits. Kept exact (not a range) so it doesn't overlap with the
# 10-digit NUBAN account pattern below.
_PHONE_RE = re.compile(r"\b(?:\+234\d{10}|0\d{10})\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_BVN_NIN_RE = re.compile(r"\b\d{11}\b")  # BVN / NIN are both 11 digits
_NUBAN_RE = re.compile(r"\b\d{10}\b")  # Nigerian bank account number
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
        """Replace every PII match in `text` with a stable placeholder token."""
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
        """Swap placeholder tokens in `text` back to their original values."""
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


# ---------------------------------------------------------------------------
# 2. Raw transaction record anonymisation
# ---------------------------------------------------------------------------

# Allowlist: only these dotted paths survive into the sanitized output.
# Everything else — including every field named in the PII removal list
# below, and any field not mentioned at all — is dropped by construction.
# An allowlist is used deliberately instead of a delete-these-fields
# blocklist: a blocklist can miss a new/renamed PII field; an allowlist
# cannot, since nothing gets through unless it's explicitly named here.
_RETAIN_PATHS: tuple[tuple[str, ...], ...] = (
    ("amount",),
    ("createdAt",),
    ("transactionType",),
    ("channel",),
    ("isBillPayment",),
    ("isExternalBankTransfer",),
    ("fi", "category"),
    ("fi", "subcategory"),
    ("fi", "is_recurring"),
    ("fi", "confidence_score"),
    ("paymentDetails", "currency"),
    ("paymentDetails", "charges"),
)

# Narration fields are retained but transformed (name-tokenised), never
# passed through verbatim and never simply dropped.
_NARRATION_PATHS: tuple[tuple[str, ...], ...] = (
    ("narration",),
    ("paymentDetails", "narration"),
)

# Documented for traceability with the spec this module implements — these
# are guaranteed absent from the output purely by virtue of not being in
# _RETAIN_PATHS above; nothing in this module deletes them explicitly.
_PII_FIELDS_REMOVED_BY_ALLOWLIST = (
    "paymentDetails.accountName",
    "accountNumber",
    "BankVerificationCode",
    "beneficiaryAccountName",
    "beneficiaryAccountNumber",
    "beneficiaryBankVerificationNumber",
    "originatorAccountName",
    "originatorAccountNumber",
    "originatorBankVerificationNumber",
    "paymentDetails.metadata.accountNumber",
    "paymentDetails.metadata.nameEnquiryRef",
)

_MISSING = object()


def _get_path(obj: Any, path: tuple[str, ...]) -> Any:
    """Navigate a dotted `path` through nested dicts, returning _MISSING
    instead of raising if any intermediate key is absent or not a dict.
    """
    current = obj
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return _MISSING
        current = current[key]
    return current


def _set_path(obj: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    """Set `value` at a dotted `path` in `obj`, creating intermediate dicts as needed."""
    current = obj
    for key in path[:-1]:
        current = current.setdefault(key, {})
    current[path[-1]] = value


# Words that commonly appear capitalized in Nigerian bank/wallet narrations
# but are never themselves part of a person's name — transaction verbs,
# channels, providers, months, and the trigger words below. Not
# exhaustive; this is a best-effort heuristic, not a name database.
_NARRATION_STOPWORDS: frozenset[str] = frozenset(
    word.upper()
    for word in (
        "NIP", "TRF", "TRANSFER", "PAYMENT", "POS", "PURCHASE", "ORDER",
        "SUBSCRIPTION", "RENEWAL", "BUNDLE", "RECHARGE", "TOKEN", "PREMIUM",
        "BILL", "BILLS", "DATA", "EVENT", "CINEMA", "GYM", "SALON",
        "PHARMACY", "HMO", "ESTATE", "RENT", "UPKEEP", "FEES", "FEE",
        "SALARY", "PAYROLL", "LOAN", "BACK", "DESIGN", "LOGO", "WEBSITE",
        "UPDATE", "REFRESH", "BRAND", "SCHOOL", "HELP", "TRIP", "CHARGE",
        "INVOICE", "PROJECT", "CONSULTING", "COMPANY", "ORGANIZATION",
        "GROUP", "ENTERPRISE", "ENTERPRISES", "LTD", "LIMITED", "INC",
        "INCORPORATED", "CORP", "INTERNATIONAL", "NIGERIA", "NG", "EU",
        "COM", "WWW", "HTTP", "HTTPS", "FOR", "THE", "AND", "WITH", "AT",
        "OF", "ON", "IN", "A", "AN",
        "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP",
        "OCT", "NOV", "DEC",
    )
)

_TRIGGER_WORDS = frozenset({"TO", "FRM", "FROM"})
_MAX_NAME_TOKENS = 4


def _is_name_candidate(token: str) -> bool:
    cleaned = token.strip(".,:;-")
    if not cleaned.isalpha():
        return False
    if cleaned.upper() in _NARRATION_STOPWORDS:
        return False
    return cleaned.isupper() or cleaned.istitle()


def _find_name_spans(tokens: list[str]) -> list[tuple[int, int]]:
    """Return (start, end) token-index spans of likely person names.

    A span is triggered by a TO/FRM/FROM keyword — the direction word real
    NIP transfer narrations use immediately before naming the counterparty
    — followed by 1-4 consecutive capitalized, non-stopword tokens. This is
    deliberately narrower than "any two capitalized words" so merchant
    narrations like "POS PURCHASE CHICKEN REPUBLIC" (no direction keyword)
    are left alone.
    """
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(tokens):
        cleaned = tokens[i].strip(".,:;-").upper()
        if cleaned in _TRIGGER_WORDS:
            j = i + 1
            count = 0
            while j < len(tokens) and count < _MAX_NAME_TOKENS and _is_name_candidate(tokens[j]):
                j += 1
                count += 1
            if count > 0:
                spans.append((i + 1, j))
                i = j
                continue
        i += 1
    return spans


class _PersonTokenAllocator:
    """Assigns sequential PERSON_A, PERSON_B, ... tokens to detected names.

    Local to one anonymise_transactions() call and discarded when it
    returns — there is no restore step for transaction data.
    """

    def __init__(self) -> None:
        self._name_to_token: dict[str, str] = {}

    def token_for(self, name_phrase: str) -> str:
        key = name_phrase.upper()
        if key not in self._name_to_token:
            self._name_to_token[key] = f"PERSON_{_sequence_label(len(self._name_to_token))}"
        return self._name_to_token[key]


def _sequence_label(index: int) -> str:
    """0 -> 'A', 1 -> 'B', ..., 25 -> 'Z', 26 -> 'AA', 27 -> 'AB', ... ."""
    letters = string.ascii_uppercase
    label = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        label = letters[remainder] + label
    return label


def _tokenise_names(text: str, allocator: _PersonTokenAllocator) -> str:
    """Replace detected name spans in `text` with sequential PERSON_x tokens."""
    tokens = text.split()
    spans = _find_name_spans(tokens)
    if not spans:
        return text

    out: list[str] = []
    span_index = 0
    i = 0
    while i < len(tokens):
        if span_index < len(spans) and i == spans[span_index][0]:
            start, end = spans[span_index]
            name_phrase = " ".join(tokens[start:end])
            out.append(allocator.token_for(name_phrase))
            i = end
            span_index += 1
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def _sanitize_transaction(txn: dict[str, Any], allocator: _PersonTokenAllocator) -> dict[str, Any]:
    """Build a new transaction dict containing only the allowlisted fields
    (unchanged) plus tokenised narration fields. Never mutates `txn`.
    """
    sanitized: dict[str, Any] = {}

    for path in _RETAIN_PATHS:
        value = _get_path(txn, path)
        if value is not _MISSING:
            _set_path(sanitized, path, value)

    for path in _NARRATION_PATHS:
        value = _get_path(txn, path)
        if isinstance(value, str):
            _set_path(sanitized, path, _tokenise_names(value, allocator))

    return sanitized


def anonymise_transaction(txn: dict[str, Any]) -> dict[str, Any]:
    """Sanitize a single raw transaction record with its own fresh name allocator.

    Prefer `anonymise_transactions` when processing more than one record so
    the same counterparty name maps to the same PERSON_x token throughout
    the batch. See the module docstring for the allowlist/tokenisation
    design.
    """
    return _sanitize_transaction(txn, _PersonTokenAllocator())


def anonymise_transactions(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip PII from a list of raw wallet transaction records before any
    LLM call or context-building step touches them.

    Every account/BVN/name identity field is removed by construction (see
    _RETAIN_PATHS — this is an allowlist, so an unlisted or newly-added
    field can never leak through by accident), and Nigerian name patterns
    detected in narration text are tokenised into sequential PERSON_A,
    PERSON_B, ... placeholders shared across the whole batch. The
    name -> token mapping lives only for the duration of this call; it is
    never returned or persisted, and there is no restore step.

    Malformed entries (not an object, or anything else that raises while
    being processed) are logged and skipped rather than raising, so one
    bad record never fails the whole batch.
    """
    allocator = _PersonTokenAllocator()
    sanitized: list[dict[str, Any]] = []

    for index, txn in enumerate(transactions):
        if not isinstance(txn, dict):
            logger.warning("Skipping malformed transaction at index %d: not an object.", index)
            continue
        try:
            sanitized.append(_sanitize_transaction(txn, allocator))
        except Exception:
            logger.warning("Skipping malformed transaction at index %d.", index, exc_info=True)
            continue

    return sanitized
