# Moniger Copilot

A conversational AI feature embedded in the Moniger app. Users ask
plain-language questions about their own spending, income, tax position, and
budgets; Moniger Copilot answers them grounded in their real transaction
history, via Groq-hosted Llama models — both for answer generation and for
intent classification (see `CLAUDE.md`, "Model routing").

See `docs/Moniger_Copilot_Overview.docx` for the product overview,
`docs/Moniger_Copilot_Engineering.docx` for the full architecture writeup,
and `docs/Moniger_Copilot_Scope_Addendum_Budget.docx` for the Budget
Tracking + Lite Recommend feature layered on top.

> **Status:** fully implemented, end to end — no `NotImplementedError`
> stubs remain anywhere under `app/` or `demo/`. See `CLAUDE.md` for the
> full pipeline breakdown. The one real gap left is `tests/test_gating.py`,
> whose 16 cases are still `pytest.skip(...)` stubs.

## Request flow

```
JWT auth + quota gate -> anonymiser (strip PII) -> classifier (Groq Llama call)
    -> context_builders (aggregate transactions/budgets) -> cache lookup
    -> [miss] router (Haiku/Sonnet, retries once on a 429) -> prompt_builder
    -> Groq -> cache write (raw text) -> response_parser (always runs,
    hit or miss) -> restore PII -> response
```

Both answer generation (Llama 3.1 8B / Llama 3.3 70B) and intent
classification (Llama 3.1 8B by default) run on Groq — the whole pipeline
depends on a single `GROQ_API_KEY`.

Classification is a live model call (not free keyword matching), so
anonymisation runs first — no raw PII may reach any model call, including
classification. This also means a cache hit still costs one small
classification call, not zero (see `CLAUDE.md`, "Caching"). What's cached
is the model's raw text, not a pre-parsed response, so `response_parser`
runs on every request either way — its `cache_hit` field always reflects
the current request rather than being frozen at write time.

`/ask` requires a bearer JWT (`Authorization: Bearer <token>`, HS256,
claims `sub`=user_id and `tier`=free|premium) — see `CLAUDE.md` for the
quota limits, cache TTLs, and the `{"error": "ERR_CODE", "message": ...}`
error schema every failure response uses.

Nine question categories: `spending`, `income`, `tax`, `comparison`,
`merchant`, `general`, `budget_track`, `budget_recommend`, and
`out_of_scope_advice` (guardrailed deflection for investment/savings
product questions — see `CLAUDE.md`).

## Local setup

```bash
python -m venv .venv && .venv\Scripts\activate   # Windows; source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
cp .env.example .env   # then fill in GROQ_API_KEY and JWT_SECRET at minimum
```

You'll need a local Redis instance for caching and quota gating:

```bash
docker run -p 6379:6379 redis:7-alpine
```

Run the API:

```bash
uvicorn app.main:app --reload
```

`POST /ask` with header `Authorization: Bearer <jwt>` and body:

```json
{
  "user_id": "user_001",
  "question": "How much did I spend on transport last month?",
  "transactions": [ /* see data/synthetic_transactions.json */ ],
  "budgets": [ /* see data/synthetic_budgets.json, only needed for budget questions */ ]
}
```

The JWT's `sub` claim must equal `user_id` above, or the request is
rejected with `ERR_USER_MISMATCH` (403) — see `CLAUDE.md`.

A successful response looks like:

```json
{
  "answer": "You spent ₦45,000 on Transport last month.",
  "explanation": "Sum of debit transactions categorized Transport for the requested period.",
  "confidence": 0.95,
  "sources": ["transactions Jun 2026"],
  "question_type": "spending",
  "cache_hit": false,
  "model_used": "llama-3.1-8b-instant"
}
```

## Demo app

A themed Streamlit chat UI (`demo/app.py`) wraps the API — dark "ledger"
aesthetic, six quick-question buttons above a chat input, and per-message
badges for question type, cache hits, confidence, and (for budget
recommendations) a visually muted disclaimer:

```bash
streamlit run demo/app.py
```

Expects the API running locally at `http://localhost:8000`, loads the
synthetic transactions/budgets fixtures by default, and authenticates with
a locally-signed demo-only JWT (see `CLAUDE.md`, "Demo app" — not a real
login flow). A sidebar lets you upload your own transactions/budgets
(JSON or CSV) to replace that default for the session — see `CLAUDE.md`.

## Tests

```bash
python -m pytest
```

42 pass, 16 skip. `test_classifier.py` and `test_anonymiser.py` are real
and passing (the classifier tests mock the one Groq call boundary, so
they run without hitting the live API); `test_gating.py` still skips
pending implementation of its assertions — the one real test-coverage gap
left in the repo.

`tests/eval/test_set.json` is a 30-case scripted evaluation set (5
spending, 3 income, 4 tax, 3 comparison, 2 merchant, 4 budget tracking, 3
budget recommend, 6 adversarial out-of-scope deflections) — not run as
part of the automated suite since it requires a live Groq API call. Score
it against a running API with:

```bash
python tests/eval/run_eval.py --api-base-url http://localhost:8000 -v
```

It hits `/ask` once per case and checks the classified `question_type`,
a minimum `confidence` floor, and `must_contain`/`must_not_contain`
substrings (every budget-recommend/out-of-scope case bans specific
investment terms and named savings apps), printing a per-case PASS/FAIL
plus a per-category pass rate.

## Deployment

- **Railway**: `railway.json` runs the app directly with uvicorn; `/health`
  is the healthcheck path.
- **AWS Lambda**: `app/handler.py` wraps the FastAPI app with Mangum — deploy
  behind API Gateway or a Lambda Function URL.

## Known TODOs

- `tests/test_gating.py`'s 16 cases are still `pytest.skip(...)` stubs —
  imports already match `gating.py`'s real API, but the assertions were
  never written.
- `app/anonymiser.py`'s `anonymise_transactions` (raw transaction PII
  stripping) isn't wired into the `/ask` pipeline — `build_context` still
  receives raw transactions. `context_builders/*` only ever reads
  date/amount/type/category/merchant fields, so no PII reaches the LLM in
  practice, but wiring this in would be defense in depth against a future
  builder reading a field it shouldn't.
- The Nigeria Tax Act 2025 figures in `app/prompts/tax_system_prompt.py`
  (bands, rent relief, exemption threshold, late-filing penalties) reflect
  the widely-reported reform figures, not the gazetted text directly —
  revisit if the final Act or NRS guidance ever diverges from what's
  encoded there.
