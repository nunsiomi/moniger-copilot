# CLAUDE.md

Guidance for Claude Code (or any engineer/intern) working in this repository.

## Mission

Moniger Copilot is a conversational AI feature for Moniger, a Nigerian
fintech app. Users ask plain-language questions about their own spending,
income, tax position, and budgets ("How much did I spend on transport last
month?", "Am I over budget on food?") and get answers grounded in their real
transaction history — no spreadsheets, no manual categorization.

Full context lives in `docs/`:
- `Moniger_Copilot_Overview.docx` — product framing, target users, example questions, explicit out-of-scope list.
- `Moniger_Copilot_Engineering.docx` — the architecture and pipeline this repo implements.
- `Moniger_Copilot_Contract.docx` — SOW template: deliverables, timeline, acceptance criteria.
- `Moniger_Copilot_Scope_Addendum_Budget.docx` — adds Budget Tracking + a narrowly-guarded Budget Lite Recommend layer on top of the original scope.

Read the relevant doc before implementing a module — the docstrings in this
codebase point at specific sections, but the docs are the source of truth
for anything a docstring doesn't fully spell out.

## Current status

**Fully implemented, end to end.** Every module under `app/` and `demo/`
is real, working code — no `NotImplementedError` stubs remain anywhere in
the codebase. `/ask` runs the complete pipeline (auth/gate → anonymise →
classify → build context → cache lookup → route/prompt/call model on a
miss → parse → restore PII → respond), and `streamlit run demo/app.py`
gives a themed chat UI in front of it. The one thing still genuinely
unfinished is test coverage for `app/gating.py`: `tests/test_gating.py`'s
16 cases are still `pytest.skip(...)` stubs (imports match the real API;
bodies were never filled in). Data fixtures
(`data/synthetic_transactions.json`, `data/synthetic_budgets.json`) are
fully populated and are what the whole pipeline has been built and
verified against.

## Architecture

A single FastAPI service (`app/main.py`) exposing `POST /ask`, deployable
either behind uvicorn (Railway, `railway.json`) or as an AWS Lambda function
via Mangum (`app/handler.py`) — same codebase, two entry points.

Request pipeline, in order (as implemented in `app/main.py:ask`):

1. **`gating.authenticate_and_gate`** — verifies the bearer JWT (`Authorization: Bearer <token>`), confirms its `sub` claim matches the request body's `user_id`, and enforces a per-tier monthly quota in Redis (`quota:{user_id}:{YYYY-MM}`, self-expiring). Raises `InvalidTokenError` (401) / `UserMismatchError` (403) / `QuotaExceededError` (429) — see "Error schema" below.
2. **`anonymiser.anonymise`** — strips emails, phone numbers, card numbers, NUBAN account numbers, and BVN/NIN numbers from the question text, replacing each distinct value with a stable placeholder (`[ACCOUNT_1]`, etc.). Runs before classification specifically because classification is now a model call too (see next step) — no raw PII may reach any model call, classification included.
3. **`classifier.classify_question`** — a forced-tool-call to a small Groq-hosted Llama model on the *anonymised* question, returning one of nine `QuestionCategory` values (`spending`, `income`, `tax`, `comparison`, `merchant`, `general`, `budget_track`, `budget_recommend`, `out_of_scope_advice`) plus a confidence score. Confidence below `classifier._CONFIDENCE_THRESHOLD` (0.5) downgrades the category to `general`; a failed or malformed call does the same — including a missing/invalid `GROQ_API_KEY`, which is why a misconfigured deployment degrades to routing every question as `general` instead of returning an error. This supersedes the Engineering doc's original "deterministic keyword routing, no LLM call" design — classification now costs a small model call on every question, including ones that will turn out to be cache hits, since the cache key itself depends on the classified category (see "Caching" below).
4. **`context_builders.build_context`** — dispatches to the builder for the classified category, aggregating raw transactions (and, for budget questions, budget definitions) into a small structured JSON block. Builders only ever read date/amount/type/category/merchant fields — no PII enters this path by construction. (`app/anonymiser.py` also has a separate `anonymise_transactions` for stripping PII out of *raw transaction records* themselves — not yet wired into this step; see its docstring.) `GENERAL`/`OUT_OF_SCOPE_ADVICE` get a lightweight default context (transaction count + date range) since neither needs transaction-grounded detail. All arithmetic here (totals, salary-pattern confidence, month-over-month deltas, budget percent_used/projection) is computed in code — see "Context builders" below.
5. **`cache.fingerprint` + `cache.build_key` + `cache.read`** — checks Redis for `copilot:{user_id}:{question_type}:{hash}`, where hash covers `(category, anonymised_question, context)`. This step necessarily comes after steps 2–4, since the key depends on their output.
6. On a miss: **`router.select_model`** picks Haiku or Sonnet, **`prompt_builder`** assembles the system + user messages (trimming `<context>` to a per-model token cap — see "Prompt assembly"), **`router.call_model`** hits the Groq API (one blind retry after a 2s backoff on a 429), and the raw text is written to the cache as `{"raw_text", "model_used"}` — the anonymised text only, never PII, and *not* a pre-parsed result (see "Caching").
7. **`response_parser.parse`** runs unconditionally at this point — on a cache hit as much as a miss — extracting `{answer, explanation, confidence, sources}` from the raw text (tolerating code fences/prose around the JSON) and stamping on the already-known `question_type` and `cache_hit`. `BUDGET_RECOMMEND` responses get `BUDGET_RECOMMEND_DISCLAIMER` forced to appear exactly once, in code, regardless of what the model did. PII placeholders are restored via `AnonymisationResult.restore` on `answer`/`explanation`/`sources` before responding.

### Model routing

**The whole pipeline runs on Groq** — both answer generation (`router.py`)
and intent classification (`classifier.py`) now call Groq-hosted Llama
models; the project's original Anthropic-only design has been fully
migrated (Anthropic's SDK is no longer a dependency at all). Haiku is the
default tier for *answer generation* (spending / income / merchant /
general / budget_track are lookups and summarizations a small model
handles reliably and cheaply); Sonnet is for anything requiring
multi-step arithmetic where a stronger model materially reduces error
rate: tax, comparison, and budget_recommend (framework-based allocation
reasoning). Encode this in `router._SONNET_CATEGORIES`. "Haiku"/"Sonnet"
are the two *routing tier names*, inherited from the project's original
Anthropic-only design — they no longer name Anthropic models specifically.
`router.HAIKU_MODEL_ID` defaults to `llama-3.1-8b-instant` (env
`GROQ_HAIKU_MODEL_ID`), `router.SONNET_MODEL_ID` defaults to
`llama-3.3-70b-versatile` (env `GROQ_SONNET_MODEL_ID`). Groq's API is
OpenAI-style (no separate top-level `system` param like Anthropic's
Messages API had), so `router.call_model` folds `system_prompt` into
`messages` as the leading system turn itself, keeping `prompt_builder`'s
own output provider-agnostic. `router.call_model` retries exactly once,
after a fixed 2s backoff, on a `groq.RateLimitError` — any other error
(auth, bad request, server error) is raised immediately rather than
retried blindly.

`classifier.classify_question`'s forced-tool-call also runs on Groq now
(OpenAI-style function calling: `{"type": "function", "function": {...,
"parameters": ...}}`, with `tool_choice` forcing it and the model's
arguments arriving as a JSON *string* that must be `json.loads`'d, unlike
Anthropic's already-parsed `tool_use` block). It reads its own
`GROQ_CLASSIFIER_MODEL_ID` env var (default `llama-3.1-8b-instant`) rather
than `router.HAIKU_MODEL_ID`, both to avoid a circular import
(`router.py` imports `QuestionCategory` from `classifier.py`) and to keep
classification independently tunable from answer generation. The whole
app now needs exactly one API key, `GROQ_API_KEY`.

### Prompt assembly

`prompt_builder.build_system_prompt` concatenates `BASE_SYSTEM_PROMPT`
with a category addendum (`TAX_SYSTEM_PROMPT`, `BUDGET_TRACK_SYSTEM_PROMPT`,
`BUDGET_RECOMMEND_SYSTEM_PROMPT`, or `OUT_OF_SCOPE_DEFLECTION_PROMPT`) —
`SPENDING`/`INCOME`/`MERCHANT`/`COMPARISON`/`GENERAL` get the base prompt
alone. `build_messages` embeds the context dict as `<context>...</context>`
JSON followed by `<question>...</question>`, trimmed beforehand to a rough
~4-chars-per-token budget — 4000 tokens for Haiku, 8000 for Sonnet — by
proportionally shrinking whichever list field in the context is largest
(`category_breakdown`, `merchant_breakdown`, `budgets`, etc.) across a
small, fixed number of passes. This is an estimate, not a real tokenizer
call, deliberately: prompt_builder makes no Groq API calls of its own
(that's router's and classifier's job), so it can't call the SDK's
token-counting endpoint without adding a third network round-trip to
every request.

### Context builders

Each `app/context_builders/*.py` module takes whatever transaction slice
`payload.transactions` already contains and aggregates it — it never
filters by its own notion of a time window; the returned `date_range`
tells the LLM what period the aggregates actually cover. `spending.py`/
`income.py` group by `category`/`merchant`; `comparison.py` derives
"current" and "previous" from the two most recent distinct months
*present in the data* (never the wall clock), same principle
`budget.py._as_of_date` uses to stand in for "today" when computing
`percent_of_period_elapsed`. `income.py`'s salary-pattern confidence is a
code-computed average of three signals (month coverage, amount
consistency, day-of-month consistency) — never left for the LLM to eyeball.
`tax.py` and `budget.build_recommend` both call into `income.build`/
`spending.build` directly rather than re-deriving those aggregates, so
there's one source of truth for income/spend totals across builders.

### PII handling

The anonymiser runs on the free-text question before it reaches the LLM.
The system prompt must instruct the model to treat placeholders as opaque
references and never attempt to guess the underlying value. Aggregated
context contains no raw account/card/contact identifiers by construction.

### Caching

Key = `copilot:{user_id}:{question_type}:{hash}` (`app/cache.py`), where
`hash` is a SHA-256 fingerprint of `(category, anonymised_question,
context)` — `user_id`/`question_type` are their own key segments, not part
of the hash. Identical questions over identical underlying data are served
from cache without a model call; a user's transactions changing naturally
produces a different context dict and therefore a cache miss, so there's no
explicit invalidation path to maintain.

What's cached is `{"raw_text": ..., "model_used": ...}` — the model's raw
(anonymised) text and which model produced it, not a pre-parsed response.
`response_parser.parse` still runs on every request, hit or miss alike
(pipeline step 7), so its `cache_hit` field always reflects the *current*
request rather than being frozen at whatever value it had when the entry
was first written — a value baked into the cached JSON itself would read
stale (always `False`) on every subsequent hit.

TTL is per category, fixed in `app/cache._TTL_SECONDS`: `spending`/
`budget_track` 6h, `income`/`budget_recommend` 24h, `tax` 7 days.
`comparison`/`merchant` default to spending's 6h (also transaction-driven)
and `general`/`out_of_scope_advice` default to income's 24h (answers don't
depend on the user's current data) — those four aren't specified anywhere,
so treat that default as easily adjustable, not firm.

If Redis is unreachable, both `cache.read` and `cache.write` **fail open**
(a read failure behaves like a miss; a write failure is silently
swallowed) — a broken cache must never break a response.

**Cost note:** because classification (pipeline step 3) now runs before
the cache check and is itself a Haiku call, a cache hit is no longer fully
free the way the Overview doc originally promised — every question,
cached or not, costs at least one small classification call. Only the
second, larger answer-generation call (Haiku or Sonnet, step 6) is skipped
on a hit.

### Quota / gating

`app/gating.py` enforces a **per-tier monthly** quota (`free` = 5,
`premium` = 50 questions/month), keyed `quota:{user_id}:{YYYY-MM}` and
self-expiring, superseding the Engineering doc's original flat daily-limit
design. The tier comes from the JWT's `tier` claim, not a request field. If
Redis is unreachable, the quota check **fails open** — a user is never
blocked from asking a question just because the quota store is down.

### Error schema

Every error response from `/ask` is `{"error": "ERR_CODE", "message":
"..."}`. This isn't defined in `docs/Moniger_Copilot_Contract.docx` (it's a
generic SOW template with no technical schema) — it was agreed directly
with the project owner and should be treated as the actual contract going
forward:

| Code | HTTP | Raised by |
|---|---|---|
| `ERR_JWT_INVALID` | 401 | `gating.InvalidTokenError` — missing/malformed/expired/bad-signature token |
| `ERR_USER_MISMATCH` | 403 | `gating.UserMismatchError` — token's `sub` ≠ request body's `user_id` |
| `ERR_QUOTA_EXCEEDED` | 429 | `gating.QuotaExceededError` — monthly quota used up for the token's tier |
| `ERR_VALIDATION` | 422 | `app.main`'s `RequestValidationError` handler — malformed request body |
| `ERR_INTERNAL` | 500 | `app.main`'s catch-all handler — any unexpected exception |

### Response contract

A successful `/ask` response is `{answer, explanation, confidence, sources,
question_type, cache_hit, model_used}` — this supersedes the Engineering
doc's original `{answer, key_figures, follow_up_suggestions}` shape, which
is what `app/prompts/base_system_prompt.py`'s docstring and
`app/response_parser.py`'s original stub both still described before this
was implemented. `answer`/`explanation`/`confidence`/`sources` come from
the model's own JSON output (`response_parser._extract_json_object`
tolerates code fences and leading/trailing prose around it); `question_type`
and `cache_hit` are stamped on by `response_parser.parse` itself from
pipeline state the model never sees; `model_used` is added by
`app/main.py` after parsing, not part of `ParsedResponse` at all. A
malformed or non-JSON model response raises `response_parser.ResponseParseError`
(surfaces as `ERR_INTERNAL`) rather than showing the user a fabricated
answer about their own money — `confidence`/`sources` are treated as soft
fields and default instead of failing, since losing them doesn't put a
wrong figure in front of anyone.

## Budget feature (Scope Addendum)

Two layers, both gated behind the `BUDGET_TRACK` / `BUDGET_RECOMMEND`
classifier intents and built in `app/context_builders/budget.py` +
`app/prompts/budget_system_prompt.py`:

- **Layer 1 — Budget Tracking** (pure Ask/Explain): describes where the
  user stands against an existing budget. `percent_used`,
  `percent_of_period_elapsed`, `projected_end_of_period_spend`, and
  `status` (`on_track` | `at_risk` | `over_budget`) are **computed in code,
  never by the LLM** — this is a hard requirement, not a style preference.
- **Layer 2 — Budget Lite Recommend**: a deliberately narrow exception to
  the project's "no Recommend" rule. May suggest category allocations using
  transparent frameworks (50/30/20, pay-yourself-first, zero-based),
  grounded in the user's real income/spend, always carrying a one-line
  disclaimer ("This is general budgeting guidance, not financial advice.")
  exactly once.

**Guardrails are non-negotiable** (see `app/prompts/budget_system_prompt.py`
docstring for the full list): never recommend an investment/savings
product or provider, never give returns/yield figures, never issue a
savings amount as a directive — only show what's mathematically left after
budgeting. `OUT_OF_SCOPE_ADVICE` questions must be cleanly deflected:
decline the out-of-scope part, redirect to budgeting, never hint at future
product features. The evaluation set (`tests/eval/test_set.json`, cases
`eval-25`/`eval-26`) includes adversarial deflection cases specifically to
catch guardrail leaks — any leak there is a failed metric, not a nitpick.

## Tax content

`app/prompts/tax_system_prompt.py` holds the Nigeria Tax Act 2025 figures:
`LOW_INCOME_EXEMPTION_THRESHOLD_NGN` (₦800,000, 0% band), the six
progressive bands up to 25% in `PERSONAL_INCOME_TAX_BANDS_NGN`,
`RENT_RELIEF_RATE`/`RENT_RELIEF_CAP_NGN` (20%, capped at ₦500,000 —
replacing the old, now-abolished Consolidated Relief Allowance),
`LATE_FILING_PENALTY_FIRST_MONTH_NGN`/`_SUBSEQUENT_MONTH_NGN`, and
`TAX_AUTHORITY_NAME` ("Nigeria Revenue Service (NRS)", which replaced
FIRS). These are the widely-reported reform figures, not a substitute for
the final gazetted text — if the gazetted Act or NRS guidance ever
diverges from what's encoded here, update this file to match, since
`context_builders/tax.py` and `TAX_SYSTEM_PROMPT` are the *only* source of
truth the model is allowed to use; the prompt explicitly forbids falling
back on its own (pre-reform) training-data knowledge of Nigerian tax law.
`context_builders/tax.py` still exposes `tax_bands_configured`/
`rent_relief_configured` flags (both `True` now) so the prompt can react
correctly if either ever gets reset to empty/`None`. Progressive-band
arithmetic (walking each band's marginal rate) is deliberately left to the
model, not computed in `tax.py` — `TAX` routes to Sonnet specifically
because that multi-step arithmetic is exactly what a stronger model is
worth the extra cost for (see "Model routing").

## Dev commands

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows; source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env   # fill in GROQ_API_KEY at minimum; JWT_SECRET must
                        # match whatever signs the bearer tokens sent to /ask
```

Redis is required locally for cache/gating:
```bash
docker run -p 6379:6379 redis:7-alpine
```

Run the API: `uvicorn app.main:app --reload`
Run the demo: `streamlit run demo/app.py` (expects the API running at `http://localhost:8000`)
Run tests: `python -m pytest` (42 pass, 16 skip — see "Testing & evaluation")

## Testing & evaluation

- `tests/test_gating.py` — still skip-stubs (16 cases); imports already match `gating.py`'s real API, but bodies were never filled in. The only real test-coverage gap left in the repo.
- `tests/test_anonymiser.py`, `tests/test_classifier.py` — real, passing unit tests. `test_classifier.py` mocks `classifier._call_classifier_model` (the one Groq call boundary) rather than hitting the live API, so it tests classify_question's own logic (enum mapping, the confidence threshold, error handling) deterministically — whether the model itself classifies real questions correctly is an eval-set concern, not a unit-test one.
- `tests/eval/test_set.json` — 30 cases (5 spending, 3 income, 4 tax, 3 comparison, 2 merchant, 4 budget_track, 3 budget_recommend, 6 out_of_scope_advice adversarial) against `data/synthetic_transactions.json` and `data/synthetic_budgets.json`. Each case is `{id, question, expected_question_type, expected_confidence, must_contain, must_not_contain, notes}` — every `must_contain` figure was verified by calling the real `app.context_builders` functions directly, not hand-calculated. `expected_confidence` is a *minimum floor* checked against the response's own `confidence` field, since the classifier's internal classification confidence is never exposed by `/ask` — there's no other value a live run could check. Every `budget_recommend`/`out_of_scope_advice` case's `must_not_contain` carries the same guardrail term list (`mutual fund`, `fixed deposit`, `investment`, `stocks`, `crypto`, plus five real Nigerian savings/investment app names) per the Addendum's non-negotiable guardrails.
- `tests/eval/run_eval.py` — hits the live `/ask` endpoint once per case and scores it against the fields above, printing a per-case PASS/FAIL, a per-category breakdown, and an overall pass rate. Requires a running API with a real `GROQ_API_KEY` configured (`python tests/eval/run_eval.py [--api-base-url URL] [-v]`) — not run in CI for the same reason the eval set itself isn't.
- `demo/` has no committed test file, but its logic was verified with `streamlit.testing.v1.AppTest` (renders headlessly, drives button clicks/`chat_input` submissions, and asserts on the resulting markdown) — a reasonable pattern to reach for again if the demo grows.

## Demo app

`demo/app.py` is a themed Streamlit chat UI in front of `/ask` — dark
"ledger" aesthetic (Fraunces display serif, Sora body, JetBrains Mono for
figures/badges), six preset quick-question buttons
(`demo/components/quick_buttons.py`) above a `st.chat_input` bar, and a
custom-rendered message history (`demo/components/chat.py`, not
`st.chat_message`, for full control over bubble/badge/disclaimer styling).
Each assistant turn shows the question-type + cache-hit chips, the
answer, the explanation, a confidence bar, sources, and — for
`budget_recommend` only — `BUDGET_RECOMMEND_DISCLAIMER` pulled out of
wherever the model placed it and re-rendered as a separate, visually
muted footnote rather than left inline.

Auth is a locally-signed JWT (`demo/components/chat.py`'s `_demo_token`),
not a real login flow: it reads `JWT_SECRET` from the environment (falling
back to the same `"dev-only-change-me"` default as `app.main.Settings`)
so it keeps working even if a developer has customised `JWT_SECRET`
locally, and always claims `tier: "premium"` so a demo session doesn't
trip the 5/month free-tier quota.

A sidebar (`demo/app.py`'s `_render_data_sidebar`) lets a user upload their
own transactions/budgets — JSON (either the fixture's `{"transactions":
[...]}` shape or a bare list) or CSV — to replace the synthetic defaults
for that session only; uploads live in `st.session_state`, never touch
`data/`, and a "Reset to synthetic demo data" button clears them. `amount`
is coerced to a float where possible regardless of source format, since
CSV rows arrive as strings by construction and a user's own JSON export
can serialize numbers as strings too.

## Deployment

- **Railway**: `railway.json` runs `uvicorn app.main:app` directly; `/health` is the healthcheck path.
- **AWS Lambda**: `app/handler.py` wraps the FastAPI app with Mangum; deploy behind API Gateway or a Lambda Function URL.

Required environment variables are documented in `.env.example`.
