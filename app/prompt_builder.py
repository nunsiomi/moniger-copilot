"""Assembles the system prompt and message list sent to Groq.

Keeps the actual Groq SDK call out of this module — prompt_builder
only knows how to turn (category, anonymised question, context) into the
`system` string and `messages` list that router.call_model expects. Token
counting is therefore a rough ~4-chars-per-token estimate rather than a
real tokenizer call: an approximation is fine for a truncation safety net,
and it keeps this module free of any network dependency.
"""
from __future__ import annotations

import json
import os
from typing import Any

from app import router
from app.classifier import QuestionCategory
from app.prompts.base_system_prompt import BASE_SYSTEM_PROMPT
from app.prompts.budget_system_prompt import (
    BUDGET_RECOMMEND_SYSTEM_PROMPT,
    BUDGET_TRACK_SYSTEM_PROMPT,
    OUT_OF_SCOPE_DEFLECTION_PROMPT,
)
from app.prompts.tax_system_prompt import TAX_SYSTEM_PROMPT

_CATEGORY_ADDENDA: dict[QuestionCategory, str] = {
    QuestionCategory.TAX: TAX_SYSTEM_PROMPT,
    QuestionCategory.BUDGET_TRACK: BUDGET_TRACK_SYSTEM_PROMPT,
    QuestionCategory.BUDGET_RECOMMEND: BUDGET_RECOMMEND_SYSTEM_PROMPT,
    QuestionCategory.OUT_OF_SCOPE_ADVICE: OUT_OF_SCOPE_DEFLECTION_PROMPT,
}

# Context token budget, enforced in code rather than left to the model to
# self-limit.
#
# Reverted 2026-09-09 to 4000 (briefly raised to 48,000 on 2026-09-01 to
# guarantee full-history correctness for SPENDING/MERCHANT/BUDGET_TRACK --
# see git history if resurrecting that attempt). That raise was wrong, not
# just expensive: Groq's on-demand tier enforces an 8,000-tokens-PER-MINUTE
# hard ceiling *per request*, independent of and separate from the
# tokens-per-day budget discussed when the cap was first raised. A request
# anywhere near 28,000 tokens (what a rich, long-history persona's full
# spending context actually costs -- confirmed live:
# `groq.APIStatusError: 413 ... tokens per minute (TPM): Limit 8000,
# Requested 28405`) is rejected outright on this tier, every single time,
# no matter how much daily quota remains -- this is a hard capacity wall,
# not a cost/latency tradeoff to weigh. 4000 (plus the ~635-1271 token
# system prompt, well within the 8,000 TPM ceiling) is the largest cap
# confirmed to actually work against this tier. The consequence stands as
# a known, documented limitation rather than a bug: a question needing
# data outside the 2 most recent months a rich persona's trimmed context
# carries (see _CHRONOLOGICAL_LIST_FIELDS below) cannot be answered
# correctly on this tier. Fixing that for real needs either a paid Groq
# tier with a materially higher TPM limit, or a smarter, query-aware
# context strategy instead of "send everything" -- not a bigger constant
# here.
#
# Sonnet's categories (TAX, COMPARISON, BUDGET_RECOMMEND, OUT_OF_SCOPE_ADVICE)
# consume already-aggregated figures (tax bands, a two-month comparison, a
# handful of income/spend summary numbers) rather than raw per-month
# history -- measured at under 1,000 tokens across every persona in the
# fixtures, nowhere near either this cap or gpt-oss-120b's own TPM limit.
#
# Both caps are env-overridable so a paid Groq tier (higher TPM) can send
# fuller context without a code change.
_HAIKU_CONTEXT_TOKEN_CAP = int(os.environ.get("HAIKU_CONTEXT_TOKEN_CAP", "4000"))
_SONNET_CONTEXT_TOKEN_CAP = int(os.environ.get("SONNET_CONTEXT_TOKEN_CAP", "8000"))
_CHARS_PER_TOKEN_ESTIMATE = 4


def build_system_prompt(category: QuestionCategory) -> str:
    """Return the base prompt, plus a category addendum if one exists.

    See app/prompts/base_system_prompt.py, app/prompts/tax_system_prompt.py,
    and app/prompts/budget_system_prompt.py for the prompt content this
    should assemble.
    """
    addendum = _CATEGORY_ADDENDA.get(category)
    if addendum is None:
        return BASE_SYSTEM_PROMPT
    return f"{BASE_SYSTEM_PROMPT}\n\n{addendum}"


def build_messages(category: QuestionCategory, anonymised_question: str, context: dict[str, Any]) -> list[dict[str, str]]:
    """Return the `messages` array: the question plus its supporting context.

    Context is trimmed to fit the token cap for whichever model
    router.select_model would pick for `category`, before being embedded
    in the user turn.
    """
    token_cap = _SONNET_CONTEXT_TOKEN_CAP if router.select_model(category) == router.SONNET_MODEL_ID else _HAIKU_CONTEXT_TOKEN_CAP
    bounded_context = _within_token_budget(context, token_cap)

    content = (
        f"<context>\n{_dumps(bounded_context)}\n</context>\n\n"
        f"<question>\n{anonymised_question}\n</question>"
    )
    return [{"role": "user", "content": content}]


def _dumps(value: Any) -> str:
    # Compact separators: indentation alone cost ~35% of every context's
    # tokens, and the model reads compact JSON just as well.
    return json.dumps(value, separators=(",", ":"), default=str)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN_ESTIMATE)


_MAX_TRUNCATION_PASSES = 5

# Fields that are chronologically ordered oldest-first (see e.g.
# app.context_builders.spending.build's monthly_breakdown, .comparison's
# monthly_totals) -- for these, the entries a real question is actually
# about ("last month", a specific recent month, "this year") sit at the
# END of the list, not the start, so trimming must keep the tail. Every
# other list field (category_breakdown, merchant_breakdown, budgets, etc.)
# is already sorted by descending relevance (biggest total first), so the
# default head-slice is correct there. Getting this backwards silently
# drops the exact data most questions need while keeping the oldest,
# least-relevant history -- verified against real 12-14 month personas,
# where a full monthly_breakdown badly overshoots the token cap (a rich
# persona's raw context estimates at 5-7x the Haiku cap) and trimming
# reliably engages on nearly every request.
_CHRONOLOGICAL_LIST_FIELDS = frozenset({"monthly_breakdown", "monthly_totals"})

# Per-month subcategory detail is the bulk of a rich persona's spending
# context. It is shed (except for the most recent months) before any whole
# list is trimmed, so every month's category totals survive and an
# all-time question ("how much on food?") still sees every category.
_NESTED_DETAIL_FIELD = "subcategory_breakdown"
_RECENT_MONTHS_WITH_DETAIL = 2
_RECENT_SUBCATEGORY_LIMIT = 15


def _within_token_budget(context: dict[str, Any], max_tokens: int) -> dict[str, Any]:
    """Trim `context`'s largest list field(s) until its serialized size
    fits `max_tokens`, marking the result if anything was dropped.

    Generic on purpose — every context builder puts its "many items" data
    in a list-valued field (category_breakdown, merchant_breakdown,
    budgets, etc.), so shrinking the largest list repeatedly works
    uniformly across categories without category-specific logic here,
    except for the ordering direction (see _CHRONOLOGICAL_LIST_FIELDS).
    Each pass estimates how many items would fit and slices straight to
    that count, rather than dropping one item at a time -- with a
    pathologically large list, removing a single item per pass would mean
    a pass count proportional to the list's length, so this instead
    converges in a small, fixed number of passes regardless of input size.
    """
    trimmed = context
    if _estimate_tokens(_dumps(trimmed)) > max_tokens:
        trimmed = _shed_nested_detail(trimmed)
    for _ in range(_MAX_TRUNCATION_PASSES):
        current_tokens = _estimate_tokens(_dumps(trimmed))
        if current_tokens <= max_tokens:
            break

        # A chronological field trimmed to a single month keeps only the
        # incomplete current month (the tail's last entry) and silently
        # drops the previous COMPLETE month -- exactly the one a "last
        # month" question needs. Floor at 2 for these fields so both
        # survive, even at the cost of a small overshoot past max_tokens;
        # a slightly oversized prompt beats a confidently wrong answer.
        # A field already at its floor is skipped, not a reason to stop.
        list_field = _largest_list_field(trimmed)
        if list_field is None:
            break

        items = trimmed[list_field]
        min_count = _min_items(list_field)

        tokens_without_list = _estimate_tokens(_dumps({**trimmed, list_field: []}))
        budget_for_list = max(0, max_tokens - tokens_without_list)
        tokens_per_item = max(1, (current_tokens - tokens_without_list) / len(items))
        target_count = max(min_count, min(len(items) - 1, int(budget_for_list / tokens_per_item)))

        kept_items = items[-target_count:] if list_field in _CHRONOLOGICAL_LIST_FIELDS else items[:target_count]
        trimmed = {**trimmed, list_field: kept_items, "_context_truncated": True}

    return trimmed


def _shed_nested_detail(context: dict[str, Any]) -> dict[str, Any]:
    """Compact the per-month entries of each chronological field: older
    months keep only a {category name: total} map, the most recent months
    keep their categories plus their largest subcategories. Returns
    `context` unchanged if nothing was compacted."""
    shed = dict(context)
    changed = False
    for field in _CHRONOLOGICAL_LIST_FIELDS & context.keys():
        entries = context[field]
        if not isinstance(entries, list):
            continue
        cutoff = len(entries) - _RECENT_MONTHS_WITH_DETAIL
        new_entries = []
        for index, entry in enumerate(entries):
            if isinstance(entry, dict):
                compacted = _compact_month(entry, recent=index >= cutoff)
                changed = changed or compacted != entry
                entry = compacted
            new_entries.append(entry)
        shed[field] = new_entries
    if changed:
        shed["_context_truncated"] = True
        return shed
    return context


def _compact_month(entry: dict[str, Any], *, recent: bool) -> dict[str, Any]:
    details = entry.get(_NESTED_DETAIL_FIELD)
    if recent:
        if isinstance(details, list) and len(details) > _RECENT_SUBCATEGORY_LIMIT:
            return {**entry, _NESTED_DETAIL_FIELD: details[:_RECENT_SUBCATEGORY_LIMIT]}
        return entry
    compact = {k: v for k, v in entry.items() if k != _NESTED_DETAIL_FIELD}
    categories = entry.get("category_breakdown")
    if isinstance(categories, list) and all(
        isinstance(c, dict) and "total_naira" in c for c in categories
    ):
        compact["category_breakdown"] = {
            c.get("category_name") or c.get("category_code"): c["total_naira"] for c in categories
        }
    return compact


def _largest_list_field(context: dict[str, Any]) -> str | None:
    """The list field taking the most space once serialized. Item count is
    the wrong measure: 16 small category rows must not be trimmed ahead of
    14 month entries that each carry nested breakdowns."""
    list_fields = [
        key for key, value in context.items()
        if isinstance(value, list) and len(value) > _min_items(key)
    ]
    if not list_fields:
        return None
    return max(list_fields, key=lambda key: len(_dumps(context[key])))


def _min_items(list_field: str) -> int:
    return 2 if list_field in _CHRONOLOGICAL_LIST_FIELDS else 1
