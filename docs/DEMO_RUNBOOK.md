# 13:30 demo — 21 September 2026

Open **http://localhost:8501**. Keep Docker Desktop running.

**Live AI restored:** disconnecting Proton VPN resolved the Groq HTTP 403. The model-list check and a real spending question now return HTTP 200. For persona 07, July food spending was NGN28,432.48, matching ground truth exactly. The repeated question was served from cache. The demo now defaults to Live Copilot (`DEMO_PREVIEW_DEFAULT=false`). Keep Proton VPN disconnected during the demo. The original fallback instructions below remain available if connectivity fails again.

The app opens in **Data preview (no AI)** because Groq rejected today's connection with HTTP 403, “Access denied. Please check your network settings.” This is a real MongoDB-backed calculation preview, not a simulated AI conversation. No generation credits are used in this mode. Changing the API key is not established as a fix for this network error.

## Three-minute walkthrough

1. Start with **07 Clean monthly salary**. Explain that these are synthetic personas, with a snapshot dated **20 July 2026**, not September account activity.
2. Show July spending: **NGN176,525.19**. Change the month to demonstrate historical spending and category/subcategory breakdowns. Both Moniger and externally observed transactions are included.
3. Open **Budget tracking**. Show allocated, spent, remaining, percentage used and the calculated status. Switch to **16 All budgets on track**, then **18 Chronically over budget** to contrast the outcomes.
4. Switch to **08 Irregular freelance, NO budget** to demonstrate the honest no-budget state.
5. Open **Allocation example**. Explain that the declared budget income is used where available; otherwise historical average income is explicitly labelled as the fallback. The 50/30/20 amounts are calculated in code.

Suggested wording: “The data and budget calculations are working against our test mirror. This preview makes those calculations visible without a model call. Live conversational generation depends on Groq, which is currently rejecting this connection.”

## Live chat, if connectivity is restored

Switch the sidebar's **Demo mode** to **Live Copilot**. For **07 Clean monthly salary**, start with “How much did I spend on food this month?” Use one question first; do not run the full live acceptance suite just before the demo. Identical successful questions with unchanged data reuse both classification and answer caches.

If the provider remains unavailable, the API returns `ERR_MODEL_UNAVAILABLE` and stops; classification failure no longer triggers a second generation call. Return to **Data preview (no AI)**. Avoid presenting tax calculations as verified: that remains a separate unresolved review item.

## Restart only if needed

From the repository root in separate terminals:

```powershell
docker compose up -d mongo
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
python demo/flask_app.py   # needs DEMO_PIN and FLASK_SECRET_KEY in .env; serves http://127.0.0.1:5000
```

The local Mongo database already contains the 8,755 transactions and 198 budgets. **Do not reload or drop the collections for this demo.** Redis was already running at localhost:6379. `.env` now points `MERCHANT_LABELS_FILE` to the synthetic merchant catalog and sets `DEMO_PREVIEW_DEFAULT=true`; existing credentials were not replaced.

Background-service logs, if needed: `demo-api.log`, `demo-api-error.log`, `demo-ui.log`, `demo-ui-error.log`. Logs are ignored by Git.

## What changed and what remains

- Fixed positive spend against a zero allocation, and spending just above allocation that rounded to 100%.
- Fixed UTC/naive date handling for budget pace.
- Recommendations distinguish declared income from observed historical income and receive precomputed 50/30/20 figures.
- Mirror reads now reject contaminated records without cleaning them. All eight poison cases are rejected. Clean fixtures pass unchanged.
- Added a trusted synthetic merchant catalog. Production requires an independently reviewed catalog; unknown labels fail closed.
- API index creation is opt-in; normal startup supports read-only credentials.
- Added classification caching, disabled hidden SDK retries, and only cache successfully parsed answers.
- Added a development-only, authenticated data-preview endpoint. It does not consume chat quota or call a model.

Validation: **115 local tests passed**. Headless UI checks rendered the preview and switched personas without errors. These results do not replace the skipped live acceptance suite or manual scoring. Full-history queries, consistent reporting-date handling, tax arithmetic/legal review, and production merchant-catalog provisioning remain follow-up work.

No commit, push or deployment was performed.
