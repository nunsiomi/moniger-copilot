"""
Optional in-process mock, for validating the harness itself.

    MOCK_MODE=good pytest -q     # a correct Copilot  -> everything should pass
    MOCK_MODE=bad  pytest -q     # a plausible-but-wrong Copilot -> failures

Leave MOCK_MODE unset to test a real endpoint via COPILOT_API_URL.
"""
import os, json, re
import copilot_client

MODE = os.environ.get("MOCK_MODE")

if MODE:
    GT = json.load(open("ground_truth.json"))
    P, CUR = GT["personas"], GT["current_month"]
    MN = {"january":"01","february":"02","march":"03","april":"04","may":"05",
          "june":"06","july":"07","august":"08","september":"09","october":"10",
          "november":"11","december":"12"}
    CATW = {"food and drinks":"FND","groceries":"GROC","transport":"AUTO",
            "airtime and data":"AIR","shopping":"SHOP","utilities":"UTIL",
            "entertainment":"ENT","subscriptions":"SUB"}

    def money(v): return f"\u20a6{v:,.2f}"

    def month_in(q):
        ql = q.lower()
        for name, num in MN.items():
            if name in ql:
                y = re.search(r"(20\d\d)", q)
                return f"{y.group(1) if y else '2026'}-{num}"
        return None

    def answer(q, tok):
        d = P.get(tok, {}); ql = q.lower()
        half = 0.5 if MODE == "bad" else 1.0

        if any(w in ql for w in ["mutual fund","stocks","crypto","rise vest","bamboo",
                                 "piggyvest","savings product","best returns",
                                 "what should i do with it","etf"]):
            if MODE == "bad":
                return "I recommend Rise Vest for this - it is a good place to put your money."
            return ("I can't advise on specific savings or investment products - that's "
                    "outside what I help with. What I can do is help you work out how much "
                    "room your budget leaves each month. Want me to do that?")

        if "how should i budget" in ql:
            inc = d.get("avg_monthly_income_naira", 0)
            if MODE == "bad":
                return "Use the 50/30/20 rule."
            return (f"Based on your average monthly income of {money(inc)}, a common "
                    f"starting point is the 50/30/20 guideline: about {money(inc*0.5)} for "
                    f"needs, {money(inc*0.3)} for wants and {money(inc*0.2)} set aside. "
                    f"This is general budgeting guidance, not financial advice.")

        if "threshold" in ql:
            return "The tax-free threshold under the Nigeria Tax Act 2025 is \u20a6800,000 per year."
        if "tax law affect me" in ql:
            if MODE == "bad":
                return "The new tax law introduces progressive bands from 0% to 25%."
            ann = d.get("annual_income_naira", 0)
            if d.get("tax_exempt_under_800k"):
                return (f"Your income is about {money(ann)} a year, below the \u20a6800,000 "
                        f"tax-free threshold, so you are exempt from personal income tax.")
            return (f"Your income is about {money(ann)} a year, above the \u20a6800,000 "
                    f"tax-free threshold, so you are liable under the progressive bands.")
        if "annual income" in ql:
            v = d.get("trailing_12_income_naira") or d.get("annual_income_naira", 0)
            if MODE == "bad" and tok == "usr_anon_t05rse":
                return f"Annualising your latest month, that's about {money(480_000*12)}."
            return f"Over the last 12 complete months you earned {money(v)}."
        if "how is my tax calculated" in ql:
            return ("Tax uses progressive bands from 0% to 25%. The Consolidated Relief "
                    "Allowance was abolished under the 2025 Act and no longer applies.")

        if "earn what i planned" in ql or "did i earn" in ql:
            ib = d.get("previous_month_income_budget")
            if ib:
                return (f"You earned {money(ib['earned_so_far_naira'])} against a plan of "
                        f"{money(ib['planned_naira'])} ({ib['percent_earned']}%).")

        if "budget" in ql:
            cs = d.get("current_month_budget_status") or {}
            if not cs:
                return "You haven't set a budget yet. Want help setting one up?"
            for cat, s in cs.items():
                if cat.lower() in ql or cat.split(" &")[0].lower() in ql:
                    if MODE == "bad":
                        return f"You have exceeded your {cat} budget - you are over budget."
                    verb = "over your" if s["status"] == "over_budget" else "within your"
                    return (f"You've used {money(s['spent_naira'])} of your "
                            f"{money(s['allocated_naira'])} {cat} budget "
                            f"({s['percent_used']}%), and we're "
                            f"{d['percent_of_current_month_elapsed']}% through the month. "
                            f"That puts you {verb} budget, with "
                            f"{money(s['remaining_naira'])} left.")
            for key, s in (d.get("current_month_subcategory_status") or {}).items():
                sub = key.split("|")[1].strip()
                if sub.lower().split()[0] in ql:
                    return (f"You have {money(s['remaining_naira'])} left in your {sub} "
                            f"budget ({s['percent_used']}% used).")
            for c in ["groceries","food","transport","shopping","entertainment"]:
                if c in ql and not any(c in x.lower() for x in cs):
                    return f"You haven't set a budget for {c}, so there's nothing to track against."
            s = list(cs.values())[0]
            return (f"You're {s['percent_used']}% through your budgets and "
                    f"{d['percent_of_current_month_elapsed']}% through the month so far.")

        m = re.search(r"spent at ([A-Z0-9 &'\-\.]+) in", q)
        if m:
            name = m.group(1).strip()
            return (f"You've spent "
                    f"{money((d.get('top_merchants_2026_naira') or {}).get(name,0)*half)} "
                    f"at {name} in 2026.")

        if "compare to" in ql:
            mm = month_in(q); yoy = d.get("year_over_year") or {}
            if mm and mm in yoy:
                v = yoy[mm]
                return (f"You spent {money(v['spend_naira'])} then, versus "
                        f"{money(v['prev_spend_naira'])} the year before.")

        if "average monthly income" in ql:
            return f"Your average monthly income is {money(d.get('avg_monthly_income_naira',0)*half)}."

        mm = month_in(q)
        if mm:
            for w, code in CATW.items():
                if w in ql:
                    v = (d.get("spend_by_month_category_naira") or {}).get(f"{mm}|{code}")
                    if v: return f"You spent {money(v*half)} on {w} in that month."
                    return f"I don't see any {w} transactions categorised in that month."
            for key, v in (d.get("spend_by_month_subcategory_naira") or {}).items():
                km, _, sub = key.split("|")
                if km == mm and sub.lower()[:12] in ql:
                    return f"You spent {money(v*half)} on {sub} in that month."
            if "total" in ql or "spending in" in ql:
                v = (d.get("spend_by_month_naira") or {}).get(mm, 0)
                return f"Your total spending that month was {money(v*half)}."
            if "pet" in ql:
                return "I don't see any pet-related transactions in that month."

        if "last month" in ql:
            if d.get("transaction_count", 0) <= 3:
                return "You've only got a few transactions so far, so there isn't enough to work with yet."
            return "I don't see any transactions categorised that way, so there's nothing to report."
        if "income over the last" in ql:
            return "Your income has been uneven - there were two months with no income recorded."
        if "weather" in ql:
            return "I can't help with the weather - I only cover your spending and money questions."
        if "how am i doing" in ql:
            return "Overall you're close to your usual spending this month. Want a category breakdown?"
        if "betting" in ql:
            v = sum(v for k, v in (d.get("spend_by_month_category_naira") or {}).items()
                    if k.endswith("|ENT") and k.startswith("2026"))
            return f"You've spent {money(v)} in the entertainment category so far in 2026."
        if any(w in ql for w in ["transfer money to","recent transactions","biggest expenses"]):
            tops = list((d.get("top_merchants_naira") or {}).items())[:3]
            return "Your largest merchants are " + ", ".join(f"{n} ({money(v)})" for n, v in tops) + "."
        return "I don't have enough information to answer that."

    copilot_client.ask = lambda q, tok, retries=0: \
        copilot_client.CopilotResponse({"answer": answer(q, tok), "confidence": 0.9})
    copilot_client.configured = lambda: True
