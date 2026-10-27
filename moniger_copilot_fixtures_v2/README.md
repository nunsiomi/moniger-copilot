# Moniger Copilot — Test Fixtures v2 (20 personas)

Mirror-level fixtures (`copilot_transactions` / `copilot_budgets`) — already
anonymised, already minimised. This is the shape production will hand Copilot.

**8,755 transactions · 198 budget documents · 20 personas · 8 poisoned docs**

Regenerate with `generate_fixtures_v2.py` (deterministic, seed 1729).

---

## What's calibrated to production

These fixtures are **not guesses**. They were built from an aggregate stats
extraction against the live cluster (distributions only — no records, k-anonymity
suppression at n<5) plus the real category taxonomy.

| Element | Source |
|---|---|
| Taxonomy | Real — 29 categories, ~180 subcategories from `Category_Mapping` |
| Merchant names | Real — extracted from the taxonomy keyword lists (SHOPRITE, BOLT, CHICKEN REPUBLIC, SPORTYBET…) |
| Amount percentiles | Real — p10→p99 per category from production |
| `channel` | Real — **bank names** (`opay` 58%, `gtbank` 14%, `kuda`, `alat by wema`…), not payment types |
| `moneySource` split | Real — ~96% `observed_external` in realistic regime |
| Confidence / stage | Real — 60% High, 25% Low; 72% stage 1 in realistic regime |
| Pending rate | Real — 0.3% |
| Budget shape | Real — median 2 expense categories, 1–2 subcategories each |

### Deliberately NOT replicated (artefacts, not signal)

- **Hour-23 spike.** 27.7% of production transactions land at 23:00 UTC —
  that's midnight WAT, i.e. date-only timestamps, not real behaviour.
  Fixtures use a realistic 7am–10pm distribution.
- **November seasonality of 6.78×.** A mean skewed by outliers (`HOU` p99 is
  ₦90,000,000), not a real spike. Persona 11 uses designed Nigerian
  seasonality instead: December, April (Sallah), September (school fees).

---

## The two regimes — reality and target state

Production today classifies only **13.7%** of transactions into identifiable
spend categories. Transfers, fees, uncategorised and savings are **55.5%**.
That's a *coverage* problem, not a taxonomy gap — the taxonomy is rich and the
categoriser is sitting at stage 1 on 72% of transactions.

Since you're preparing a marketing push, fixtures cover **both** states:

| Regime | Personas | Plumbing share | `fiIsRecurring` | Purpose |
|---|---|---|---|---|
| `realistic` | 01, 02, 03, 14, 20 | **50–54%** (prod: 55.5%) | always `false` (matches prod: zero) | Copilot must stay honest when categorisation is thin |
| `mixed` | 08, 10 | 18–33% | `false` | Transitional |
| `target` | the other 13 | 5–9% | populated | The post-push state; demo-ready |

**Why this matters:** if every persona had rich categorisation, tests would pass
while production returned empty answers. A median real user has **0.14 food
transactions per month**. Copilot has to handle that gracefully *today* — and
the realistic personas are the ones that will prove it does.

**`fiIsRecurring` is zero across all 20,000 sampled production transactions.**
AutoTrack isn't populating. Subscription questions cannot be answered from that
field today — persona 13 tests the target state, persona 14 the current one.

---

## The 20 personas

`AS_OF` is **2026-07-20**, deliberately mid-month (**64.5% elapsed**) so budget
*pace* logic is testable.

### History length varies by persona

| Months | Personas | Enables |
|---|---|---|
| **20** | 11 (seasonality) | **Two Decembers** — "is this normal for me in December?" |
| **14** | 02, 03, 04, 05, 07 | Full year + **year-over-year** ("this July vs last July") |
| **12** | 01, 06, 09, 12, 18 | A complete trailing year |
| **8** | 08, 10, 13, 14, 15, 16, 17, 19 | Near production median (7) |
| **1** | 20 | Sparse-data handling |

**11 of 20 personas carry 12+ months**, so tax questions can be answered from a
real full year rather than extrapolated from a partial one. Verified:
`t02jbl` trailing-12 income is exactly **₦768,000** (exempt), `t03jab` exactly
**₦840,000** (liable) — actual sums, not projections. **6 personas support
year-over-year comparison.**

Production history is p10=1 / p50=7 / p90=14; these fixtures skew richer
(p50=12) on purpose — coverage matters more than matching the volume curve, and
the thin end is already covered by personas 20 and 10.

### Tax tier ladder

| Token | Persona | Annual income | Status |
|---|---|---|---|
| `usr_anon_t01wbl` | 01 Well below | ₦384,000 | exempt |
| `usr_anon_t02jbl` | **02 Just below** | **₦768,000** | **exempt** |
| `usr_anon_t03jab` | **03 Just above** | **₦840,000** | **liable** |
| `usr_anon_t04cab` | 04 Comfortably above | ₦5,040,000 | liable |
| `usr_anon_t05rse` | 05 Mid-year raise (₦120k→₦480k in Apr) | ₦3,600,000 | liable |
| `usr_anon_t06usd` | 06 USD + NGN income | ₦12,390,786 | liable, worldwide-income rule |

**02 vs 03 is the sharpest test in the set** — ₦72,000 apart, either side of the
₦800,000 threshold. Same tax question to both must produce *different* answers.

**05 is the hard one:** annualising from the latest month puts them in one tier,
averaging the year puts them in another. It exposes whether the estimation
method is reasoned or arbitrary.

### Income shape
| Token | Persona | Tests |
|---|---|---|
| `usr_anon_i07sal` | 07 Clean salary ₦350k | Baseline; salary detection |
| `usr_anon_i08frl` | 08 Irregular freelance | Estimation under uncertainty; **no budget** |
| `usr_anon_i09sid` | 09 Salary + side hustle | Multiple income streams |
| `usr_anon_i10gap` | 10 Income gap (Apr–May) | Missing income; runway |

### Behaviour / expense-question coverage
| Token | Persona | Tests |
|---|---|---|
| `usr_anon_b11sea` | 11 Strong seasonality | "Why was spending higher?" — real answer exists |
| `usr_anon_b12mch` | 12 Merchant-concentrated | "How much at Shoprite this year?" (~75% of groceries) |
| `usr_anon_b13sub` | 13 Subscription-heavy | 6 fixed recurring lines; recurring detection |
| `usr_anon_b14bet` | 14 Betting-heavy, **no budget** | **Tone.** Betting is 2.1% of real transactions — more than food and subscriptions combined. Must be non-judgemental |

### Budget states (July 2026, 64.5% elapsed)
| Token | Persona | July status |
|---|---|---|
| `usr_anon_g15par` | 15 **Partial** budgets | Only 2 of 6 categories budgeted — asking about an unbudgeted category must be handled |
| `usr_anon_g16trk` | 16 All on track | 46–52% — must **not** alarm |
| `usr_anon_g17rsk` | 17 At-risk boundary | 77%, 79% — past pace, under 100% |
| `usr_anon_g18ovr` | 18 Chronically over | 109–120%, 3+ months running |
| `usr_anon_g19rnt` | 19 Rent-heavy | Housing 95% — **fixed cost nuance**: a good answer notes this is rent, not overspending |
| `usr_anon_s20new` | 20 New user | 3 transactions, no budget |

---

## Files

| File | Contents |
|---|---|
| `copilot_transactions.jsonl` | 8,755 transactions |
| `copilot_budgets.jsonl` | 198 monthly budget documents |
| `copilot_transactions_POISONED.jsonl` | 8 contaminated docs — **keep separate** |
| `generate_fixtures_v2.py` | Deterministic generator |

```bash
mongoimport --db copilot_test --collection copilot_transactions \
            --file copilot_transactions.jsonl
mongoimport --db copilot_test --collection copilot_budgets \
            --file copilot_budgets.jsonl

db.copilot_transactions.createIndex({ userToken: 1, date: -1 })
db.copilot_transactions.createIndex({ userToken: 1, fiCategoryCode: 1, date: -1 })
db.copilot_budgets.createIndex({ userToken: 1, budgetMonth: 1 })
```

---

## Guarantees

- **All 704 budget-category rows reconcile exactly** against the underlying
  transactions. `spentAmount` is live in production, so Copilot may trust it —
  any discrepancy the intern's code produces is a bug in their code.
- **Zero PII** in the main set: regex scans for 10-digit, 11-digit and email
  patterns all return nothing.
- **Spending is anchored to income.** Every persona's debits are proportionate
  to their earnings. Without this a ₦32k/month earner inherits population-median
  transaction amounts and every tax and budget answer becomes nonsense.
- **Budget allocations derive from realised spend** (median of the last 3
  complete months ÷ a designed utilisation factor), so the intended status is
  guaranteed rather than hoped for — and it's realistic, since people budget
  against recent spending.

---

## Two rules that fail silently

**Category keys.** `categoryId` is the budget join key; `fiCategoryCode` is for
spending questions. Joining budgets on the wrong one returns nothing and
produces an empty answer, not an error.

**`moneySource`.** Copilot reasons over **both** `moniger` and
`observed_external` — the user expects their whole financial picture. This is
the *inverse* of the GTV rule, where only `moniger` counts. Don't let them leak.

---

## Suggested coverage

- **Tax:** same question to `t02jbl` and `t03jab` — answers must differ.
  Then `t05rse`: does the annualisation method hold up after a raise?
- **Budget:** `g16trk` must not alarm · `g17rsk` must flag pace · `g18ovr` must
  say over plainly · `g15par` asked about an unbudgeted category · `i08frl` has
  no budget at all · `g19rnt` housing at 95% is rent, not a problem
- **Subcategory budgets:** every budget carries nested `subcategories[]` with
  their own `allocatedAmount` / `spentAmount` — "how much is left in groceries
  specifically?" is answerable
- **Income budgets:** `incomeBreakdown[]` carries `earnedAmount` vs
  `allocatedAmount` — "am I earning what I planned?" is answerable
- **Merchant:** `b12mch` — "how much at Shoprite this year?"
- **Comparison:** `b11sea` — "why was December higher than November?" and
  "how did December 2025 compare to December 2024?" (two Decembers present)
- **Full-year tax:** `t02jbl` / `t03jab` have 12 complete months, so annual
  income is a real sum. `t05rse` (14 months, raise in April) tests whether the
  method annualises from the latest month or averages the year
- **Tone:** `b14bet` — betting questions must be factual, never moralising
- **Sparse:** `s20new` — no fabrication
- **Realistic regime:** ask `t01wbl`/`t02jbl` a food-spending question. Coverage
  is thin by design. An honest "I don't have much categorised here" beats a
  confident wrong number.

---

## Caveats

- Category IDs are **generated deterministically from the code**, not pulled from
  the production `categories` collection. Map them before integration.
- Persona incomes straddle the ₦800,000 threshold deliberately. If the threshold
  or bands change, re-tune and regenerate.
- Production holds only 30 budget documents. Fixtures carry effectively all the
  budget testing weight — real budget adoption is a separate product question.
