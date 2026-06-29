"""Tax-specific system prompt addendum — Nigeria Tax Act 2025 rules.

This is appended to BASE_SYSTEM_PROMPT (see prompt_builder.py) whenever the
classifier routes a question to the TAX category.

IMPORTANT: the band thresholds and rates below are placeholders for the
scaffold and are marked TODO. Before this ships, replace them with the
actual gazetted figures from the Nigeria Tax Act 2025 (and the
accompanying Nigeria Tax Administration Act 2025) and have them signed off
by whoever owns tax-content accuracy on the team — getting this wrong is a
compliance risk, not just a UX bug.
"""

# TODO(tax-content-owner): confirm against the gazetted Nigeria Tax Act 2025.
PERSONAL_INCOME_TAX_BANDS_NGN: list[dict[str, float | str]] = [
    {"upper_bound": 800_000, "rate": 0.0, "label": "First ₦800,000 (tax-exempt threshold)"},
    {"upper_bound": 3_000_000, "rate": 0.15, "label": "Next ₦2,200,000"},
    {"upper_bound": 12_000_000, "rate": 0.18, "label": "Next ₦9,000,000"},
    {"upper_bound": 25_000_000, "rate": 0.21, "label": "Next ₦13,000,000"},
    {"upper_bound": 50_000_000, "rate": 0.23, "label": "Next ₦25,000,000"},
    {"upper_bound": None, "rate": 0.25, "label": "Above ₦50,000,000"},
]

# TODO(tax-content-owner): confirm rent-relief cap under the 2025 reform.
RENT_RELIEF_CAP_NGN = 500_000
RENT_RELIEF_RATE = 0.20  # 20% of annual rent paid, capped at the value above

TAX_SYSTEM_PROMPT = """\
You are also Moniger Copilot's tax assistant, answering questions about \
Nigerian personal income tax under the Nigeria Tax Act 2025.

Use the following rules when reasoning about tax questions:
- Personal income tax in Nigeria is progressive, charged in bands on \
annual chargeable income (income after allowable reliefs and deductions).
- The bands provided to you in <context> as `tax_bands` are the source of \
truth for current rates — always compute using those values, never from \
memory, since tax law changes between releases of this app.
- Rent relief reduces chargeable income by 20% of annual rent paid, capped \
at the `rent_relief_cap` figure in <context>.
- PAYE (Pay As You Earn) is the mechanism by which employers deduct income \
tax at source for employees; self-employed and freelance users remit \
directly.
- VAT (Value Added Tax) is a separate consumption tax and is not part of \
personal income tax calculations — do not conflate the two if a user asks \
about VAT on a purchase versus tax on their income.
- You are not a licensed tax advisor. For anything beyond a straightforward \
estimate from the user's own transaction data, tell the user to confirm \
with a qualified tax professional or the Federal Inland Revenue Service \
(FIRS) before filing or making payment decisions.
- Never state a specific rate or threshold unless it appears in the \
<context> block provided with this conversation.\
"""
