#!/usr/bin/env python3
"""
Moniger Copilot — 20-Persona Fixture Generator  (v2)
=====================================================
Generates mirror-level fixtures (copilot_transactions / copilot_budgets)
for 20 designed personas.

CALIBRATED TO PRODUCTION:
  - Real taxonomy (29 categories / ~180 subcategories) from Category_Mapping
  - Real merchant names extracted from the taxonomy keyword lists
  - Real amount percentiles per category (from aggregate stats extraction)
  - Real channel mix (channel = BANK NAME, not payment type)
  - Real ratios: pending 0.3%, observed_external ~96%, confidence dist

TWO CATEGORISATION REGIMES ("balance of reality and target state"):
  realistic : mirrors today's coverage - heavy TRF/FEE/OTH, thin spend cats,
              fiIsRecurring always False (AutoTrack not populating in prod)
  target    : the post-marketing-push state - rich categorisation, low OTH,
              recurring flags populated

DELIBERATELY NOT REPLICATED (data artefacts, not signal):
  - hour-23 spike (27.7% of prod txns; timezone/date-only artefact)
  - November seasonality index of 6.78x (mean skewed by outliers)

Run:  python3 generate_fixtures_v2.py
"""

import json, random, os, re
from datetime import datetime, timedelta, timezone
from collections import defaultdict

random.seed(1729)

# ---------------------------------------------------------------- CONFIG
AS_OF          = datetime(2026, 7, 20, 23, 59, 59, tzinfo=timezone.utc)
MONTHS_HISTORY = 8                       # Dec 2025 -> Jul 2026
OUT_DIR        = "/mnt/user-data/outputs/moniger_copilot_fixtures_v2"
TAXONOMY_XLSX  = "/mnt/user-data/uploads/Category_Mapping_Download.xlsx"

def kobo(naira_amt): return int(round(naira_amt * 100))

# ---------------------------------------------------------------- TAXONOMY
import pandas as pd
_tax = pd.read_excel(TAXONOMY_XLSX)
_tax.columns = [str(c).strip() for c in _tax.columns]

CATEGORY_NAME, SUBCATS, MERCHANTS = {}, defaultdict(list), {}
for _, r in _tax.iterrows():
    code, cat, sub, kw = (r.get("Category Code"), r.get("Category"),
                          r.get("Sub-Category"), r.get("Keywords"))
    if pd.isna(code) or pd.isna(sub):
        continue
    code = str(code).strip()
    if not pd.isna(cat):
        CATEGORY_NAME.setdefault(code, str(cat).strip())
    sub = str(sub).strip()
    if sub not in SUBCATS[code]:
        SUBCATS[code].append(sub)
    if pd.isna(kw):
        continue
    names = []
    for tok in str(kw).split(","):
        t = tok.strip().strip('"').replace("\\n", " ").strip()
        if not t or len(t) < 3 or len(t) > 26:            continue
        if any(c in t for c in "()|?*\\[]^$"):            continue
        if not re.match(r"^[A-Za-z0-9 .&'-]+$", t):       continue
        if t.lower() == t:                                continue
        names.append(t.upper())
    names = list(dict.fromkeys(names))[:14]
    if names:
        MERCHANTS.setdefault(f"{code}|{sub}", names)

# Deterministic ObjectId-style category ids derived from the code
def cat_id(code):
    h = 0
    for ch in code:
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return f"6a{h:08x}{'c1dd1fd52817':.12}"[:24].ljust(24, "0")

CAT_IDS = {c: cat_id(c) for c in CATEGORY_NAME}

def merchant_for(code, sub):
    pool = MERCHANTS.get(f"{code}|{sub}")
    if pool:
        return random.choice(pool)
    return CATEGORY_NAME.get(code, code).upper()

# ---------------------------------------------------------------- AMOUNTS
# Percentiles taken from the production aggregate extraction (naira).
PCT = {
 "TRF": [25,1000,2645,9000,20000,285000],   "FEE": [0.75,3.75,10.75,50,50,848],
 "OTH": [300,700,2100,6000,20000,130000],   "AIR": [100,200,500,1000,1500,13000],
 "SAVE":[21,80,275,1000,3640,50816],        "SHOP":[600,1250,2700,5508,13850,50000],
 "ENT": [100,150,400,1000,3000,27410],      "UTIL":[983,1000,1800,5000,10000,100000],
 "FND": [1000,2500,4452,6712,14935,56500],  "SUB": [1017,1500,2000,4500,6500,84950],
 "LOAN":[180,4650,10000,18494,27200,49972], "TRV": [2000,2580,3500,5500,9027,11000],
 "HLTH":[800,2000,3500,10000,56000,59000],  "DON": [1010,2000,4000,5000,19150,23000],
 "EDU": [100,1000,15500,68913,100000,100000],
 "AUTO":[1500,2000,3775,8116,10000,25000],
 "PERS":[200,500,2000,4500,25600,91000],
 # designed (production n too small to trust)
 "GROC":[2000,4500,12000,25000,45000,120000],
 "HOU": [80000,150000,250000,450000,900000,2500000],
 "GAD": [8000,20000,45000,120000,300000,650000],
 "INS": [5000,12000,25000,60000,150000,400000],
 "LOG": [800,1500,2500,5000,12000,40000],
 "TAX": [7,50,50,500,5000,50000],
 "PETS":[1500,3000,6000,12000,30000,60000],
 "FAM": [5000,15000,35000,80000,200000,400000],
}
BANDS = [(0,10),(10,25),(25,50),(50,75),(75,90),(90,99)]

def amount_for(code):
    p = PCT.get(code, [500,1500,4000,10000,25000,80000])
    i = random.choices(range(6), weights=[10,15,25,25,15,10])[0]
    lo = p[i]; hi = p[min(i+1, 5)]
    if hi <= lo: hi = lo * 1.6 + 1
    return round(random.uniform(lo, hi), 2)

# ---------------------------------------------------------------- CHANNELS
BANKS   = ["opay","gtbank","alat by wema","kuda bank","sterling bank",
           "access bank","zenith bank","providus"]
BANK_W  = [0.58, 0.14, 0.06, 0.056, 0.045, 0.037, 0.026, 0.056]

def confidence(regime):
    r = random.random()
    if regime == "target":                       # better classification
        return "High" if r < .86 else ("Medium" if r < .96 else "Low")
    return "High" if r < .60 else ("Medium" if r < .75 else "Low")

def stage(regime):
    r = random.random()
    if regime == "target":
        return "3" if r < .55 else ("2" if r < .85 else "1")
    return "1" if r < .72 else ("2" if r < .90 else "3")

# ---------------------------------------------------------------- HELPERS
_oid = [0x610000000000000000000000]
def oid():
    _oid[0] += 1
    return format(_oid[0], "024x")

def xdate(dt): return {"$date": dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
def xlong(v):  return {"$numberLong": str(int(v))}

def month_starts(n, as_of):
    out, y, m = [], as_of.year, as_of.month
    for _ in range(n):
        out.append(datetime(y, m, 1, tzinfo=timezone.utc))
        m -= 1
        if m == 0: m, y = 12, y - 1
    return list(reversed(out))

def month_end(dt):
    y, m = (dt.year + 1, 1) if dt.month == 12 else (dt.year, dt.month + 1)
    return datetime(y, m, 1, tzinfo=timezone.utc) - timedelta(seconds=1)

def realistic_hour():
    """Deliberately NOT the prod hour-23 artefact."""
    return random.choices(
        [7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22],
        weights=[3,5,7,8,8,9,8,7,7,8,9,10,10,7,4,2])[0]

def dates_in(ms, count, as_of, day_bias=None):
    last = min(month_end(ms), as_of)
    span = (last - ms).days
    if span < 1 or count <= 0:
        return []
    out = []
    for _ in range(count):
        d = random.randint(0, span) if not day_bias else \
            min(span, max(0, int(random.gauss(day_bias, 4))))
        out.append(ms + timedelta(days=d, hours=realistic_hour(),
                                  minutes=random.randint(0, 59)))
    return sorted(t for t in out if t <= as_of)

# ---------------------------------------------------------------- TXN
def make_txn(token, dt, code, sub, amt, direction, money_source, regime,
             merchant=None, recurring=False, status=None):
    ch = "providus" if money_source == "moniger" else random.choices(BANKS, BANK_W)[0]
    return {
        "_id":            {"$oid": oid()},
        "srcId":          {"$oid": oid()},
        "userToken":      token,
        "date":           xdate(dt),
        "amountKobo":     xlong(kobo(amt)),
        "direction":      direction,
        "status":         status or ("pending" if random.random() < 0.003 else "success"),
        "categoryId":     CAT_IDS[code],
        "categoryName":   CATEGORY_NAME[code],
        "fiCategory":     CATEGORY_NAME[code],
        "fiCategoryCode": code,
        "fiSubcategory":  sub,
        "fiConfidence":   confidence(regime),
        "fiClassificationStage": stage(regime),
        "fiIsRecurring":  bool(recurring) if regime == "target" else False,
        "merchantLabel":  merchant or merchant_for(code, sub),
        "channel":        ch,
        "moneySource":    money_source,
        "isBillPayment":       code in ("UTIL", "AIR"),
        "isWalletToWallet":    code == "TRF" and sub == "Self Transfer",
        "isExternalTransfer":  money_source == "observed_external",
    }

# ---------------------------------------------------------------- SPEND MIXES
# weight = relative share of a persona's DEBIT transactions
REALISTIC_MIX = {
    "TRF":.30,"FEE":.17,"OTH":.12,"AIR":.09,"SAVE":.07,"SHOP":.05,"ENT":.05,
    "FND":.04,"UTIL":.03,"AUTO":.03,"SUB":.02,"HLTH":.015,"GROC":.015,"LOG":.01,
}
TARGET_MIX = {
    "FND":.15,"GROC":.12,"AUTO":.12,"AIR":.09,"SHOP":.09,"UTIL":.08,"ENT":.06,
    "TRF":.09,"SUB":.05,"HLTH":.04,"FEE":.04,"PERS":.025,"LOG":.02,"EDU":.015,
    "DON":.01,"OTH":.01,
}
def mix_for(regime):
    if regime == "target":   return dict(TARGET_MIX)
    if regime == "mixed":
        m = {}
        for k in set(REALISTIC_MIX) | set(TARGET_MIX):
            m[k] = (REALISTIC_MIX.get(k,0) + TARGET_MIX.get(k,0)) / 2
        return m
    return dict(REALISTIC_MIX)

SEASONAL = {12:1.55, 1:1.15, 2:.85, 3:.95, 4:1.30, 5:.95, 6:1.0, 7:1.0,
            8:.95, 9:1.20, 10:.95, 11:1.05}   # Dec, Sallah/Apr, Sept school fees

# ---------------------------------------------------------------- PERSONAS
def P(label, regime, density, income, budget=None, spend_ratio=0.80,
      budget_util=0.90, history=None, **kw):
    """
    spend_ratio : monthly debits as a fraction of monthly income
    budget_util : designed END-OF-MONTH budget utilisation. Allocations are
                  derived from realised spend / budget_util, so the designed
                  on-track / at-risk / over status is guaranteed rather than
                  hoped for.
    """
    return dict(label=label, regime=regime, density=density, income=income,
                budget=budget, spend_ratio=spend_ratio,
                budget_util=budget_util,
                history=history or MONTHS_HISTORY, **kw)

def salary(amount, day=26, sub="Salary & Wages"):
    return dict(kind="salary", code="INCM", sub=sub, amount=amount, day=day, per_month=1)

def irregular(amount, per_month, sub="Marketplace / Side-Hustle Proceeds", jitter=(.35,2.0)):
    return dict(kind="irregular", code="INCM", sub=sub, amount=amount,
                per_month=per_month, jitter=jitter)

PERSONAS = {
# ---- tax tier ladder
"usr_anon_t01wbl": P("01 Well below tax threshold (~N32k/mo)","realistic",14,
    [salary(32_000, 28)], ["FND","AIR"], history=12),
"usr_anon_t02jbl": P("02 JUST BELOW threshold (N64k/mo = N768k/yr)","realistic",18,
    [salary(64_000, 27)], ["FND","AIR","AUTO"], history=14),
"usr_anon_t03jab": P("03 JUST ABOVE threshold (N70k/mo = N840k/yr)","realistic",20,
    [salary(70_000, 27)], ["FND","AIR","AUTO"], history=14),
"usr_anon_t04cab": P("04 Comfortably above threshold (N420k/mo)","target",46,
    [salary(420_000, 25)], ["FND","GROC","AUTO","UTIL","ENT"], history=14),
"usr_anon_t05rse": P("05 Mid-year RAISE (N120k -> N480k in Apr)","target",42,
    [salary(120_000, 26)], ["FND","GROC","AUTO"],
    raise_month=4, raise_to=480_000, history=14),
"usr_anon_t06usd": P("06 USD + NGN income (worldwide income rule)","target",38,
    [salary(260_000, 25), irregular(700_000, 1, "Other Income (Unclassified Credit)", (.8,1.2))],
    ["FND","GROC","TRV","AUTO"], history=12),

# ---- income shape
"usr_anon_i07sal": P("07 Clean monthly salary baseline (N350k)","target",40,
    [salary(350_000, 26)], ["FND","GROC","AUTO","UTIL"], history=14),
"usr_anon_i08frl": P("08 Irregular freelance, no fixed salary","mixed",34,
    [irregular(95_000, 3), irregular(38_000, 2, "Gifts & Grants Received")],
    None),
"usr_anon_i09sid": P("09 Salary + side hustle","target",44,
    [salary(240_000, 25), irregular(70_000, 2)],
    ["FND","GROC","AUTO","SHOP"], history=12),
"usr_anon_i10gap": P("10 Income GAP mid-period (job loss Apr-May)","mixed",30,
    [salary(300_000, 26)], ["FND","GROC","AUTO"],
    income_gap=(4,5)),

# ---- behaviour / expense-question coverage
"usr_anon_b11sea": P("11 Strong SEASONALITY (Dec/Apr/Sept spikes)","target",48,
    [salary(380_000, 26)], ["FND","GROC","SHOP","AUTO"],
    seasonal=True, history=20),
"usr_anon_b12mch": P("12 MERCHANT-concentrated (heavy Shoprite/Bolt)","target",52,
    [salary(330_000, 26)], ["GROC","AUTO","FND"],
    concentrate=[("GROC","Supermarkets & Hypermarkets","SHOPRITE",.75),
                 ("AUTO","Ride-hailing & Taxis","BOLT",.70)], history=12),
"usr_anon_b13sub": P("13 SUBSCRIPTION-heavy (recurring detection)","target",40,
    [salary(300_000, 26)], ["SUB","FND","UTIL","ENT"],
    subs_heavy=True),
"usr_anon_b14bet": P("14 Betting-heavy, NO budget (tone handling)","realistic",70,
    [salary(210_000, 26), irregular(45_000, 2, "Gambling Winnings (betting payout)")],
    None, betting_heavy=True, spend_ratio=1.06),

# ---- budget states
"usr_anon_g15par": P("15 PARTIAL budgets (only 2 of 6 categories)","target",44,
    [salary(320_000, 26)], ["FND","AUTO"]),
"usr_anon_g16trk": P("16 All budgets ON TRACK","target",38,
    [salary(300_000, 26)], ["FND","GROC","AUTO","UTIL","ENT"],
    spend_ratio=0.62, budget_util=0.76),
"usr_anon_g17rsk": P("17 AT-RISK boundary (just past pace)","target",40,
    [salary(280_000, 26)], ["FND","GROC","AUTO"],
    spend_ratio=0.80, budget_util=1.22),
"usr_anon_g18ovr": P("18 CHRONICALLY over budget, 3+ months","target",58,
    [salary(260_000, 25)], ["FND","GROC","AUTO","SHOP"],
    spend_ratio=1.18, budget_util=1.85, history=12),
"usr_anon_g19rnt": P("19 RENT-heavy, far from 50/30/20","target",36,
    [salary(450_000, 26)], ["HOU","FND","GROC","AUTO"],
    rent=280_000, spend_ratio=0.92, budget_util=0.95),

# ---- sparse
"usr_anon_s20new": P("20 NEW user, 3 transactions","realistic",0,
    [], None, sparse=True),
}

# ---------------------------------------------------------------- GENERATE
def gen_persona(token, p):
    txns, regime = [], p["regime"]
    if p.get("sparse"):
        base = AS_OF - timedelta(days=7)
        for i, (code, sub, amt) in enumerate(
                [("GROC","Supermarkets & Hypermarkets",8_400),
                 ("AUTO","Ride-hailing & Taxis",2_150),
                 ("AIR","Mobile Airtime",1_000)]):
            txns.append(make_txn(token, base + timedelta(days=i*2, hours=3),
                                 code, sub, amt, "debit", "moniger", regime))
        return txns

    mix     = mix_for(regime)
    codes   = list(mix); weights = [mix[c] for c in codes]
    ext_p   = 0.75 if regime == "target" else 0.94

    for ms in month_starts(p["history"], AS_OF):
        is_cur  = (ms.year, ms.month) == (AS_OF.year, AS_OF.month)
        frac    = (((AS_OF - ms).days + 1) / ((month_end(ms) - ms).days + 1)) if is_cur else 1.0
        season  = SEASONAL.get(ms.month, 1.0) if p.get("seasonal") else 1.0
        gapped  = p.get("income_gap") and ms.month in p["income_gap"]

        # ---- income
        for inc in p["income"]:
            if gapped: continue
            amt = inc["amount"]
            if p.get("raise_month") and (ms.month >= p["raise_month"] or ms.year > 2025):
                if inc["kind"] == "salary" and ms.month >= p["raise_month"] and ms.year == 2026:
                    amt = p["raise_to"]
            if inc["kind"] == "salary":
                d = ms + timedelta(days=inc["day"] - 1, hours=8, minutes=random.randint(0,50))
                if d <= AS_OF:
                    txns.append(make_txn(token, d, "INCM", inc["sub"], amt, "credit",
                                         "moniger", regime, merchant="SALARY CREDIT",
                                         recurring=True))
            else:
                n = max(0, int(round(inc["per_month"] * frac)))
                for d in dates_in(ms, n, AS_OF):
                    j = random.uniform(*inc.get("jitter", (1,1)))
                    txns.append(make_txn(token, d, "INCM", inc["sub"],
                                         round(amt * j, 2), "credit",
                                         "observed_external", regime))

        # ---- rent (fixed, monthly)
        if p.get("rent"):
            d = ms + timedelta(days=2, hours=10)
            if d <= AS_OF:
                txns.append(make_txn(token, d, "HOU", "Residential Rent", p["rent"],
                                     "debit", "moniger", regime,
                                     merchant="LANDLORD RENT", recurring=True))

        # ---- subscriptions (fixed recurring set)
        if p.get("subs_heavy"):
            for name, amt, day in [("NETFLIX",4_400,3),("SPOTIFY",1_300,7),
                                   ("SHOWMAX",3_200,11),("YOUTUBE PREMIUM",2_200,15),
                                   ("ICLOUD",1_100,19),("DSTV",12_500,1)]:
                d = ms + timedelta(days=day-1, hours=9)
                if d <= AS_OF:
                    code = "SUB" if name != "DSTV" else "UTIL"
                    sub  = "Streaming Services" if name not in ("ICLOUD","DSTV") else \
                           ("Software & Apps" if name == "ICLOUD" else "Internet & Cable")
                    txns.append(make_txn(token, d, code, sub, amt, "debit",
                                         "moniger", regime, merchant=name, recurring=True))

        # ---- betting
        if p.get("betting_heavy"):
            bet_income = sum(int(t["amountKobo"]["$numberLong"]) for t in txns
                             if t["direction"] == "credit"
                             and t["date"]["$date"][:7] == ms.strftime("%Y-%m")) or kobo(200_000)
            bet_pool = bet_income * 0.22 * frac / 100.0
            bdates = dates_in(ms, int(round(22 * frac)), AS_OF)
            if bdates:
                per = bet_pool / len(bdates)
                for d in bdates:
                    txns.append(make_txn(token, d, "ENT", "Gambling & Betting (expense)",
                                         round(max(per * random.uniform(.3, 2.2), 50), 2),
                                         "debit", "observed_external", regime))

        # ---- general spend, ANCHORED TO INCOME
        # Draw the shape from real percentiles, then scale the month so that
        # total debits ~= monthly income * spend_ratio. Without this, a
        # NGN32k/month earner would be handed population-median amounts.
        # Pool is based on NOMINAL monthly income, not receipts-to-date.
        # People spend steadily through the month regardless of whether payday
        # has landed yet; using actual receipts makes the current month lumpy
        # purely because of the salary date relative to AS_OF.
        nominal = 0
        for i in p["income"]:
            amt = i["amount"]
            if (p.get("raise_month") and i["kind"] == "salary"
                    and ms.year == 2026 and ms.month >= p["raise_month"]):
                amt = p["raise_to"]
            nominal += amt * i.get("per_month", 1)
        nominal = nominal or p.get("fallback_income", 90_000)
        month_income = kobo(nominal)
        fixed = sum(int(t["amountKobo"]["$numberLong"]) for t in txns
                    if t["direction"] == "debit"
                    and t["date"]["$date"][:7] == ms.strftime("%Y-%m"))
        target = month_income * p["spend_ratio"] * season * frac
        budget_pool = max(target - fixed, target * 0.25)

        n = int(round(p["density"] * frac * season))
        bcats = list(p["budget"] or [])
        pool_naira = budget_pool / 100.0

        def emit(d, code, amt):
            sub = random.choice(SUBCATS.get(code) or ["Uncategorized"])
            merch = None
            for ccode, csub, cmerch, prob in p.get("concentrate", []):
                if code == ccode and random.random() < prob:
                    sub, merch = csub, cmerch
            txns.append(make_txn(token, d, code, sub, max(round(amt, 2), 20.0), "debit",
                                 "observed_external" if random.random() < ext_p else "moniger",
                                 regime, merchant=merch))

        # BUDGETED categories get a CONTROLLED monthly total (low variance).
        # Random per-transaction category assignment produces monthly swings
        # far too large for a budget allocation to track, which makes the
        # designed on-track / at-risk / over status unreproducible.
        # Realistic-regime personas must keep production's plumbing dominance
        # (TRF/FEE/OTH/SAVE ~55% of transactions). Giving budgeted categories
        # the same share as target personas would erase that character.
        budgeted_share = {"realistic": 0.26, "mixed": 0.44}.get(regime, 0.62) if bcats else 0.0
        n_budgeted = int(round(n * budgeted_share))
        for code in bcats:
            if code == "HOU":          # rent already emitted as a fixed line
                continue
            share  = budgeted_share / len(bcats)
            target = pool_naira * share * random.uniform(0.94, 1.06)
            k      = max(1, int(round(n_budgeted / len(bcats))))
            ds     = dates_in(ms, k, AS_OF)
            if not ds:
                continue
            wts = [random.uniform(0.5, 1.6) for _ in ds]
            tot = sum(wts)
            for d, w in zip(ds, wts):
                emit(d, code, target * w / tot)

        # Remaining pool spreads across NON-budgeted categories at random.
        rest_codes = [c for c in codes if c not in bcats] or codes
        rest_w     = [mix[c] for c in rest_codes]
        rest_pool  = pool_naira * (1 - budgeted_share)
        ds = dates_in(ms, max(1, n - n_budgeted), AS_OF)
        if ds:
            draft = [(d, random.choices(rest_codes, rest_w)[0]) for d in ds]
            raws  = [amount_for(c) for _, c in draft]
            scale = rest_pool / (sum(raws) or 1)
            for (d, c), r in zip(draft, raws):
                emit(d, c, r * scale)

    txns.sort(key=lambda t: t["date"]["$date"])
    return txns

# ---------------------------------------------------------------- BUDGETS
def gen_budgets(token, p, txns):
    """
    Budget allocations are DERIVED from each persona's realised spend:
        allocation = median full-month spend on that category / budget_util
    so the designed end-of-month status (on-track / at-risk / over) is
    guaranteed. This is also realistic - users set budgets against their own
    recent spending, accurately or otherwise.
    """
    if not p["budget"]:
        return []
    regime = p["regime"]
    n_sub  = 1 if regime == "realistic" else random.choice([2, 3])
    starts = month_starts(p["history"], AS_OF)
    cur_ym = AS_OF.strftime("%Y-%m")

    # realised spend per (month, category)
    spend = defaultdict(int); earned = defaultdict(int)
    for t in txns:
        if t["status"] != "success":
            continue
        ym = t["date"]["$date"][:7]
        v  = int(t["amountKobo"]["$numberLong"])
        if t["direction"] == "debit":
            spend[(ym, t["fiCategoryCode"])] += v
        else:
            earned[ym] += v

    # allocation from median of COMPLETE months only
    # Allocations track RECENT spending (last 3 complete months), not the whole
    # history - people budget against what they have just been spending. Using
    # the full window would leave the mid-year-raise persona with a budget set
    # against their old salary, showing an implausible 200%+ utilisation.
    recent = [ms for ms in starts if ms.strftime("%Y-%m") != cur_ym][-3:]
    alloc = {}
    for code in p["budget"]:
        vals = sorted(spend.get((ms.strftime("%Y-%m"), code), 0) for ms in recent)
        vals = [v for v in vals if v > 0]
        med  = vals[len(vals)//2] if vals else kobo(20_000)
        a    = int(med / max(p["budget_util"], 0.05))
        alloc[code] = max(int(round(a / 100_000.0)) * 100_000, kobo(1_000))  # round to N1,000

    declared = sum(i["amount"] for i in p["income"] if i["kind"] == "salary")
    if not declared:
        declared = int(sum(earned.values()) / max(len(earned), 1) / 100)

    docs = []
    for ms in starts:
        ym, me = ms.strftime("%Y-%m"), month_end(ms)
        eb, tot_a, tot_s = [], 0, 0
        for code in p["budget"]:
            a   = alloc[code]
            csp = spend.get((ym, code), 0)
            subs = (SUBCATS.get(code) or ["Uncategorized"])[:n_sub]
            rows, ra, rs = [], a, csp
            for i, sname in enumerate(subs):
                last = (i == len(subs) - 1)
                av = ra if last else int(a   * (0.6 if i == 0 else 0.25))
                sv = rs if last else int(csp * (0.6 if i == 0 else 0.25))
                ra -= av; rs -= sv
                rows.append({"_id": oid(), "name": sname,
                             "icon": f"icon_{code.lower()}_{i}",
                             "allocatedAmount": xlong(av),
                             "spentAmount": xlong(max(sv, 0))})
            eb.append({"category_id": CAT_IDS[code],
                       "category_name": CATEGORY_NAME[code],
                       "total_allocated": xlong(a), "subcategories": rows})
            tot_a += a; tot_s += csp

        inc_k = kobo(declared)
        docs.append({
            "_id": {"$oid": oid()}, "srcId": {"$oid": oid()}, "userToken": token,
            "budgetMonth": xdate(ms), "periodStart": xdate(ms), "periodEnd": xdate(me),
            "status": "active", "applyToFutureMonths": True,
            "totalMonthlyIncome": xlong(inc_k), "totalMonthlyExpense": xlong(tot_a),
            "spentAmount": xlong(tot_s), "remainingAmount": xlong(tot_a - tot_s),
            "expenseBreakdown": eb,
            "incomeBreakdown": [{
                "category_id": CAT_IDS["INCM"], "category_name": CATEGORY_NAME["INCM"],
                "total_allocated": xlong(inc_k),
                "subcategories": [{"_id": oid(), "name": "Salary & Wages",
                                   "icon": "icon_incm_0",
                                   "allocatedAmount": xlong(inc_k),
                                   "earnedAmount": xlong(earned.get(ym, 0)),
                                   "spentAmount": xlong(0)}]}]})
    return docs

# ---------------------------------------------------------------- POISONED
def gen_poisoned():
    base = dict(date=xdate(AS_OF - timedelta(days=3)), amountKobo=xlong(kobo(12_500)),
                direction="debit", status="success", categoryId=CAT_IDS["GROC"],
                categoryName="Groceries", fiCategory="Groceries", fiCategoryCode="GROC",
                fiSubcategory="Supermarkets & Hypermarkets", fiConfidence="High",
                fiClassificationStage="3", fiIsRecurring=False, channel="opay",
                moneySource="moniger", isBillPayment=False, isWalletToWallet=False,
                isExternalTransfer=False)
    cases = [
      ("POISON-01 un-tokenised personal name in merchantLabel",
       dict(userToken="usr_anon_p0is0n", merchantLabel="ADEBAYO OKONKWO")),
      ("POISON-02 10-digit account number in merchantLabel",
       dict(userToken="usr_anon_p0is0n", merchantLabel="TRF TO 0123456789")),
      ("POISON-03 11-digit BVN-shaped value present",
       dict(userToken="usr_anon_p0is0n", merchantLabel="SHOPRITE", bvn="22000000021")),
      ("POISON-04 raw ObjectId as userToken",
       dict(userToken="6a285f12c1dd1fd52817f913", merchantLabel="SHOPRITE")),
      ("POISON-05 email address present",
       dict(userToken="usr_anon_p0is0n", merchantLabel="JUMIA",
            contact="chidinma.eze@example.com")),
      ("POISON-06 raw narration leaked from source",
       dict(userToken="usr_anon_p0is0n", merchantLabel="TRANSFER",
            narration="NIP TRF FROM CHIDINMA EZE 0123456789 TO MONIGER WALLET")),
      ("POISON-07 phone number present",
       dict(userToken="usr_anon_p0is0n", merchantLabel="MTN AIRTIME 08031234567")),
      ("POISON-08 paymentDetails subtree not dropped at ingest",
       dict(userToken="usr_anon_p0is0n", merchantLabel="SHOPRITE",
            paymentDetails={"accountName":"CHIDINMA EZE","accountNumber":"0123456789"})),
    ]
    return [{"_id": {"$oid": oid()}, "srcId": {"$oid": oid()}, "_testCase": lbl,
             **base, **extra} for lbl, extra in cases]

# ---------------------------------------------------------------- MAIN
os.makedirs(OUT_DIR, exist_ok=True)
all_t, all_b, summary = [], [], []

for tok, p in PERSONAS.items():
    t = gen_persona(tok, p)
    b = gen_budgets(tok, p, t)
    all_t += t; all_b += b
    dr = sum(int(x["amountKobo"]["$numberLong"]) for x in t if x["direction"]=="debit")
    cr = sum(int(x["amountKobo"]["$numberLong"]) for x in t if x["direction"]=="credit")
    cats = len({x["fiCategoryCode"] for x in t if x["direction"]=="debit"})
    thin = sum(1 for x in t if x["fiCategoryCode"] in ("TRF","FEE","OTH","SAVE")) / max(len(t),1)
    summary.append((tok, p["label"], p["regime"], len(t), len(b),
                    cr/100, dr/100, cats, thin))

pois = gen_poisoned()

def write(path, docs):
    with open(path, "w") as f:
        for d in docs: f.write(json.dumps(d) + "\n")

write(f"{OUT_DIR}/copilot_transactions.jsonl", all_t)
write(f"{OUT_DIR}/copilot_budgets.jsonl", all_b)
write(f"{OUT_DIR}/copilot_transactions_POISONED.jsonl", pois)

print(f"AS_OF {AS_OF.date()}   history {MONTHS_HISTORY} months\n")
print(f"{'TOKEN':<18}{'REG':<10}{'TXN':>5}{'BUD':>5}{'CREDITS':>13}{'DEBITS':>13}{'CAT':>5}{'PLUMB':>7}  LABEL")
print("-"*138)
for tok,lbl,reg,nt,nb,cr,dr,cats,thin in summary:
    print(f"{tok:<18}{reg:<10}{nt:>5}{nb:>5}{cr:>13,.0f}{dr:>13,.0f}{cats:>5}{thin:>6.0%}  {lbl}")
print("-"*138)
print(f"{'TOTAL':<18}{'':<10}{len(all_t):>5}{len(all_b):>5}")
print(f"\nPoisoned docs: {len(pois)} (separate file)")
