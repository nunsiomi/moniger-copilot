#!/usr/bin/env python3
"""
Moniger Copilot — Ground Truth Generator  (v2, 20 personas)
============================================================
Computes the CORRECT answer to every deterministic test question directly
from the fixtures. Without this you cannot grade a numeric answer.

Covers: spending by month / category / SUBCATEGORY, merchant totals, income,
trailing-12 tax position, budget status at CATEGORY and SUBCATEGORY level,
INCOME budget (earned vs planned), and year-over-year month pairs.

Output: ground_truth.json  (consumed by test_copilot.py)

Run:  FIXTURE_DIR=../moniger_copilot_fixtures_v2 python3 ground_truth.py
"""

import json, os
from collections import defaultdict
from datetime import datetime, timezone

FIXTURES = os.environ.get("FIXTURE_DIR", "../moniger_copilot_fixtures_v2")
OUT      = "ground_truth.json"
AS_OF    = datetime(2026, 7, 20, 23, 59, 59, tzinfo=timezone.utc)
CUR      = AS_OF.strftime("%Y-%m")
TAX_FREE_KOBO = 80_000_000          # NGN800,000

def load(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return [json.loads(l) for l in f if l.strip()]

txns = load("copilot_transactions.jsonl")
buds = load("copilot_budgets.jsonl")

def k(d):     return int(d["$numberLong"])
def naira(v): return round(v / 100, 2)
def ym(t):    return t["date"]["$date"][:7]

PERSONAS = {
 "usr_anon_t01wbl": "01 Well below tax threshold",
 "usr_anon_t02jbl": "02 JUST BELOW threshold (NGN768k/yr)",
 "usr_anon_t03jab": "03 JUST ABOVE threshold (NGN840k/yr)",
 "usr_anon_t04cab": "04 Comfortably above threshold",
 "usr_anon_t05rse": "05 Mid-year raise",
 "usr_anon_t06usd": "06 USD + NGN income",
 "usr_anon_i07sal": "07 Clean monthly salary",
 "usr_anon_i08frl": "08 Irregular freelance, NO budget",
 "usr_anon_i09sid": "09 Salary + side hustle",
 "usr_anon_i10gap": "10 Income gap mid-period",
 "usr_anon_b11sea": "11 Strong seasonality",
 "usr_anon_b12mch": "12 Merchant-concentrated",
 "usr_anon_b13sub": "13 Subscription-heavy",
 "usr_anon_b14bet": "14 Betting-heavy, NO budget",
 "usr_anon_g15par": "15 Partial budgets",
 "usr_anon_g16trk": "16 All budgets on track",
 "usr_anon_g17rsk": "17 At-risk boundary",
 "usr_anon_g18ovr": "18 Chronically over budget",
 "usr_anon_g19rnt": "19 Rent-heavy",
 "usr_anon_s20new": "20 New/sparse user",
}

gt = {"as_of": AS_OF.isoformat(), "current_month": CUR,
      "tax_free_threshold_naira": 800_000, "personas": {}}

CODE_BY_ID = {t["categoryId"]: t["fiCategoryCode"] for t in txns}

for tok, label in PERSONAS.items():
    tu = [t for t in txns if t["userToken"] == tok]
    ok = [t for t in tu if t["status"] == "success"]

    spend_mc  = defaultdict(int)
    spend_msc = defaultdict(int)
    spend_m   = defaultdict(int)
    income_m  = defaultdict(int)
    merchant  = defaultdict(int)
    merch_yr  = defaultdict(int)
    by_source = defaultdict(int)
    recurring = set()

    for t in ok:
        m, v = ym(t), k(t["amountKobo"])
        if t["direction"] == "debit":
            spend_mc[(m, t["fiCategoryCode"])] += v
            spend_msc[(m, t["fiCategoryCode"], t["fiSubcategory"])] += v
            spend_m[m] += v
            merchant[t["merchantLabel"]] += v
            if m.startswith("2026"):
                merch_yr[t["merchantLabel"]] += v
            by_source[t["moneySource"]] += v
            if t.get("fiIsRecurring"):
                recurring.add(t["merchantLabel"])
        else:
            income_m[m] += v

    months   = sorted(set(list(spend_m) + list(income_m)))
    complete = [m for m in months if m != CUR]

    last12    = complete[-12:]
    trailing  = sum(income_m.get(m, 0) for m in last12)
    full_year = len(last12) == 12
    avg       = (sum(income_m.get(m, 0) for m in complete) / len(complete)) if complete else 0
    annual    = trailing if full_year else avg * 12

    yoy = {}
    for m in months:
        prev = f"{int(m[:4])-1}-{m[5:]}"
        if prev in months and m != CUR:
            yoy[m] = {"vs": prev,
                      "spend_naira": naira(spend_m.get(m, 0)),
                      "prev_spend_naira": naira(spend_m.get(prev, 0))}

    def budget_for(month, as_of_dt):
        """Budget status for a given month. as_of_dt=None means month complete."""
        bs = [b for b in buds if b["userToken"] == tok
              and b["budgetMonth"]["$date"][:7] == month]
        if not bs:
            return {}, {}, None, None, []
        b  = bs[0]
        ps = datetime.strptime(b["periodStart"]["$date"], "%Y-%m-%dT%H:%M:%S.000Z")
        pe = datetime.strptime(b["periodEnd"]["$date"],   "%Y-%m-%dT%H:%M:%S.000Z")
        elapsed = 100.0 if as_of_dt is None else \
            ((as_of_dt.replace(tzinfo=None) - ps).total_seconds()
             / (pe - ps).total_seconds()) * 100
        pct_elapsed = round(elapsed, 1)
        cat_status, sub_status, budgeted_codes, income_budget = {}, {}, [], None

        for c in b["expenseBreakdown"]:
            code = CODE_BY_ID.get(c["category_id"])
            budgeted_codes.append(code)
            alloc = k(c["total_allocated"])
            spent = sum(k(s["spentAmount"]) for s in c["subcategories"])
            pct   = round(spent / alloc * 100, 1) if alloc else None
            cat_status[c["category_name"]] = {
                "category_id": c["category_id"], "category_code": code,
                "allocated_naira": naira(alloc), "spent_naira": naira(spent),
                "percent_used": pct, "remaining_naira": naira(alloc - spent),
                "status": ("over_budget" if pct and pct > 100
                           else "at_risk" if pct and pct > elapsed else "on_track")}
            for s in c["subcategories"]:
                sa, ss = k(s["allocatedAmount"]), k(s["spentAmount"])
                sp = round(ss / sa * 100, 1) if sa else None
                sub_status[f"{c['category_name']} | {s['name']}"] = {
                    "allocated_naira": naira(sa), "spent_naira": naira(ss),
                    "percent_used": sp, "remaining_naira": naira(sa - ss),
                    "status": ("over_budget" if sp and sp > 100
                               else "at_risk" if sp and sp > elapsed else "on_track")}

        ib   = (b.get("incomeBreakdown") or [{}])[0]
        subs = ib.get("subcategories") or []
        if subs:
            al = k(subs[0]["allocatedAmount"])
            ea = k(subs[0].get("earnedAmount", {"$numberLong": "0"}))
            income_budget = {
                "planned_naira": naira(al), "earned_so_far_naira": naira(ea),
                "percent_earned": round(ea / al * 100, 1) if al else None,
                "shortfall_naira": naira(max(al - ea, 0))}
        return cat_status, sub_status, income_budget, pct_elapsed, budgeted_codes

    cat_status, sub_status, income_budget, pct_elapsed, budgeted_codes = \
        budget_for(CUR, AS_OF)
    prev_month = complete[-1] if complete else None
    p_cat, p_sub, p_income, _, _ = budget_for(prev_month, None) if prev_month else ({}, {}, None, None, [])

    gt["personas"][tok] = {
        "label": label,
        "transaction_count": len(tu),
        "months_of_history": len(months),
        "months_covered": months,
        "has_budget": bool(cat_status),
        "budgeted_category_codes": budgeted_codes,
        "percent_of_current_month_elapsed": pct_elapsed,

        "spend_by_month_naira":  {m: naira(v) for m, v in sorted(spend_m.items())},
        "income_by_month_naira": {m: naira(v) for m, v in sorted(income_m.items())},
        "spend_by_month_category_naira": {
            f"{m}|{c}": naira(v) for (m, c), v in sorted(spend_mc.items())},
        "spend_by_month_subcategory_naira": {
            f"{m}|{c}|{s}": naira(v) for (m, c, s), v in sorted(spend_msc.items())},
        "top_merchants_naira": {kk: naira(v) for kk, v in
                                sorted(merchant.items(), key=lambda x: -x[1])[:10]},
        "top_merchants_2026_naira": {kk: naira(v) for kk, v in
                                     sorted(merch_yr.items(), key=lambda x: -x[1])[:10]},
        "spend_by_money_source_naira": {kk: naira(v) for kk, v in by_source.items()},
        "recurring_merchants": sorted(recurring),

        "avg_monthly_income_naira": naira(avg),
        "trailing_12_income_naira": naira(trailing) if full_year else None,
        "has_full_year": full_year,
        "annual_income_naira": naira(annual),
        "tax_exempt_under_800k": annual <= TAX_FREE_KOBO,

        "year_over_year": yoy,
        "current_month_budget_status": cat_status,
        "current_month_subcategory_status": sub_status,
        "current_month_income_budget": income_budget,

        "previous_month": prev_month,
        "previous_month_budget_status": p_cat,
        "previous_month_subcategory_status": p_sub,
        "previous_month_income_budget": p_income,
    }

with open(OUT, "w") as f:
    json.dump(gt, f, indent=2)

print(f"Ground truth -> {OUT}   (as_of {AS_OF.date()}, current month {CUR})\n")
print(f"{'PERSONA':<20}{'TXN':>6}{'MTHS':>6}{'ANNUAL INC':>14}{'EXEMPT':>8}{'BUD':>5}{'YOY':>5}  LABEL")
print("-" * 118)
for tok, d in gt["personas"].items():
    print(f"{tok:<20}{d['transaction_count']:>6}{d['months_of_history']:>6}"
          f"{d['annual_income_naira']:>14,.0f}"
          f"{('YES' if d['tax_exempt_under_800k'] else '-'):>8}"
          f"{('y' if d['has_budget'] else '-'):>5}"
          f"{len(d['year_over_year']):>5}  {d['label']}")
print("-" * 118)

a, b = gt["personas"]["usr_anon_t02jbl"], gt["personas"]["usr_anon_t03jab"]
print("\nTAX PAIR (the discriminating test)")
print(f"  t02jbl trailing-12 = NGN{a['trailing_12_income_naira']:,.0f} -> "
      f"{'EXEMPT' if a['tax_exempt_under_800k'] else 'liable'}")
print(f"  t03jab trailing-12 = NGN{b['trailing_12_income_naira']:,.0f} -> "
      f"{'EXEMPT' if b['tax_exempt_under_800k'] else 'liable'}")

print("\nSample checkable facts:")
d = gt["personas"]["usr_anon_g18ovr"]
for cat, s in list(d["current_month_budget_status"].items())[:2]:
    print(f"  g18ovr {cat}: {s['percent_used']}% -> {s['status']}")
for kk, s in list(gt["personas"]["usr_anon_g16trk"]["current_month_subcategory_status"].items())[:2]:
    print(f"  g16trk {kk}: {s['percent_used']}% -> {s['status']}")
mm = gt["personas"]["usr_anon_b12mch"]["top_merchants_2026_naira"]
if mm:
    t0 = list(mm.items())[0]
    print(f"  b12mch top 2026 merchant: {t0[0]} = NGN{t0[1]:,.2f}")
pi = gt["personas"]["usr_anon_i07sal"]
ibud = pi["previous_month_income_budget"]
if ibud:
    print(f"  i07sal income budget ({pi['previous_month']}, complete month): "
          f"earned NGN{ibud['earned_so_far_naira']:,.0f} of "
          f"NGN{ibud['planned_naira']:,.0f} ({ibud['percent_earned']}%)")
print("\nNOTE: most personas show 0% earned for the CURRENT month because payday "
      "(day 25-28) falls after AS_OF (the 20th). That is realistic - use "
      "previous_month_income_budget for income-budget assertions.")
