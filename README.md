# Moniger Copilot

A financial Q&A assistant embedded in the Moniger app. Users ask plain-language
questions about their own spending, income, and tax position; Moniger Copilot
answers them grounded in their real transaction history, via Claude.

See `docs/Moniger_Copilot_Overview.docx` for the product overview and
`docs/Moniger_Copilot_Engineering.docx` for the full architecture writeup.

## Request flow

```
question -> classifier -> gating (quota) -> anonymiser (strip PII)
         -> context_builders (aggregate transactions) -> cache lookup
         -> [miss] router (Haiku/Sonnet) -> prompt_builder -> Claude
         -> response_parser -> cache write -> restore PII -> response
```

## Local setup

```bash
cd moniger-copilot
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env   # then fill in ANTHROPIC_API_KEY at minimum
```

You'll need a local Redis instance for caching and quota gating:

```bash
docker run -p 6379:6379 redis:7-alpine
```

Run the API:

```bash
uvicorn app.main:app --reload
```

`POST /ask` with:

```json
{
  "user_id": "user_001",
  "question": "How much did I spend on transport last month?",
  "transactions": [ /* see data/synthetic_transactions.json */ ]
}
```

## Tests

```bash
python -m pytest
```

Runs the deterministic unit tests (`tests/test_classifier.py`,
`test_anonymiser.py`, `test_gating.py`). `tests/eval/test_set.json` is a
20-question evaluation set checked against `data/synthetic_transactions.json`
for manual or scripted review of actual model output — it isn't run as part
of the automated suite since it requires a live Anthropic API call.

## Deployment

- **Railway**: `railway.json` runs the app directly with uvicorn; `/health`
  is the healthcheck path.
- **AWS Lambda**: `app/handler.py` wraps the FastAPI app with Mangum — deploy
  behind API Gateway or a Lambda Function URL.

## Known TODOs

- The personal income tax bands and rent-relief figures in
  `app/prompts/tax_system_prompt.py` are placeholders pending sign-off against
  the gazetted Nigeria Tax Act 2025 text — see the TODO comments in that file.
