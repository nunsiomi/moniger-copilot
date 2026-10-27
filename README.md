# Moniger Copilot

> **Live connectivity restored:** disconnecting Proton VPN resolved Groq HTTP 403. A live spending answer matched fixture ground truth and its repeat hit the cache. The demo defaults to Live Copilot. Older outage notes below are historical; the full live acceptance suite remains unrun.

> **21 September demo update:** see [the demo runbook](docs/DEMO_RUNBOOK.md).
> Local tests: 115 passed. Mirror validation now fails closed, and requires
> `MERCHANT_LABELS_FILE` (synthetic catalog supplied for development only).
> Repeated successful questions cache classification as well as the answer.
> Live Groq access is currently blocked by HTTP 403 on this connection;
> the demo offers an explicitly labelled **Data preview (no AI)** mode.
> This update supersedes the older privacy, cache and test-count descriptions below.

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
> stubs remain anywhere under `app/` or `demo/`, and `python -m pytest`
> is fully green (92 pass, 0 skip). See `CLAUDE.md` for the full pipeline
> breakdown. As of 2026-08-30 the pipeline reads real transaction/budget
> data from MongoDB (`moniger_copilot_fixtures_v2`, 20 personas) instead of
> the request body — see "Local setup" below.

## Request flow

```
JWT auth + quota gate -> anonymiser (strip PII) -> classifier (Groq Llama call)
    -> data_access (fetch this user's transactions/budgets from MongoDB,
       PII-scrubbed) -> context_builders (aggregate) -> cache lookup
    -> [miss] router (Haiku/Sonnet, retries once on a 429) -> prompt_builder
    -> Groq -> cache write (raw text) -> response_parser (always runs,
    hit or miss) -> restore PII -> response
```

Both answer generation (`openai/gpt-oss-20b` / `openai/gpt-oss-120b` by
default) and intent classification (`openai/gpt-oss-20b` by default) run
on Groq — the whole pipeline depends on a single `GROQ_API_KEY`. Groq
retires model IDs outright from time to time; if `/ask` starts failing
with `ERR_INTERNAL` across every question, check the configured model IDs
against a live `client.models.list()` call before looking anywhere else
(see `CLAUDE.md`'s "Model routing" incident note).

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

You'll need a local Redis instance (caching/quota gating) and a local
MongoDB instance (the transaction/budget mirror, `app/data_access.py`):

```bash
docker compose up -d              # starts both, see docker-compose.yml
python scripts/load_fixtures.py   # loads moniger_copilot_fixtures_v2 into Mongo
```

Run the API:

```bash
uvicorn app.main:app --reload
```

`POST /ask` with header `Authorization: Bearer <jwt>` and body:

```json
{
  "user_id": "usr_anon_i07sal",
  "question": "How much did I spend on transport last month?"
}
```

`/ask` never accepts transaction/budget data in the request body — it
looks both up server-side from MongoDB by `user_id` (see
`app/data_access.py` and `CLAUDE.md`'s "Architecture"). `user_id` is the
same identifier as the mirror's own `userToken` field —
`moniger_copilot_fixtures_v2/README.md` lists all 20 test personas
(`usr_anon_i07sal` above is "Clean monthly salary"). The JWT's `sub` claim
must equal `user_id`, or the request is rejected with `ERR_USER_MISMATCH`
(403) — see `CLAUDE.md`.

A successful response looks like:

```json
{
  "answer": "You spent ₦45,000 on Transport last month.",
  "explanation": "Sum of debit transactions categorized Transport for the requested period.",
  "confidence": 0.95,
  "sources": ["transactions Jun 2026"],
  "question_type": "spending",
  "cache_hit": false,
  "model_used": "openai/gpt-oss-20b"
}
```

## Demo app

A PIN-gated Flask chat UI (`demo/flask_app.py`) wraps the API: dark
"ledger" aesthetic, six quick-question buttons, a free-text box, JSON/CSV
upload, and per-message badges for question type, cache hits, confidence,
and (for budget recommendations) a visually muted disclaimer. Plain HTML,
CSS and vanilla JS in `demo/templates/` and `demo/static/`.

```bash
# .env needs DEMO_PIN (4 digits), FLASK_SECRET_KEY, and optionally API_URL
python demo/flask_app.py          # local, http://127.0.0.1:5000
gunicorn demo.flask_app:app       # production
```

Expects the API running at `API_URL` (default `http://localhost:8000/ask`)
with the fixtures loaded into Mongo. Flask signs the demo JWT server-side;
the browser only holds a signed session cookie. See `CLAUDE.md`, "Demo app".

## Tests

```bash
python -m pytest
```

92 pass, 0 skip. `test_classifier.py` and `test_anonymiser.py` are real
and passing (the classifier tests mock the one Groq call boundary, so
they run without hitting the live API); `test_gating.py` and
`test_context_builders.py` are real too — see `CLAUDE.md`'s "Testing &
evaluation" for the full breakdown, including
`moniger_copilot_tests/test_copilot.py`'s 72-case live acceptance suite
(needs a running API + Mongo/Redis + a real `GROQ_API_KEY`):

```bash
cd moniger_copilot_tests && python ground_truth.py
COPILOT_API_URL=http://localhost:8000/ask pytest -v
```

## Deployment

- **Railway**: `railway.json` runs the app directly with uvicorn; `/health`
  is the healthcheck path.
- **AWS Lambda**: `app/handler.py` wraps the FastAPI app with Mangum — deploy
  behind API Gateway or a Lambda Function URL.

## Known TODOs

- Production's real `categoryId`-to-category mapping isn't reflected here —
  `moniger_copilot_fixtures_v2` generates category ids deterministically
  from the category code rather than pulling them from the real
  `categories` collection (see that folder's README, "Caveats"). Map them
  before integrating against production Mongo.
- The three v1.1 docs the project brief references
  (`Moniger_Copilot_Intern_Brief_v1.1.docx`,
  `Moniger_Copilot_Scope_Addendum_Budget_v1.1.docx`,
  `Moniger_Copilot_Data_Architecture_v1.1.docx`) haven't been added to this
  repo — see `CLAUDE.md`'s note under "Mission" for what was reconstructed
  from the fixtures/tests folders instead.
- The Nigeria Tax Act 2025 figures in `app/prompts/tax_system_prompt.py`
  (bands, rent relief, exemption threshold, late-filing penalties) reflect
  the widely-reported reform figures, not the gazetted text directly —
  revisit if the final Act or NRS guidance ever diverges from what's
  encoded there.
- `app/anonymiser.py`'s merchantLabel scrubbing has one known, accepted
  gap: a bare leaked personal name with no digits and no transfer keyword
  isn't caught — see its docstring for why a stricter heuristic isn't the
  right trade (it flags real merchant/place names too).
