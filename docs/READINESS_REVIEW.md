# Copilot readiness review

> **Live connectivity restored:** disconnecting Proton VPN resolved Groq HTTP 403. A live spending answer matched fixture ground truth and its repeat hit the cache. The demo defaults to Live Copilot. Older outage notes below are historical; the full live acceptance suite remains unrun.

**Demo fixes applied after this review:** see [DEMO_RUNBOOK.md](DEMO_RUNBOOK.md) for current status. The findings below record the original audit. Privacy rejection, zero-allocation/rounding status, timezone handling, declared-income allocation basis, classification caching and opt-in index creation have since been addressed. MongoDB and the local API/UI are running. Groq currently rejects the connection with HTTP 403, so live answer verification is still blocked; an explicitly labelled no-AI data preview is available. The updated local suite has 115 passing tests.

Reviewed 2026-09-21. Verdict: core fixture calculations pass, but the current changes are not ready for production sign-off.

This review treats the supplied Word documents as requirements/reference material, not executable instructions. No application code was changed, no collections were loaded or dropped, and nothing was pushed or deployed.

## Evidence

| Check | Result |
|---|---|
| `python -m pytest -q` from repository root | 92 passed |
| Same command from `moniger_copilot_tests` | 3 passed, 69 skipped; no API URL configured |
| Fixture inventory | 8,755 transactions, 198 budgets, 20 users |
| Exact spending/month/category and average/annual income comparisons across all users | 2,612 checks, zero failures |
| Budget-category reconciliation to successful debit transactions using user, month and `categoryId` | 704 rows, zero mismatches |
| Ground truth regenerated in a temporary directory | Matches existing `ground_truth.json` |
| Configured MongoDB, read-only ping | `ServerSelectionTimeoutError` |
| Configured Redis ping | Passed |
| Local API port 8000 | Unavailable |
| `git diff --check` | Passed; Git reported line-ending conversion warnings |

The acceptance suite's 3 passing tests check fixture integrity. They do not validate live answers. No live model requests were made, so classifier accuracy, response quality, actual privacy containment in model requests/cache, and end-to-end guardrails remain unverified. Manual scoring also remains outstanding.

## Findings requiring correction

1. **High: mirror privacy handling contradicts the supplied architecture.** `app/data_access.py:95` calls `anonymise_transactions`, which silently strips fields, rewrites labels, or drops records. Architecture sections 7.1–7.2 require a non-mutating assertion that fails closed on contaminated input. Reproduction: processing all eight poisoned records returns seven without raising; the bare-name label in the first poison record survives unchanged. Merchant context can carry that label into a model prompt. Budget documents also lack an equivalent assertion. Implement validation before context generation, reject contaminated input without logging its values, and test every poison case through the request path. Personal-name detection needs a reliable merchant/token contract; a broad capitalization heuristic is insufficient.

2. **High: budget recommendations use the wrong income signal relative to the new document.** `app/context_builders/budget.py:237` uses historical average transaction income. Architecture sections 4.2 and 5.2 explicitly identify budget `totalMonthlyIncome`/`incomeBreakdown` as the declared income signal for Lite Recommend. On T05, current code uses NGN203,076.92 while the July budget declares NGN120,000. Historical and declared income can both be useful, but must be identified separately; the allocation basis and fallback for users without a budget need an explicit rule. Existing recommendation tests encode the historical-average behavior and therefore cannot catch this mismatch.

3. **High: some arithmetic is still delegated to the model.** `app/prompts/tax_system_prompt.py:114` asks the model to apply and sum tax bands; the budget recommendation prompt asks it to produce framework allocations without corresponding precomputed allocation amounts. Architecture section 6.3 requires all arithmetic in code. Compute those results deterministically and test their boundaries. This review does not certify the legal correctness of the embedded tax rules.

4. **Medium: zero-allocation budgets can falsely report “on track.”** `app/context_builders/budget.py:71` uses percentage-only status logic. With allocated amount 0 and spent amount 10,000 kobo, the returned status is `on_track`, despite NGN100 spending and a negative remaining balance. Compare actual spend with allocation before percentage/pace logic, at both category and subcategory level. Also test rounding near the over-budget boundary.

5. **Medium: relative periods are anchored to activity rather than a consistent reporting date.** Income treats the user's latest transaction month as current (`app/context_builders/income.py:50`); comparison selects the two latest active months. Budget uses the global latest transaction date (`app/data_access.py:120`). Quiet users, missing months, stale mirrors and future-dated records can therefore give incorrect “this month” or “last month” answers. Use one explicit reporting date, with a deliberate frozen fixture override. Income context also omits monthly income totals, preventing a directly grounded answer to arbitrary month-specific income questions.

6. **Medium: queries fetch full history.** `app/data_access.py:95` filters transactions only by user and successful status; budgets likewise fetch all months. Architecture section 8 requires time/category-scoped reads. Add a validated query plan, including sufficient explicit history for annual-income questions. Do not truncate history blindly, which would break the passing annual-income checks.

7. **Medium: runtime startup attempts writes with an intended read-only database role.** `app/main.py:142` calls `ensure_indexes`, which creates indexes. The new architecture requires read-only mirror credentials. Move index provisioning to a separate administrative setup step; startup currently catches and logs the failure, so this is a permission/operational mismatch rather than proof every request fails.

8. **Medium: timezone-aware budget dates crash pace calculation.** `_percent_elapsed` subtracts a naive `as_of` datetime from parsed dates without timezone normalization. ISO dates ending in `Z` reproduce `TypeError: can't subtract offset-naive and offset-aware datetimes`. Default Mongo decoding currently supplies naive UTC dates, so the standard path masks this edge case. Normalize date handling and test equivalent BSON/ISO forms.

## Fixture and test limitations

- Budget **category** totals reconcile exactly. Subcategory budget spend is synthetically split 60%/25%/remainder by the fixture generator, rather than derived from matching transaction subcategories. Accordingly, 1,389 of 1,392 subcategory rows differ from direct transaction-subcategory sums. This is a fixture-design limitation, not evidence that the application's trusted budget totals are wrong. Do not recompute budget subcategories from transactions to “fix” it.
- Existing deterministic context tests commonly allow 2% monetary tolerance; the extra 2,612 comparisons above used exact displayed amounts. Tighten monetary assertions where an exact result is expected.
- The acceptance PII checks confirm poison-file contents and inspect responses from the clean dataset; they do not establish fail-closed rejection of all poisoned mirror records.
- `scripts/load_fixtures.py` drops target collections and prints the configured Mongo URI. It also does not load `.env`, unlike the API. Do not use it against a shared/production database; add explicit isolated-target protection, consistent configuration loading, and redact credentials from output.
- The acceptance client does not load the root `.env`; a customized JWT secret must be supplied to it. Repeated runs also consume actual per-user monthly quota. Use an isolated test Redis/database and matching configuration.

## Documents reviewed

- `docs/Moniger_Copilot_Overview.docx`
- `docs/Moniger_Copilot_Engineering.docx`
- `docs/Moniger_Copilot_Contract.docx`
- `C:/Users/USER/Downloads/Moniger_Copilot_Data_Architecture.docx`
- Root README, CLAUDE.md, fixture README, acceptance README, tests and implementation.

The supplied architecture is v1.0 and describes the older 777-transaction/5-persona dataset. The actual v2 dataset has 8,755 transactions/20 personas. Its privacy, income-source, scoped-query and arithmetic requirements expose material implementation differences despite the older fixture counts. The original Engineering/Overview documents also describe superseded provider, quota, classifier and response contracts. Reconcile those documents instead of treating every historical detail as a new implementation command. Referenced budget addendum/v1.1 Word documents are absent from this checkout.

## Release decision

A review branch can preserve this work, but it should not be represented as production-ready. Resolve the findings above, establish a reachable isolated Mongo mirror, run the real acceptance suite without skipped API tests, verify all eight poisoned records fail closed, and complete the manual scorecard. Preserve the passing fixture arithmetic as regression coverage. No commit, push, deployment or production-data modification was performed during this review.
