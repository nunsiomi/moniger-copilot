"""Tax-specific system prompt addendum — Nigeria Tax Act 2025 rules.

This is appended to BASE_SYSTEM_PROMPT (see prompt_builder.py) whenever the
classifier routes a question to the TAX category.

The Nigeria Tax Act 2025 (one of four harmonisation Acts signed in 2025,
effective 1 January 2026) overhauled personal income tax: a higher
tax-free threshold, new progressive bands, the old Consolidated Relief
Allowance replaced by rent relief, the old minimum-tax floor removed, a
worldwide-income rule for residents, and the Nigeria Revenue Service (NRS)
replacing FIRS as the tax authority. The figures below encode that reform.
They're the widely-reported headline figures, not a substitute for the
final gazetted text — if the gazetted Act or NRS implementation guidance
ever diverges from what's encoded here, this file should be updated to
match, since app.context_builders.tax and this prompt are the *only*
source of truth the model is allowed to use (see the instruction below
against falling back on training-data knowledge of Nigerian tax law).
"""

# The first slice of chargeable income taxed at 0% under the 2025 reform.
LOW_INCOME_EXEMPTION_THRESHOLD_NGN: float = 800_000

# Progressive personal income tax bands (Nigeria Tax Act 2025). Each band's
# rate applies only to the slice of chargeable income within that band, not
# the whole amount — see app.context_builders.tax._determine_tier, which
# expects exactly this shape. `max: None` marks the open-ended top band.
PERSONAL_INCOME_TAX_BANDS_NGN: list[dict[str, float | str | None]] = [
    {"min": 0, "max": LOW_INCOME_EXEMPTION_THRESHOLD_NGN, "rate": 0.0, "label": "0%"},
    {"min": LOW_INCOME_EXEMPTION_THRESHOLD_NGN, "max": 3_000_000, "rate": 0.15, "label": "15%"},
    {"min": 3_000_000, "max": 7_000_000, "rate": 0.18, "label": "18%"},
    {"min": 7_000_000, "max": 12_000_000, "rate": 0.21, "label": "21%"},
    {"min": 12_000_000, "max": 20_000_000, "rate": 0.23, "label": "23%"},
    {"min": 20_000_000, "max": None, "rate": 0.25, "label": "25%"},
]

# The old Consolidated Relief Allowance (CRA) is abolished under the 2025
# reform. Rent relief is the one relief left: a percentage of annual rent
# paid, deducted from gross annual income to reach chargeable income,
# capped at a fixed naira ceiling.
RENT_RELIEF_RATE: float = 0.20
RENT_RELIEF_CAP_NGN: float = 500_000

# Late-filing penalties for a personal income tax return under the reform.
LATE_FILING_PENALTY_FIRST_MONTH_NGN = 100_000
LATE_FILING_PENALTY_SUBSEQUENT_MONTH_NGN = 50_000

# The Nigeria Revenue Service (NRS) replaced the Federal Inland Revenue
# Service (FIRS) as the tax authority under the reform — the prompt below
# refers to this name, never FIRS.
TAX_AUTHORITY_NAME = "Nigeria Revenue Service (NRS)"


def _format_ngn(amount: float) -> str:
    return f"₦{amount:,.0f}"


def _band_description_lines() -> str:
    lines = []
    for band in PERSONAL_INCOME_TAX_BANDS_NGN:
        lower = _format_ngn(band["min"])
        if band["max"] is None:
            span = f"Above {lower}"
        else:
            span = f"{lower} to {_format_ngn(band['max'])}"
        lines.append(f"  - {span}: {band['rate'] * 100:.0f}%")
    return "\n".join(lines)


# The arithmetic itself lives in app.context_builders.tax.compute_tax; the
# model presents tax_computation's figures. It must still use only the
# tax_bands /
# rent_relief / low_income_exemption_threshold values provided in
# <context> (app.context_builders.tax) — never from its own training-data
# knowledge of Nigerian tax law, since that knowledge predates the 2025
# reform and would silently apply the wrong bands.
TAX_SYSTEM_PROMPT = f"""\
The user is asking about personal income tax under the Nigeria Tax Act \
2025 (effective 1 January 2026), which replaced the old Personal Income \
Tax Act (PITA) regime. Compute strictly from the tax_bands, \
rent_relief_estimate_naira, chargeable_income_naira, and \
low_income_exemption_threshold_naira fields already provided in <context> \
— never from your own training-data \
knowledge of Nigerian tax law or the old PITA bands/reliefs, since the \
2025 reform changed both.

Key rules of the 2025 reform:
- The first {_format_ngn(LOW_INCOME_EXEMPTION_THRESHOLD_NGN)} of annual \
chargeable income is tax-exempt (the 0% band below). If the context's \
chargeable_income_naira is at or under this threshold, the user owes no \
personal income tax at all — say so plainly rather than walking through \
the bands.
- Above that threshold, personal income tax is progressive across these \
bands. Each rate applies only to the slice of chargeable income that \
falls within that band, not to the whole amount:
{_band_description_lines()}
- The old Consolidated Relief Allowance (CRA) has been abolished. The \
only relief now available is rent relief: {RENT_RELIEF_RATE * 100:.0f}% \
of annual rent paid, capped at {_format_ngn(RENT_RELIEF_CAP_NGN)}, \
deducted from gross annual income to reach chargeable_income_naira — this \
deduction is already applied in the context's chargeable_income_naira \
figure, so do not subtract it again.
- The minimum-tax floor that used to apply regardless of taxable profit \
has been removed.
- Nigerian tax residents are now taxed on worldwide income, not only \
income sourced within Nigeria — if the user's context includes income \
from outside Nigeria, treat it the same as any other income rather than \
excluding it.
- If asked about filing deadlines or penalties: late filing of a return \
is penalised at {_format_ngn(LATE_FILING_PENALTY_FIRST_MONTH_NGN)} for \
the first month of default and {_format_ngn(LATE_FILING_PENALTY_SUBSEQUENT_MONTH_NGN)} \
for every subsequent month.
- The tax authority is the {TAX_AUTHORITY_NAME}, which replaced the \
Federal Inland Revenue Service (FIRS) — refer to it by this name, not FIRS.

The band-by-band calculation is already done for you in the context's \
tax_computation field: each band's taxable slice and tax, \
total_annual_tax_naira, monthly_tax_naira and effective_rate_percent. Use \
those figures exactly - never recompute them, and never apply the top \
marginal rate to the whole amount. When giving an estimate, itemise it: \
the estimated annual income and the period it is based on \
(income_estimate_method, income_date_range), any rent relief, the \
chargeable income, then the tax from each band that applies, then the \
**total**. If has_full_year_of_history is false, say the estimate is \
extrapolated from fewer than 12 months of income and may change.

If the user asks how much tax they have paid or spent, explain that \
Moniger doesn't see tax payments as a transaction category - PAYE is \
usually deducted by an employer before salary arrives - and offer the \
estimated liability from tax_computation instead.

State plainly that this is an estimate for informational purposes, not a filed \
return, and that the user should confirm anything beyond a rough estimate \
with the {TAX_AUTHORITY_NAME} or a qualified tax professional.\
"""
