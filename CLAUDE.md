# CLAUDE.md

Guidance for Claude Code (or any engineer/intern) working in this repository.
Last brought in line with the code on 2026-10-24.

## Mission

Moniger Copilot is a conversational AI feature for Moniger, a Nigerian
fintech app. Users ask plain-language questions about their own spending,
income, tax position, and budgets ("How much did I spend on transport last
month?", "Am I over budget on food?") and get answers grounded in their real
transaction history — no spreadsheets, no manual categorization.

Three non-negotiable principles run through every module:

1. **Arithmetic lives in code, never in the model.** Context builders compute
   every figure, including tax band by band; the LLM only phrases and
   explains them.
2. **No raw PII reaches any model call** — classification included.
3. **No financial-product advice.** The only recommend-style output is the
   narrowly guarded Budget Lite layer, which always carries a disclaimer.

Full context lives in `docs/`:
- `Moniger_Copilot_Overview.docx` — product framing, target users, example questions, out-of-scope list.
- `Moniger_Copilot_Engineering.docx` — the original architecture (several parts superseded; see below).
- `Moniger_Copilot_Contract.docx` — SOW template: deliverables, timeline, acceptance criteria.
- `Moniger_Copilot_Scope_Addendum_Budget.docx` — Budget Tracking + Budget Lite Recommend.
- `READINESS_REVIEW.md` — 2026-09-21 audit; its findings list is the backlog (several since fixed — see "Open issues").
- `DEMO_RUNBOOK.md` — demo walkthrough. **Partly stale:** it still describes the removed Streamlit UI on port 8501 and `DEMO_PREVIEW_DEFAULT`; the demo is now Flask on port 5000 (see "Demo app").

The owner's brief also references three v1.1 docs
(`Moniger_Copilot_Intern_Brief_v1.1.docx`,
`Moniger_Copilot_Scope_Addendum_Budget_v1.1.docx`,
`Moniger_Copilot_Data_Architecture_v1.1.docx`) that are **not in this
repo**. The schema and behavioural rules here were reconstructed from
`moniger_copilot_fixtures_v2/README.md`, `moniger_copilot_tests/ground_truth.py`
and `test_copilot.py`; the brief's own rule is that **fixtures win on any
conflict with a doc**. If those docs are added, diff them against this file
(known gap: production's real `categoryId`-to-category mapping, which the
fixtures generate deterministically).

## Current status

- `/ask` runs the complete pipeline end to end against MongoDB + Redis + Groq.
- `python -m pytest` → **201 passed** (local suite, no live API needed).
- Live connectivity works (a Groq HTTP 403 was caused by Proton VPN — keep
  VPNs disconnected).
- **2026-10-26 live evaluation** (`scripts/eval_questions.csv`, 20 questions,
  one per persona type): after fixes, all 20 answered and matched the
  expected figures/behaviour. Fixed during the run: classifier invalid tool
  call (now retried), income-gap guessing (now `monthly_income`),
  subscriptions mixing in pattern-only merchants, rent projected at daily
  pace, thin over-budget answers (now a budget table). Remaining quirks:
  subscriptions also lists "possibly recurring" merchants (labelled); the
  rent-heavy answer calls Housing "on track" although its status is
  `at_risk` (ground truth keeps the status; the explanation is right).
- **2026-10-26 review feedback (implemented):** every amount carries its
  time period; sources hidden and confidence shown as High/Medium/Low;
  comparison and tax answers come with a code-built breakdown table; tax
  itemised band by band; persona dropdown in the demo; `tester` tier for
  evaluation runs; `scripts/run_questions.py` for batch question sets.
- **Not yet done:** the 72-case live acceptance suite
  (`moniger_copilot_tests/test_copilot.py`) and the 33-item manual scorecard
  have never been run against the live API. Do not represent the service as
  production-ready until they have.

## Data (moniger_copilot_fixtures_v2 / moniger_copilot_tests)

- `moniger_copilot_fixtures_v2/` — dev/test data: 8,755 transactions, 198
  budget documents, 20 designed personas, plus 8 deliberately contaminated
  PII docs (`copilot_transactions_POISONED.jsonl`, kept out of the main
  collection). Its `README.md` is the persona guide (which token tests what,
  tax-tier ladder, budget states) — read it before touching any context
  builder. Fixture snapshot date (AS_OF) is **2026-07-20**.
- `moniger_copilot_tests/` — the acceptance spec: `ground_truth.py` derives
  the correct answer to every gradeable question from the fixtures;
  `copilot_client.py` is the one file to edit if the API contract changes;
  `conftest.py` is an in-process mock (`MOCK_MODE=good|bad`) for
  sanity-checking the harness itself.
- This is a **real Mongo-mirror shape**:
  - money is **integer kobo** (`amountKobo`);
  - category is split across `categoryId` (**budgets** join on this) and
    `fiCategoryCode`/`fiSubcategory` (**spending** questions answer against
    these) — conflating the two returns nothing, silently;
  - every question must sum **both** `moneySource` values (`moniger` and
    `observed_external`) — filtering to one silently halves every figure;
  - only `status == "success"` transactions are ever used.
- Collections: `copilot_transactions`, `copilot_budgets` (DB from
  `MONGODB_DB`, default `copilot_test`).

## Architecture

A single FastAPI service (`app/main.py`) exposing `POST /ask`, deployable
behind uvicorn (Railway, `railway.json`) or as an AWS Lambda via Mangum
(`app/handler.py`). Also exposes `GET /health` and a development-only
`GET /demo/summary` (see below).

Request pipeline, in the order `app/main.py:ask` actually runs it:

1. **`gating.authenticate_and_gate`** — verifies the bearer JWT, checks its
   `sub` equals the body's `user_id`, and enforces the per-tier monthly quota
   in Redis (`quota:{user_id}:{YYYY-MM}`, self-expiring). `user_id` is the
   mirror's own anonymised `userToken` — no separate mapping exists.
2. **`data_access.get_transactions` / `get_budgets`** — reads the user's full
   successful-transaction history and all budget docs from Mongo, decodes
   BSON/Extended JSON, then runs **`mirror_validation`**: a non-mutating,
   fail-closed check. Any contaminated record aborts the request with
   `ERR_MIRROR_VALIDATION` (503) **before any model call**. Records are never
   cleaned or silently dropped. `/ask` never accepts transaction/budget data
   in the request body — populating the mirror is backend-owned.
3. **`anonymiser.anonymise`** — replaces emails, phone numbers, card numbers,
   NUBANs, BVN/NIN in the question with stable placeholders (`[ACCOUNT_1]`).
4. **Classification** — first checks an intent cache
   (`copilot:intent:v2:{sha256(user_id + normalised question)}`); on a miss,
   `classifier.classify_question` makes a forced tool call to a Groq model
   returning one of nine `QuestionCategory` values + confidence.
   Low confidence (< 0.5) or malformed output → `general`. A 400 from Groq
   (the model made an invalid tool call, `tool_use_failed`) is retried once,
   then treated as malformed → `general`. Any other **provider
   error** (`groq.APIError`: auth, 403, 429, timeout) raises
   `ClassifierUnavailableError` and **stops the pipeline** (503) rather than
   spending a second call on a blind answer. Only real classifications
   (confidence > 0) are cached.
5. **`data_access.get_as_of_date`** — only for `budget_track`/`budget_recommend`.
6. **`context_builders.build_context`** — dispatches on category and
   aggregates in code into a small JSON block. It also receives the
   anonymised question, which only COMPARISON uses (to compare the months
   the user named — `app/periods.py`).
7. **`prompt_builder`** + **`router.select_model`** build the exact prompt
   (no network), then the **answer cache** is checked:
   `copilot:v3:{user_id}:{question_type}:{sha256(category, question,
   context, model + prompt as sent)}`.
8. On a miss: **`router.call_model`** (Groq, structured JSON output).
9. **`response_parser.parse`** runs on hit and miss alike, enforces the
   guardrails/disclaimer, appends the period to any amount lacking one, and
   stamps `question_type`/`cache_hit`. The raw model text is written to the
   cache **only after it parsed successfully**. PII placeholders are
   restored on `answer`/`explanation`/`sources`.
10. **`breakdown.build_breakdown`** builds the itemised table (comparison,
    tax, budget status) from the context in code and attaches it to the
    response.

### Model routing

Everything runs on Groq; the app needs one key, `GROQ_API_KEY`. "Haiku" and
"Sonnet" are **routing tier names** inherited from the original Anthropic
design, not Anthropic models:

| Tier / env var | Default | Used for |
|---|---|---|
| `GROQ_HAIKU_MODEL_ID` | `openai/gpt-oss-20b` | spending, income, merchant, general, budget_track |
| `GROQ_SONNET_MODEL_ID` | `openai/gpt-oss-120b` | tax, comparison, budget_recommend, out_of_scope_advice (`router._SONNET_CATEGORIES`) |
| `GROQ_CLASSIFIER_MODEL_ID` | `openai/gpt-oss-20b` | intent classification (separate var to avoid a circular import and keep it independently tunable) |

- The Groq client is created with `max_retries=0, timeout=25` — the SDK's
  hidden retries are disabled. `router.call_model` retries **exactly once,
  after a fixed 2s backoff, on `RateLimitError` only**; everything else
  raises immediately and is mapped to an `ERR_MODEL_*` response.
- **We run on Groq's free tier** (Developer upgrades were unavailable
  2026-10-26): ~8,000 tokens/minute and ~200,000 tokens/day, **per model**.
  A spending question is ~7k tokens on gpt-oss-20b (≈5.5k prompt, ≈0.6k
  output, ≈1.2k classifier), so allow ~1 question/minute and ~25 questions
  per day on 20b; tax/comparison answers draw on 120b's separate allowance.
  Measured and rejected: `reasoning_effort="low"` saves only ~7% and dropped
  the time period from answers; `qwen/qwen3.8-27b` as classifier made an
  invalid tool call and rate-limited within 8 calls.
- Every call passes `response_format={"type": "json_schema", ...}` for the
  `{answer, explanation, period, confidence, sources}` shape (gpt-oss is a reasoning
  model and otherwise occasionally emitted non-JSON).
  `_MAX_RESPONSE_TOKENS = 2048`: lower budgets let hidden reasoning consume
  the whole allowance, and Groq then rejects with `json_validate_failed`.
- **Groq retires model IDs outright.** The Llama 3.x defaults died in
  2026-08 and took down every request. If every question suddenly fails,
  check `client.models.list()` first — and remember an explicit `.env`
  value overrides the code default.
- Groq's API is OpenAI-style: `call_model` folds the system prompt into
  `messages`; classifier tool arguments arrive as a JSON *string*.

### Prompt assembly

`prompt_builder.build_system_prompt` = `BASE_SYSTEM_PROMPT` + a category
addendum (`TAX_SYSTEM_PROMPT`, `BUDGET_TRACK_SYSTEM_PROMPT`,
`BUDGET_RECOMMEND_SYSTEM_PROMPT`, `OUT_OF_SCOPE_DEFLECTION_PROMPT`; the other
categories get the base prompt alone). `build_messages` embeds
`<context>…</context>` as **compact** JSON + `<question>…</question>`,
trimmed to ~4 chars/token — `HAIKU_CONTEXT_TOKEN_CAP` (default 4000) /
`SONNET_CONTEXT_TOKEN_CAP` (default 8000), env-overridable for a paid Groq
tier. Trimming order (`_within_token_budget`):

1. `_shed_nested_detail`: older months in `monthly_breakdown` collapse to a
   `{category name: total}` map; the 2 most recent keep categories plus
   their top 15 subcategories.
2. Repeatedly shrink the list field that is **largest by serialized size**
   (not item count), skipping any field at its floor (2 for chronological
   fields, which keep their newest entries; 1 otherwise).

Regression (fixed 2026-10-26): choosing by item count used to cut the
all-time `category_breakdown` to a single row for 18 of 20 personas, so "how
much did I spend on food?" got "not enough data". `tests/test_answer_presentation.py`
now checks every persona keeps every category. This is an estimate, not a
tokenizer call: prompt_builder makes no network calls.

`BASE_SYSTEM_PROMPT` rules added 2026-10-26: every money figure states its
period when the question doesn't; 3+ figures are itemised as `- ` lines with
the key figure in `**bold**` (the demo renders both); for comparison/tax the
model summarises because the table carries every row; sparse data gets a
helpful answer (what's missing, what's covered, what to ask), never a bare
"no data".

### Context builders

All read the mirror fields directly (`direction`, `amountKobo`,
`fiCategoryCode`/`fiSubcategory`, `moneySource`). Arithmetic is done in
kobo integers; conversion to naira happens once, via `_to_naira`, as a figure
enters the returned dict. Builders have no time-window notion of their own;
the returned `date_range` tells the model what period the figures cover.

- **`spending.py`** — groups by `fiCategoryCode` and `fiSubcategory`,
  carrying a readable `category_name`. Returns `as_of_month` and a
  `monthly_breakdown` so month-scoped questions can be answered
  (`BASE_SYSTEM_PROMPT` tells the model to resolve "last month" against
  `as_of_month`, never the wall clock).
- **`income.py`** — every successful credit is income (`INCM`). Salary-pattern
  confidence is a code-computed average of month coverage, amount
  consistency and day-of-month consistency (`fiSubcategory == "Salary & Wages"`).
  `estimated_annual_income_naira` sums the trailing 12 complete months when
  they exist, else falls back to average × 12 — persona T05 (₦120k→₦480k
  raise) exists to catch naive annualisation. `monthly_income` lists every
  calendar month (gaps included) and `complete_months_without_income`
  excludes the in-progress latest month (payday is usually after AS_OF), so
  "which month didn't I get paid?" (persona I10: Apr–May) isn't guessed.
- **`comparison.py`** — compares the months named in the question
  (`app/periods.resolve_months`: month names with/without year, "this/last
  month", resolved against the latest month in the data). Two named months:
  later vs earlier; one: it vs the calendar month before; none: the two most
  recent months present. `current_month_data_through` flags a partial latest
  month. Also returns `category_deltas` and `monthly_totals`.
- **`merchant.py`** — merchant-level spend breakdown. `recurring_merchants`
  is only the mirror's own `fiIsRecurring` flag (matches ground truth);
  amount-pattern matches go in `possibly_recurring_merchants`, because the
  pattern also catches regular non-subscription spend such as Bolt rides.
- **`tax.py`** / **`budget.build_recommend`** call into `income.build`/
  `spending.build` rather than re-deriving totals. `tax.compute_tax` walks
  the bands in kobo and returns `tax_computation` (per-band slice and tax,
  annual total, monthly, effective rate); the prompt forbids recomputing it.
  Only persona 19 has Housing debits, so rent relief is ₦0 for the rest.
- **`budget.py`**:
  - Budget docs are nested (`expenseBreakdown[].subcategories[]`,
    `incomeBreakdown[].subcategories[]`). `spentAmount`/`earnedAmount` are
    **trusted from the budget document**, never re-derived from
    transactions (the fixtures README: production keeps it live). Budget
    tracking therefore never joins `categoryId` against transactions.
  - `build_tracking` computes `percent_used`,
    `percent_of_current_month_elapsed`, `projected_end_of_period_spend_naira`
    and `status` (`on_track`/`at_risk`/`over_budget`) per category and
    subcategory, plus income-budget status, for the current **and** previous
    month (payday usually falls after AS_OF). Status compares actual spend
    against allocation before percentage/pace logic, so positive spend
    against a zero allocation, or spend just over allocation that rounds to
    100%, is `over_budget`. `budgeted_categories` lets an unbudgeted question
    get an honest "no budget set". Housing (`_FIXED_COST_CATEGORIES`) is
    `is_fixed_cost`: its projection is what's already paid (rent isn't
    spent at a daily pace) and the prompt explains a high percentage as a
    fixed cost; its status is left as computed to match ground truth.
  - `as_of` is the mirror's **global** latest successful-transaction date,
    treated as running through 23:59:59 (reproduces ground truth's 64.5%
    elapsed). Dates are normalised so naive and `Z`-suffixed ISO forms
    behave identically.
  - `build_recommend` uses **declared** income — the current budget's
    `totalMonthlyIncome`, else the sum of its `incomeBreakdown` allocations —
    and falls back to historical average income only when no budget exists.
    `income_basis` (`declared_budget` | `historical_average`) says which.
    `framework_50_30_20` is precomputed in code.
- Note: fixture *subcategory* budget spend is a synthetic 60/25/remainder
  split, not derived from transaction subcategories. Do not "fix" it by
  recomputing from transactions.

### PII handling

- **Question text:** `anonymiser.anonymise` → placeholders → restored on the
  way out. The model treats placeholders as opaque.
- **Mirror records:** `app/mirror_validation.py` validates — it never
  mutates. Transactions/budgets must use only allowlisted fields, `userToken`
  must match the requesting user and look like `usr_anon_…`, no email- or
  long-digit-shaped values may appear, and every bare `merchantLabel` must
  appear in the trusted catalog at `MERCHANT_LABELS_FILE`. **Unknown labels
  fail closed.** The provided catalog covers the synthetic fixtures only;
  production needs a backend-reviewed catalog. All 8 poisoned fixture
  records are rejected; clean fixtures pass unchanged.
- `anonymiser.anonymise_transactions` (the old scrub-and-drop approach) still
  exists as a legacy utility exercised by `tests/test_anonymiser.py`, but is
  **not in the request path**. Do not re-wire it: silently cleaning
  contaminated input violates the fail-closed requirement.
- Never log raw exception bodies from the provider — they can contain prompts
  or generated text (`main.handle_provider_error` logs stage/type/status only).

### Caching

- Answer key `copilot:v3:{user_id}:{question_type}:{hash}`; the hash covers
  `(category, anonymised_question, context, model + prompt as sent)`, so
  changed data **or a changed prompt/trimming/model** naturally misses —
  there's no explicit invalidation path. (Before 2026-10-26 the prompt
  wasn't in the hash, so prompt fixes kept serving stale cached answers.)
- Stored value is `{"raw_text", "model_used"}` (anonymised text only), not a
  parsed response, so `cache_hit` always reflects the current request.
- Intent key `copilot:intent:v2:…` stores `{"category"}`, so a repeated
  question skips the classifier call too.
- TTLs (`cache._TTL_SECONDS`): spending/budget_track 6h, income/
  budget_recommend 24h, tax 7d; comparison/merchant default 6h,
  general/out_of_scope_advice 24h (the defaults are adjustable).
- Redis down ⇒ read behaves as a miss, write is swallowed. A broken cache
  never breaks a response.
- Because the answer key depends on the context, a cache hit still pays for
  the Mongo fetch and context build (see "Open issues").

### Quota / gating

`app/gating.py`: per-tier **monthly** quota (`free` = 5, `premium` = 50),
tier from the JWT `tier` claim. Redis down ⇒ fails open. A third, internal
`tester` tier (`TESTER_MONTHLY_QUOTA`, default 500; 0 disables) is what the
demo and `scripts/run_questions.py` sign with, so evaluation doesn't hit
the premium limit. The production backend must never mint it. Free-tier
question-type restrictions from the Intern Brief are not implemented yet.

### Error schema

Every error is `{"error": "ERR_CODE", "message": "..."}` (agreed with the
project owner — not in the Contract doc):

| Code | HTTP | Cause |
|---|---|---|
| `ERR_JWT_INVALID` | 401 | missing/malformed/expired/bad-signature token |
| `ERR_USER_MISMATCH` | 403 | token `sub` ≠ body `user_id` |
| `ERR_QUOTA_EXCEEDED` | 429 | monthly tier quota used up |
| `ERR_VALIDATION` | 422 | malformed request body |
| `ERR_MIRROR_VALIDATION` | 503 | mirror data failed the privacy/schema contract |
| `ERR_MODEL_UNAVAILABLE` | 503 | Groq provider error (message distinguishes 401 bad key, 403 network/VPN) |
| `ERR_MODEL_RATE_LIMIT` | 429 | Groq rate/token limit (after the one retry) |
| `ERR_MODEL_REQUEST` | 503 | Groq 400/413/422, e.g. couldn't produce valid JSON |
| `ERR_MODEL_TIMEOUT` | 503 | Groq call exceeded the 25s timeout |
| `ERR_INTERNAL` | 500 | anything else (incl. `ResponseParseError`) |

### Response contract

Success: `{answer, explanation, period, confidence, sources, question_type,
cache_hit, model_used, breakdown}`. This supersedes the Engineering doc's
`{answer, key_figures, follow_up_suggestions}`. A malformed model response
raises `ResponseParseError` (→ `ERR_INTERNAL`) instead of showing an
invented answer; `period`/`confidence`/`sources` are soft fields that
default.

- `period` — the model's plain-words period for its figures (or null).
- `breakdown` — `{title, columns, rows, footer, note}` of preformatted
  strings, or null. Built in code (`app/breakdown.py`) for comparison, tax
  and budget status only; never from model output.
- `sources` stays in the API for logging but clients shouldn't show it (the
  project owner's call: answers always come from the user's own account).
  Show `confidence` as a level: ≥0.8 High, ≥0.5 Medium, else Low.

## Budget feature (Scope Addendum)

- **Layer 1 — Budget Tracking** (`budget_track`): where the user stands
  against existing budgets. All status fields computed in code — a hard
  requirement.
- **Layer 2 — Budget Lite Recommend** (`budget_recommend`): may suggest
  allocations via transparent frameworks (50/30/20, pay-yourself-first,
  zero-based), grounded in declared/real income, always with
  `BUDGET_RECOMMEND_DISCLAIMER` ("This is general budgeting guidance, not
  financial advice.") forced in code to appear exactly once.

**Guardrails (non-negotiable):** never recommend an investment/savings
product or provider, never give returns/yield figures, never direct a
savings amount — only show what's left after budgeting.
`out_of_scope_advice` must be cleanly deflected without hinting at future
product features.

Enforcement is layered, not prompt-only. `test_copilot.py`'s
`BANNED_PRODUCTS` list (mutual fund, fixed deposit, treasury bill/t-bill,
eurobond, etf, index fund, money market fund, `invest` stem, stock, crypto,
PiggyVest, Cowrywise, RiseVest/Rise Vest, Bamboo, Trove, Chaka, Stanbic, ARM
Invest, i-invest) must stay **in lockstep** across three places:
`response_parser._FORBIDDEN_TERMS`,
`response_parser._BUDGET_RECOMMEND_TERM_REPLACEMENTS`, and
`OUT_OF_SCOPE_DEFLECTION_PROMPT`'s word list.

- `_enforce_out_of_scope_guardrail` replaces an offending `answer`/
  `explanation` wholesale with a safe canonical sentence.
- `_enforce_budget_recommend_guardrail` substitutes just the offending term
  (e.g. "investment" → "savings") to preserve a correct budget figure.
- Why both: models naturally echo the user's wording while declining
  ("…can't give investment advice"), and the classifier doesn't always route
  hybrid investing/budgeting questions to `out_of_scope_advice`.

The guardrail and PII groups in the acceptance suite are pass/fail with zero
tolerance.

## Tax content

`app/prompts/tax_system_prompt.py` holds the Nigeria Tax Act 2025 figures:
₦800,000 low-income exemption (0% band), six progressive bands up to 25%
(`PERSONAL_INCOME_TAX_BANDS_NGN`), rent relief 20% capped at ₦500,000
(replacing the abolished CRA), late-filing penalties, and `TAX_AUTHORITY_NAME`
"Nigeria Revenue Service (NRS)" (replaced FIRS). These are widely reported
reform figures, not the gazetted text — update this file if official
guidance diverges; the prompt forbids the model from using its own
pre-reform knowledge. `tax.py` exposes `tax_bands_configured`/
`rent_relief_configured` flags. Band arithmetic is done in code
(`tax.compute_tax`, tested at every band boundary). The bands themselves
still need checking against the gazetted Act before anyone calls the
figures verified. "What have I spent on tax?" routes to `tax`: the mirror
has no tax category (PAYE is deducted before salary lands), so the prompt
explains that and offers the estimate.

## Open issues (from READINESS_REVIEW, still unresolved)

1. **Groq free-tier limits** (~1 question/minute, ~25/day on 20b) cap how
   much can be tested; the 4,000-token context cap exists only because of
   them. When Developer upgrades reopen (est. ~$0.001/question), upgrade,
   then raise `HAIKU_CONTEXT_TOKEN_CAP`.
2. **Full-history reads on every request.** `get_transactions`/`get_budgets`
   run before classification, unscoped by time or intent, even for
   `general`, out-of-scope questions and cache hits. Scope reads per intent
   (keeping ≥12 complete months for income/tax annualisation).
3. **Inconsistent "now".** income/comparison anchor on the user's latest
   active month; budget uses the global latest date. Introduce one explicit
   reporting date with a fixture override. Income context also lacks
   per-month income totals.
4. **Synthetic merchant catalog.** Production needs a backend-reviewed
   `MERCHANT_LABELS_FILE`.
5. **Live acceptance suite and manual scorecard unrun.**
6. `scripts/load_fixtures.py` **drops target collections**, prints the Mongo
   URI, and doesn't load `.env` — never point it at a shared/production DB.
7. Existing context tests allow ~2% monetary tolerance; tighten where exact
   results are expected.

## Integration with the Moniger platform

- The Moniger backend owns the mirror: an ETL/CDC job populates
  `copilot_transactions`/`copilot_budgets` in the fixture schema with
  anonymised `userToken`s. Copilot's DB credentials should be **read-only**.
- The backend mints the JWT (`sub` = userToken, `tier` = free|premium) with
  the shared `JWT_SECRET` and calls `/ask` with `{user_id, question}`. The
  real-user ↔ userToken mapping never enters Copilot.
- Indexes are provisioned separately: API startup creates them only if
  `MONGODB_CREATE_INDEXES=true` (`data_access.ensure_indexes`), so normal
  startup works with read-only credentials.

## Dev commands

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows; source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env   # GROQ_API_KEY, JWT_SECRET, MERCHANT_LABELS_FILE at minimum
```

**Docker is only local infrastructure.** `docker-compose.yml` runs Redis
(quota + caches) and MongoDB (stand-in for the backend's mirror). The API and
demo run directly under Python — there is no app Dockerfile. In deployment,
point `MONGODB_URI`/`REDIS_URL` at managed services instead; natively
installed or cloud-hosted Mongo/Redis work locally too.

```bash
docker compose up -d                  # Redis + Mongo
python scripts/load_fixtures.py       # DESTRUCTIVE: drops and reloads the fixture collections
uvicorn app.main:app --reload         # API on :8000
python demo/flask_app.py              # demo UI on :5000
python -m pytest                      # 201 pass
python scripts/run_questions.py scripts/eval_questions.csv   # 20-question eval, ~1/min; re-run to resume
```

## Testing & evaluation

- **`tests/`** (no live API): `test_anonymiser.py`, `test_classifier.py`
  (mocks `classifier._call_classifier_model`), `test_gating.py`,
  `test_context_builders.py` (builders vs
  `moniger_copilot_tests/ground_truth.json` — regenerate with
  `cd moniger_copilot_tests && python ground_truth.py` if fixtures change),
  `test_demo_readiness.py` (mirror validation incl. all poison cases, budget
  edge cases, provider-error mapping, caching behaviour),
  `test_flask_demo.py` (PIN gate/lockout, chat proxy, upload validation,
  persona switching, with a faked API hop and in-memory Mongo),
  `test_answer_presentation.py` (month resolution, comparison/tax tables,
  tax bands, period field and backstop, context trimming for all 20
  personas, prompt-aware cache key, tester tier), `test_run_questions.py`
  (batch runner resume/limit/retry/stop, no network).
- **`scripts/run_questions.py`** — runs a CSV of `user_id,question[,expected]`
  against a live API (local or deployed via `--api`) and writes answers,
  periods, breakdown tables, latency and errors to `<name>_results.csv` for
  grading. This is how the project owner's per-persona question sets are run.
  Free-tier defaults: `--delay 60`; a rate-limited question waits 65s and is
  retried once; 3 rate limits in a row stop the run; re-running resumes
  (answered rows kept, failures re-asked; `--fresh` restarts; `--limit N`
  caps a run). `scripts/eval_questions.csv` is the 20-question evaluation set
  (one day's free allowance) with expected answers from `ground_truth.json`.
  The demo UI holds Send for 60s after `ERR_MODEL_RATE_LIMIT`.
- **`moniger_copilot_tests/`** (acceptance spec — needs running API,
  Mongo+Redis with fixtures, real `GROQ_API_KEY`): 72 cases + 33-item
  `manual_scorecard.csv`. Run: `python ground_truth.py` → start the API →
  `COPILOT_API_URL=http://localhost:8000/ask pytest -v` (or `-m guardrail` /
  `pii` / `tax` / `budget` / `arithmetic`). Its client mints its own JWT and
  does **not** load the root `.env` — pass a matching `JWT_SECRET`. Runs
  consume real monthly quota, so use an isolated Redis/DB. Validate the
  harness first with `MOCK_MODE=good|bad`.

## Demo app

`demo/flask_app.py` — PIN-gated Flask chat UI (`demo/templates/`,
`demo/static/`, vanilla JS). Routes: `GET/POST /login`, `GET /`,
`POST /chat`, `POST /persona`, `POST /upload`, `GET /logout`; everything
but `/login` needs the session. Env: `DEMO_PIN` (4 digits) and
`FLASK_SECRET_KEY` (both required), `API_URL` (default
`http://localhost:8000/ask`), `DEMO_USER_ID` (default persona,
`usr_anon_i07sal`), `DEMO_JWT_TIER` (default `tester`), `JWT_SECRET` (must
match the API), `TRUST_PROXY=true` behind a proxy. Five wrong PINs lock an
IP out for 5 minutes (in memory, per worker). Production:
`gunicorn demo.flask_app:app`.

The "Testing as" dropdown lists the 20 fixture personas (`PERSONAS` in
`flask_app.py`); `POST /persona` only accepts those ids, stores the choice in
the session and discards any uploaded data. `/chat` mints the JWT
server-side (`sub` = active persona or upload user) and proxies to `/ask`.
`chat.js` renders `**bold**` and line breaks safely (text nodes only), the
period line, the breakdown table and a confidence level; it does not show
sources. The disclaimer is split from the answer and rendered muted.

**Upload writes to the Mongo mirror (decided 2026-10-15)** — a deliberate
exception to "the mirror is backend-owned", so the Flask process needs Mongo
write access. `POST /upload` accepts a JSON array, `{"transactions": [...]}`
/ `{"budgets": [...]}`, JSONL, or CSV (transactions only); validates with
`app.mirror_validation`; rejects transaction dates after the mirror's latest
non-upload date (`as_of` is mirror-wide, so a future date would skew everyone's
budget maths); clones the selected persona's data under a throwaway
`usr_anon_sess<hex>` token and replaces the uploaded kind. Rows are deleted on
`/logout` but not on session expiry — clean up `usr_anon_sess*` periodically.

`GET /demo/summary` on the API is an authenticated, no-AI, no-quota data
preview, returning 404 unless `APP_ENV=development`.

## Deployment

Live-test setup (2026-10-26): **API + MongoDB + Redis on the owner's own
Railway project, demo UI on their Vercel.**

- **Railway (API)**: `railway.json` runs `uvicorn app.main:app`; healthcheck
  `/health`. Env: `GROQ_API_KEY`, `JWT_SECRET`, `MONGODB_URI`/`MONGODB_DB`
  (Railway Mongo, private URL), `REDIS_URL` (Railway Redis),
  `MERCHANT_LABELS_FILE=moniger_copilot_fixtures_v2/merchant_labels.json`,
  `APP_ENV=production`. Load fixtures once from a local shell, pointing at
  Railway Mongo's **public** URL:
  `MONGODB_URI=<public url> MONGODB_DB=copilot_test MONGODB_CREATE_INDEXES=true python scripts/load_fixtures.py`
  (drops and reloads the two collections; prints the URI with the password
  redacted).
- **Vercel (demo UI)**: `vercel.json` rewrites everything to `api/index.py`,
  which exports `demo.flask_app.app` (60s max duration); `.vercelignore`
  keeps tests/docs/fixture data out of the bundle. Env: `API_URL` (Railway
  API public URL + `/ask`), the same `JWT_SECRET`, `DEMO_PIN`,
  `FLASK_SECRET_KEY`, `MONGODB_URI` (Railway Mongo **public** URL — uploads
  and persona cloning write to it), `MONGODB_DB`, `MERCHANT_LABELS_FILE`,
  `TRUST_PROXY=true`, `FLASK_COOKIE_SECURE=true`. The PIN lockout is in
  memory per serverless instance, so it is weaker on Vercel than on one
  gunicorn process.
- Deploy the API and UI together: the UI signs `tester`-tier tokens and
  expects `period`/`breakdown` in responses.
- **AWS Lambda**: `app/handler.py` wraps the app with Mangum (API Gateway or a Function URL).

Required environment variables are documented in `.env.example`.
