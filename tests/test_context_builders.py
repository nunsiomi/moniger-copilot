"""Validates every context builder directly against
moniger_copilot_fixtures_v2 + its own ground_truth.py output — bypassing
MongoDB, the classifier, and the LLM entirely, so a schema/arithmetic bug
is caught at the unit level rather than only showing up as a failure deep
in moniger_copilot_tests' live API suite (which needs a running API,
Mongo, Redis, and a real GROQ_API_KEY — see that suite's own README).

Regenerate ground_truth.json before running this file if the fixtures
change:
    cd moniger_copilot_tests && python ground_truth.py
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from app.context_builders import budget, comparison, income, merchant, spending, tax
from app.data_access import _decode_document

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES_DIR = _REPO_ROOT / "moniger_copilot_fixtures_v2"
_TESTS_DIR = _REPO_ROOT / "moniger_copilot_tests"
_AS_OF = date(2026, 7, 20)  # matches ground_truth.py's hardcoded AS_OF


def _load_jsonl(name: str) -> list[dict]:
    with open(_FIXTURES_DIR / name, encoding="utf-8") as f:
        return [_decode_document(json.loads(line)) for line in f if line.strip()]


if not (_TESTS_DIR / "ground_truth.json").exists():
    pytest.skip(
        "ground_truth.json not generated -- run "
        "`cd moniger_copilot_tests && python ground_truth.py` first",
        allow_module_level=True,
    )

_ALL_TXNS = _load_jsonl("copilot_transactions.jsonl")
_ALL_BUDGETS = _load_jsonl("copilot_budgets.jsonl")
with open(_TESTS_DIR / "ground_truth.json", encoding="utf-8") as f:
    _GT = json.load(f)["personas"]


def _txns_for(user_token: str) -> list[dict]:
    return [t for t in _ALL_TXNS if t["userToken"] == user_token and t.get("status") == "success"]


def _budgets_for(user_token: str) -> list[dict]:
    return [b for b in _ALL_BUDGETS if b["userToken"] == user_token]


def _close(actual: float | None, expected: float | None, tol: float = 0.02) -> bool:
    if actual is None or expected is None:
        return actual == expected
    return abs(actual - expected) <= max(abs(expected) * tol, 0.01)


# ---------------------------------------------------------------------------
# income.py / tax.py -- the tax-tier ladder and the raise persona
# ---------------------------------------------------------------------------

_TAX_LADDER_TOKENS = [
    "usr_anon_t01wbl", "usr_anon_t02jbl", "usr_anon_t03jab",
    "usr_anon_t04cab", "usr_anon_t05rse", "usr_anon_t06usd",
]


@pytest.mark.parametrize("token", _TAX_LADDER_TOKENS)
def test_income_annual_estimate_matches_ground_truth(token):
    result = income.build(_txns_for(token))
    expected = _GT[token]
    assert _close(result["average_monthly_income_naira"], expected["avg_monthly_income_naira"])
    assert _close(result["estimated_annual_income_naira"], expected["annual_income_naira"])


def test_tax_pair_discriminates_exemption():
    """T02 (NGN768,000) and T03 (NGN840,000) sit either side of the
    NGN800,000 threshold -- the sharpest test in the fixture set."""
    below = tax.build(_txns_for("usr_anon_t02jbl"))
    above = tax.build(_txns_for("usr_anon_t03jab"))
    assert below["is_below_exemption_threshold"] is True
    assert above["is_below_exemption_threshold"] is False


def test_raise_persona_is_not_naively_annualised():
    """T05 was raised NGN120k -> NGN480k in April. Naively annualising the
    latest month gives ~NGN5.76m; the real trailing-12 sum is ~NGN2.52m."""
    result = income.build(_txns_for("usr_anon_t05rse"))
    naive = 480_000 * 12
    assert result["has_full_year_of_history"] is True
    assert not _close(result["estimated_annual_income_naira"], naive, tol=0.05)
    assert _close(result["estimated_annual_income_naira"], _GT["usr_anon_t05rse"]["annual_income_naira"])


# ---------------------------------------------------------------------------
# spending.py -- monthly/category/subcategory breakdowns
# ---------------------------------------------------------------------------

_SPENDING_TOKENS = ["usr_anon_i07sal", "usr_anon_t02jbl", "usr_anon_g18ovr", "usr_anon_b11sea"]


@pytest.mark.parametrize("token", _SPENDING_TOKENS)
def test_spending_monthly_totals_match_ground_truth(token):
    result = spending.build(_txns_for(token))
    monthly = {entry["month"]: entry for entry in result["monthly_breakdown"]}
    expected = _GT[token]

    for month, expected_total in expected["spend_by_month_naira"].items():
        assert _close(monthly.get(month, {}).get("total_spend_naira"), expected_total), month

    for key, expected_total in expected["spend_by_month_category_naira"].items():
        month, code = key.split("|")
        breakdown = {c["category_code"]: c["total_naira"] for c in monthly.get(month, {}).get("category_breakdown", [])}
        assert _close(breakdown.get(code), expected_total), key


def test_subcategory_breakdown_matches_ground_truth():
    result = spending.build(_txns_for("usr_anon_i07sal"))
    monthly = {entry["month"]: entry for entry in result["monthly_breakdown"]}
    expected = _GT["usr_anon_i07sal"]["spend_by_month_subcategory_naira"]

    for key, expected_total in expected.items():
        month, code, subcategory = key.split("|")
        breakdown = {
            (s["category_code"], s["subcategory"]): s["total_naira"]
            for s in monthly.get(month, {}).get("subcategory_breakdown", [])
        }
        assert _close(breakdown.get((code, subcategory)), expected_total), key


# ---------------------------------------------------------------------------
# merchant.py -- merchant-concentrated persona
# ---------------------------------------------------------------------------

def test_merchant_totals_match_ground_truth():
    result = merchant.build(_txns_for("usr_anon_b12mch"))
    expected = _GT["usr_anon_b12mch"]

    all_time = {m["merchant"]: m["total_naira"] for m in result["merchant_breakdown"]}
    for name, expected_total in expected["top_merchants_naira"].items():
        assert _close(all_time.get(name), expected_total), name

    current_year = {m["merchant"]: m["total_naira"] for m in result["top_merchants_current_year"]}
    for name, expected_total in expected["top_merchants_2026_naira"].items():
        assert _close(current_year.get(name), expected_total), name


# ---------------------------------------------------------------------------
# comparison.py -- monthly totals and a year-over-year pair
# ---------------------------------------------------------------------------

def test_comparison_monthly_totals_match_ground_truth():
    result = comparison.build(_txns_for("usr_anon_b11sea"))
    monthly = {entry["month"]: entry for entry in result["monthly_totals"]}
    expected = _GT["usr_anon_b11sea"]

    for month, expected_total in expected["spend_by_month_naira"].items():
        assert _close(monthly.get(month, {}).get("spend_naira"), expected_total), month
    for month, expected_total in expected["income_by_month_naira"].items():
        assert _close(monthly.get(month, {}).get("income_naira"), expected_total), month


def test_comparison_resolves_a_year_over_year_pair():
    """B11 has 20 months of history -- two Decembers present."""
    result = comparison.build(_txns_for("usr_anon_b11sea"))
    monthly = {entry["month"]: entry for entry in result["monthly_totals"]}
    yoy = _GT["usr_anon_b11sea"]["year_over_year"]
    december_pairs = [m for m in yoy if m.endswith("-12")]
    assert december_pairs, "fixture should have at least one December year-over-year pair"

    month = december_pairs[0]
    expected = yoy[month]
    assert _close(monthly[month]["spend_naira"], expected["spend_naira"])
    assert _close(monthly[expected["vs"]]["spend_naira"], expected["prev_spend_naira"])


# ---------------------------------------------------------------------------
# budget.py -- category, subcategory, and income budget status
# ---------------------------------------------------------------------------

_BUDGET_TOKENS = [
    "usr_anon_g16trk", "usr_anon_g17rsk", "usr_anon_g18ovr", "usr_anon_g19rnt",
    "usr_anon_g15par", "usr_anon_i07sal", "usr_anon_t04cab", "usr_anon_b13sub",
]


@pytest.mark.parametrize("token", _BUDGET_TOKENS)
def test_budget_tracking_matches_ground_truth(token):
    result = budget.build_tracking(_txns_for(token), _budgets_for(token), _AS_OF)
    expected = _GT[token]

    assert result["has_budget"] == expected["has_budget"]
    assert _close(result["percent_of_current_month_elapsed"], expected["percent_of_current_month_elapsed"], tol=0.01)

    got_category = {c["category_name"]: c for c in result["current_month_category_status"]}
    for category_name, expected_status in expected["current_month_budget_status"].items():
        got = got_category.get(category_name)
        assert got is not None, f"missing category {category_name}"
        assert _close(got["percent_used"], expected_status["percent_used"])
        assert got["status"] == expected_status["status"]

    got_subcategory = {
        (s["category_name"], s["subcategory"]): s for s in result["current_month_subcategory_status"]
    }
    for key, expected_status in expected["current_month_subcategory_status"].items():
        category_name, subcategory = (part.strip() for part in key.split("|"))
        got = got_subcategory.get((category_name, subcategory))
        assert got is not None, f"missing subcategory {key}"
        assert got["status"] == expected_status["status"]


def test_income_budget_uses_previous_complete_month():
    """Most personas show ~0% earned in the current month because payday
    falls after AS_OF -- previous_month_income_budget is what a "did I earn
    what I planned" question should use."""
    result = budget.build_tracking(_txns_for("usr_anon_i07sal"), _budgets_for("usr_anon_i07sal"), _AS_OF)
    expected = _GT["usr_anon_i07sal"]["previous_month_income_budget"]

    got = result["previous_month_income_budget"]
    assert got is not None
    assert _close(got["earned_so_far_naira"], expected["earned_so_far_naira"])
    assert _close(got["planned_naira"], expected["planned_naira"])


@pytest.mark.parametrize("token", ["usr_anon_i08frl", "usr_anon_b14bet"])
def test_users_without_a_budget_are_handled(token):
    result = budget.build_tracking(_txns_for(token), _budgets_for(token), _AS_OF)
    assert result["has_budget"] is False
    assert result["current_month_category_status"] == []


def test_unbudgeted_category_is_not_invented():
    """G15 only budgets 2 of 6 categories -- Groceries must not appear."""
    result = budget.build_tracking(_txns_for("usr_anon_g15par"), _budgets_for("usr_anon_g15par"), _AS_OF)
    assert "Groceries" not in result["budgeted_categories"]


def test_recommend_is_grounded_in_real_income():
    """G19 is rent-heavy -- housing should dominate the category split."""
    result = budget.build_recommend(_txns_for("usr_anon_g19rnt"), _budgets_for("usr_anon_g19rnt"), _AS_OF)
    expected_income = _GT["usr_anon_g19rnt"]["avg_monthly_income_naira"]
    assert _close(result["average_monthly_income_naira"], expected_income)
    top_category = max(result["category_split"], key=lambda entry: entry["percent_of_income"])
    assert top_category["category_name"] == "Housing"
