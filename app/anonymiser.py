"""PII stripping pipeline.

Two independent concerns live in this module:

1. Free-text question anonymisation (`anonymise` / `anonymise_many`) —
   strips emails, phone numbers, card numbers, NUBAN account numbers, and
   BVN/NIN numbers out of whatever the user actually typed, replacing each
   distinct value with a stable `[TYPE_n]` placeholder, and can restore the
   original values into the LLM's response afterwards (so the user still
   sees their real numbers). See docs/Moniger_Copilot_Engineering.docx
   ("PII handling"). Used by app/main.py on `payload.question`.

2. Raw transaction record anonymisation (`anonymise_transactions`) — a
   deterministic backstop applied to every record app/data_access.py reads
   from the Mongo mirror (moniger_copilot_fixtures_v2's schema), before any
   context builder or LLM call sees it. The mirror is contractually supposed
   to arrive already PII-free (populating it is backend-owned — see
   CLAUDE.md's "Scope" note) but this module treats that as a contract to
   verify, not a guarantee to trust blindly: `copilot_transactions_POISONED.jsonl`
   ships 8 cases of exactly the kind of upstream failure this guards
   against (an un-tokenised name in `merchantLabel`, a raw account/BVN
   number, an email, a raw ObjectId used as `userToken`, a `narration` or
   `paymentDetails` subtree that should never have reached the mirror at
   all). This is a strict allowlist, not a restore-able placeholder scheme:
   only explicitly safe fields survive, `merchantLabel` is scrubbed (never
   passed through unconditionally), a `userToken` that doesn't look like a
   genuine anonymised token drops the whole record, and there is no
   `restore` step, since raw transaction PII is never reconstructed from a
   model response the way a question's PII is.
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
#
# Schema: moniger_copilot_fixtures_v2 (the Mongo mirror app.data_access
# reads from). Allowlist, not blocklist, same rationale as the free-text
# side: only the fields named below survive; anything else — a stray
# `narration`, `bvn`, `contact`, or `paymentDetails` subtree that should
# never have reached the mirror in the first place — is dropped by
# construction, whether or not anyone thought to list it as "a PII field."
_RETAIN_FIELDS: tuple[str, ...] = (
    "date",
    "amountKobo",
    "direction",
    "status",
    "categoryId",
    "categoryName",
    "fiCategory",
    "fiCategoryCode",
    "fiSubcategory",
    "fiConfidence",
    "fiClassificationStage",
    "fiIsRecurring",
    "channel",
    "moneySource",
    "isBillPayment",
    "isWalletToWallet",
    "isExternalTransfer",
)

# These are controlled-vocabulary taxonomy strings (category/subcategory
# names, bank names), not expected to ever carry PII — but scrubbed anyway
# since it's nearly free and this module's whole premise is not trusting
# the mirror blindly. `merchantLabel` gets its own richer treatment (see
# _sanitize_merchant_label) since it's the one retained field with real,
# unconstrained free-text risk.
_TEXT_FIELDS_REQUIRING_SCRUB: tuple[str, ...] = ("channel", "categoryName", "fiCategory", "fiSubcategory")

# Documented for traceability with the spec this module implements — these
# are guaranteed absent from the output purely by virtue of not being in
# _RETAIN_FIELDS above; nothing in this module deletes them explicitly.
# copilot_transactions_POISONED.jsonl exercises exactly these: POISON-03
# (`bvn`), POISON-05 (`contact`), POISON-06 (`narration`), POISON-08
# (`paymentDetails`).
_PII_FIELDS_REMOVED_BY_ALLOWLIST = (
    "narration",
    "contact",
    "bvn",
    "nin",
    "paymentDetails",
    "paymentDetails.accountName",
    "paymentDetails.accountNumber",
    "beneficiaryAccountName",
    "beneficiaryAccountNumber",
    "beneficiaryBankVerificationNumber",
    "originatorAccountName",
    "originatorAccountNumber",
    "originatorBankVerificationNumber",
)

# A genuine anonymised userToken looks like "usr_anon_t01wbl" — never a raw
# 24-hex-char Mongo ObjectId. POISON-04 ships exactly that (a raw ObjectId
# as userToken); reject the whole record rather than let an internal
# database identifier travel downstream.
_ANON_TOKEN_RE = re.compile(r"^usr_[a-z0-9_]+$", re.IGNORECASE)
_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$", re.IGNORECASE)


def _looks_like_anon_token(user_token: Any) -> bool:
    if not isinstance(user_token, str):
        return False
    if _OBJECT_ID_RE.match(user_token):
        return False
    return bool(_ANON_TOKEN_RE.match(user_token))


def _strip_pii_patterns(text: str) -> str:
    """Redact email/phone/card/BVN/NUBAN-shaped substrings from `text`.

    Stateless and non-reversible — unlike the free-text question
    anonymiser, there is no restore step for transaction data (see module
    docstring), so a generic marker is enough; nothing downstream needs the
    original value back.
    """
    for _, pattern in _PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


# Words that commonly appear capitalized in Nigerian bank/wallet narrations
# and merchant labels but are never themselves part of a person's name —
# transaction verbs, channels, providers, months, and the trigger words
# below. Not exhaustive; this is a best-effort heuristic, not a name
# database.
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
        "OF", "ON", "IN", "A", "AN", "CREDIT", "AIRTIME",
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
    labels like "CHICKEN REPUBLIC" (no direction keyword) are left alone.
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


def _sanitize_merchant_label(txn: dict[str, Any], allocator: _PersonTokenAllocator) -> str | None:
    """Return a safe `merchantLabel` for `txn`, or None if it had none.

    Applies the same TO/FRM/FROM name-span tokenisation used for narration
    (catches a leaked name immediately following a transfer-direction
    keyword, e.g. POISON-06's narration pattern if it ever showed up in a
    label instead), then strips any residual PII-shaped digit run
    (POISON-02, POISON-07).

    Deliberately does NOT attempt to flag a *bare* label — one with no
    digits and no TO/FRM/FROM trigger — as a possible leaked name (e.g.
    POISON-01's "ADEBAYO OKONKWO"): a heuristic aggressive enough to catch
    that is, by construction, aggressive enough to also catch real
    merchant/place names built the same way ("FESTAC MARKET AREA",
    "CHICKEN REPUBLIC DELIVERY", "SUPER EAGLES", "LAGOS BRT" all measured as
    false positives against this fixture's own clean data during
    development — hundreds of distinct labels, thousands of transactions).
    Structurally, a two-to-three-word Title/Upper-case label is not
    reliably distinguishable from a person's name without a real merchant
    taxonomy lookup (backend-owned, out of scope here — see CLAUDE.md).
    Mangling real merchant answering for every user to catch one synthetic
    poison case is the wrong trade; this is a known, accepted gap, not an
    oversight.
    """
    raw = txn.get("merchantLabel")
    if not isinstance(raw, str) or not raw.strip():
        return None
    label = raw.strip()

    return _strip_pii_patterns(_tokenise_names(label, allocator))


def _sanitize_transaction(txn: dict[str, Any], allocator: _PersonTokenAllocator) -> dict[str, Any] | None:
    """Build a new transaction dict containing only the allowlisted fields
    (scrubbed where relevant) plus a sanitized `merchantLabel`.

    Returns None if `userToken` doesn't look like a genuine anonymised
    token (see _looks_like_anon_token) — the whole record is dropped rather
    than letting a raw internal id travel downstream (POISON-04). Never
    mutates `txn`.
    """
    if not _looks_like_anon_token(txn.get("userToken")):
        return None

    sanitized: dict[str, Any] = {"userToken": txn["userToken"]}
    for field_name in _RETAIN_FIELDS:
        if field_name not in txn:
            continue
        value = txn[field_name]
        if field_name in _TEXT_FIELDS_REQUIRING_SCRUB and isinstance(value, str):
            value = _strip_pii_patterns(value)
        sanitized[field_name] = value

    merchant_label = _sanitize_merchant_label(txn, allocator)
    if merchant_label is not None:
        sanitized["merchantLabel"] = merchant_label

    return sanitized


def anonymise_transaction(txn: dict[str, Any]) -> dict[str, Any] | None:
    """Sanitize a single raw transaction record with its own fresh name allocator.

    Prefer `anonymise_transactions` when processing more than one record so
    the same counterparty name maps to the same PERSON_x token throughout
    the batch. Returns None if the record was rejected outright (see
    _sanitize_transaction). See the module docstring for the allowlist/
    tokenisation design.
    """
    return _sanitize_transaction(txn, _PersonTokenAllocator())


def anonymise_transactions(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip PII from a list of raw wallet transaction records before any
    LLM call or context-building step touches them.

    Every account/BVN/name identity field is removed by construction (see
    _RETAIN_FIELDS — this is an allowlist, so an unlisted or newly-added
    field can never leak through by accident), `merchantLabel` is scrubbed
    rather than trusted verbatim, and a record whose `userToken` doesn't
    look like a genuine anonymised token is dropped outright. Nigerian name
    patterns detected in `merchantLabel` are tokenised into sequential
    PERSON_A, PERSON_B, ... placeholders shared across the whole batch. The
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
            result = _sanitize_transaction(txn, allocator)
        except Exception:
            logger.warning("Skipping malformed transaction at index %d.", index, exc_info=True)
            continue
        if result is None:
            logger.warning("Dropping transaction at index %d: userToken failed shape validation.", index)
            continue
        sanitized.append(result)

    return sanitized
