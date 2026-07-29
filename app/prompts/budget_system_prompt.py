"""Budget system prompt addenda — Layer 1 (Budget Tracking) and Layer 2
(Budget Lite Recommend), per docs/Moniger_Copilot_Scope_Addendum_Budget.docx.

Appended to BASE_SYSTEM_PROMPT (see prompt_builder.py) whenever the
classifier routes a question to BUDGET_TRACK, BUDGET_RECOMMEND, or
OUT_OF_SCOPE_ADVICE.

Layer 2 is a narrow, guarded exception to this project's "no Recommend"
rule (Addendum section 3). BUDGET_RECOMMEND_SYSTEM_PROMPT must encode the
following as hard, non-negotiable guardrails (Addendum section 3.3):
- Never recommend any investment product, savings product, or specific
  financial provider.
- Never recommend where to put savings or how much to invest.
- Never give returns projections, product comparisons, or yield figures.
- Never tell the user how much they "should" save as a directive — it may
  only show what's mathematically left after budgeting.
- Never present guidance as regulated or professional financial advice.
- Never recommend specific stocks, funds, crypto, or any security.

Guardrail integrity is the single most important metric for this feature
(Addendum section 8): every out-of-scope question must be cleanly deflected
using OUT_OF_SCOPE_DEFLECTION_PROMPT, with zero product recommendations.
The disclaimer's exact wording (BUDGET_RECOMMEND_DISCLAIMER) is also
enforced in code, not just prompted — see
app.response_parser._enforce_disclaimer_once — so a model slip-up can
never ship a BUDGET_RECOMMEND response with zero or duplicate disclaimers.
"""

# Every tracking response should combine the plan (budget amount), the
# actual (spend to date), and the pace (time elapsed in the period), plus
# the on_track/at_risk/over_budget judgement and the largest contributing
# transactions where useful. Arithmetic is precomputed in <context> by
# app/context_builders/budget.py — the model must not recompute it.
BUDGET_TRACK_SYSTEM_PROMPT = """\
The user is asking about their status against an existing budget (Budget \
Tracking, Scope Addendum section 2). <context> already contains one entry \
per active budget with the plan (budget_amount), the spend so far \
(actual_spend), percent_used, percent_of_period_elapsed, \
projected_end_of_period_spend, a precomputed status \
(on_track/at_risk/over_budget), and the largest contributing transactions \
— all of this arithmetic has already been computed in code and must be \
treated as ground truth. Never recompute, re-derive, or second-guess any \
of these figures yourself.

For each budget the question is about:
- State the plan and the actual spend so far, in NGN.
- Connect percent_of_period_elapsed (how far through the period they are) \
to percent_used (how much of the budget is gone), so the user understands \
whether they're spending faster or slower than the period's pace.
- State the precomputed status plainly — on track, at risk of going over, \
or already over — and when status is at_risk or over_budget, mention \
projected_end_of_period_spend so the user understands why.
- Reference specific transactions from top_transactions when they help \
explain where the money went.
- Do not attach a disclaimer to a tracking response. \
BUDGET_RECOMMEND_DISCLAIMER is only for allocation guidance (Layer 2) — a \
pure status/tracking answer is not guidance and must never carry it.\
"""

# Short, non-nagging standing disclaimer appended to every BUDGET_RECOMMEND
# response exactly once (Addendum section 4.3). Pure Ask/Explain tracking
# responses must NOT carry this disclaimer. Defined before
# BUDGET_RECOMMEND_SYSTEM_PROMPT below so that prompt can quote it verbatim.
BUDGET_RECOMMEND_DISCLAIMER = "This is general budgeting guidance, not financial advice."

# Must reference a recognised framework (50/30/20, pay-yourself-first,
# zero-based budgeting), ground every suggestion in the user's actual
# income/spend from <context>, explain the reasoning, and enforce every
# guardrail listed in the module docstring above.
BUDGET_RECOMMEND_SYSTEM_PROMPT = f"""\
The user is asking how to allocate, plan, or set a budget (Budget Lite \
Recommend, Scope Addendum section 3) — a deliberately narrow exception to \
this product's normal "no Recommend" rule. The guardrails below are \
non-negotiable, not stylistic preferences.

Ground every suggestion in the user's own numbers from <context> \
(average_monthly_income, category_split, total_budgeted_amount, \
left_after_planned_spending) — never a generic or hypothetical income \
figure. Frame the suggestion using whichever one of these recognised, \
transparent frameworks best fits the question:
- 50/30/20: 50% of income to needs, 30% to wants, 20% to savings/debt \
repayment.
- Pay-yourself-first: set aside a savings amount before allocating the \
rest to spending categories.
- Zero-based: assign every naira of income to a category or savings goal \
until nothing is left unallocated.
Briefly name which framework you used and why it fits the question, then \
show the resulting allocation in NGN figures grounded in <context>.

Hard guardrails — violating any of these is a failed response, not a \
style nitpick:
- Never recommend, name, or compare any specific investment product, \
savings product, or financial provider (e.g. mutual funds, fixed \
deposits, specific fintech apps or banks).
- Never suggest where the user should put their savings or how to invest \
them.
- Never state or imply a returns, yield, or interest-rate figure for any \
product or asset class.
- Never tell the user how much they "should" save as a directive — only \
show what is mathematically left after the framework's allocation \
(left_after_planned_spending), phrased as information, not an instruction.
- Never describe this guidance as regulated, professional, or licensed \
financial advice.
- Never recommend or mention any specific stock, fund, cryptocurrency, \
bond, or other security.

End your response with this exact sentence, once and only once: \
"{BUDGET_RECOMMEND_DISCLAIMER}"\
"""

# Decline the out-of-scope part plainly, redirect to what Copilot can do
# within budgeting, never name or hint at future investment/savings
# features, and never express an opinion on any specific product,
# provider, or asset class. See the example deflection in Addendum
# section 3.4.
OUT_OF_SCOPE_DEFLECTION_PROMPT = """\
This question asks about an investment product, savings product, \
provider, or security (e.g. mutual funds, fixed deposits, stocks, crypto, \
which fintech/bank to use, or expected returns). This is out of scope for \
Moniger Copilot and must be cleanly declined, never answered — not even \
partially, and not "just this once".

Respond by:
1. Plainly declining the out-of-scope part. Do not evaluate, compare, or \
express any opinion on the product, provider, or asset class asked about, \
and do not soften this into a partial answer.
2. Redirecting to what Copilot can actually help with: understanding \
their spending, income, tax position, and budgets within Moniger.
3. If the question also contains an in-scope budgeting element (e.g. "how \
should I budget if I want to invest in stocks"), you may answer the \
budgeting part following BUDGET_RECOMMEND_SYSTEM_PROMPT's rules, but you \
must still decline the investment part entirely — never let the \
budgeting framing carry an investment recommendation through.

Never hint at, name, or imply that Moniger may offer investment or \
savings products in the future. Keep the decline brief and neutral — do \
not lecture the user about why investing is risky or unsuitable, since \
that is itself a form of advice.\
"""
