"""Tests for the 2026-10-26 review feedback: month resolution, comparison
breakdown tables, tax itemisation computed in code, the `period` field, the
tester tier, and persona selection in the demo."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import response_parser
from app.breakdown import build_breakdown
from app.classifier import ClassificationResult, QuestionCategory
from app.context_builders import comparison, tax
from app.data_access import _decode_document
from app.periods import resolve_months, shift_month

REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = REPO_ROOT / "moniger_copilot_fixtures_v2" / "copilot_transactions.jsonl"


@pytest.fixture(scope="module")
def transactions_for():
    rows = [_decode_document(json.loads(line)) for line in _FIXTURES.read_text(encoding="utf-8").splitlines()]

    def _for(user: str) -> list[dict]:
        return [t for t in rows if t["userToken"] == user and t.get("status") == "success"]

    return _for


# --- Month resolution ---------------------------------------------------------


@pytest.mark.parametrize("question, expected", [
    ("How does my June spending compare to May?", ["2026-06", "2026-05"]),
    ("Compare December to November", ["2025-12", "2025-11"]),
    ("How did December 2025 compare to December 2024?", ["2025-12", "2024-12"]),
    ("How does this month compare to last month?", ["2026-07", "2026-06"]),
    ("How much did I spend in Sept?", ["2025-09"]),
    ("How may I compare my spending?", []),
    ("How does my spending compare?", []),
])
def test_resolve_months(question, expected):
    assert resolve_months(question, "2026-07") == expected


def test_shift_month_crosses_year_boundary():
    assert shift_month("2026-01", -1) == "2025-12"
    assert shift_month("2025-12", 1) == "2026-01"


# --- Comparison ---------------------------------------------------------------


def test_comparison_uses_months_named_in_question(transactions_for):
    context = comparison.build(transactions_for("usr_anon_i07sal"), "How does my June spending compare to May?")
    assert (context["previous_month"], context["current_month"]) == ("2026-05", "2026-06")
    assert context["months_from_question"] is True
    assert context["current_month_data_through"] is None  # June is complete


def test_comparison_default_flags_partial_latest_month(transactions_for):
    context = comparison.build(transactions_for("usr_anon_i07sal"))
    assert context["current_month"] == "2026-07"
    assert context["current_month_data_through"] == "2026-07-20"
    table = build_breakdown(QuestionCategory.COMPARISON, context)
    assert "1-20 Jul 2026 only" in table["note"]


def test_comparison_breakdown_rows_reconcile_to_totals(transactions_for):
    context = comparison.build(transactions_for("usr_anon_i07sal"), "June vs May")
    table = build_breakdown(QuestionCategory.COMPARISON, context)
    assert table["columns"] == ["Category", "May 2026", "Jun 2026", "Change"]
    current_sum = round(sum(e["current_naira"] for e in context["category_deltas"]), 2)
    assert current_sum == context["current_totals_naira"]["spend_naira"]
    assert table["footer"][0][0] == "Total spending"
    assert len(table["rows"]) == len(context["category_deltas"])


# --- Tax ----------------------------------------------------------------------


@pytest.mark.parametrize("chargeable, total", [
    (0, 0.0),
    (800_000, 0.0),
    (840_000, 6_000.0),
    (3_000_000, 330_000.0),
    (5_000_000, 690_000.0),
    (25_000_000, 5_190_000.0),
])
def test_compute_tax_walks_bands(chargeable, total):
    result = tax.compute_tax(chargeable)
    assert result["total_annual_tax_naira"] == total
    assert round(sum(b["tax_naira"] for b in result["bands"]), 2) == total
    assert round(sum(b["taxable_naira"] for b in result["bands"]), 2) == chargeable


def test_tax_threshold_pair_differs(transactions_for):
    below = tax.build(transactions_for("usr_anon_t02jbl"))
    above = tax.build(transactions_for("usr_anon_t03jab"))
    assert below["tax_computation"]["total_annual_tax_naira"] == 0
    assert above["tax_computation"]["total_annual_tax_naira"] == 6_000.0


def test_tax_breakdown_itemises_bands(transactions_for):
    context = tax.build(transactions_for("usr_anon_t04cab"))
    table = build_breakdown(QuestionCategory.TAX, context)
    assert [row[0] for row in table["rows"]] == ["First ₦800,000", "Next ₦2,200,000", "Next ₦4,000,000"]
    assert table["footer"][0][-1] == f"₦{context['tax_computation']['total_annual_tax_naira']:,.2f}"
    assert "last 12 months" in table["note"]


def test_breakdown_only_for_comparison_and_tax():
    assert build_breakdown(QuestionCategory.SPENDING, {"total_spend_naira": 1}) is None


# --- Response contract ----------------------------------------------------------


def test_period_is_parsed_and_soft():
    raw = json.dumps({"answer": "a", "explanation": "b", "period": " June 2026 ", "confidence": 1, "sources": []})
    assert response_parser.parse(raw, question_type=QuestionCategory.SPENDING, cache_hit=False)["period"] == "June 2026"
    raw = json.dumps({"answer": "a", "explanation": "b", "confidence": 1, "sources": []})
    assert response_parser.parse(raw, question_type=QuestionCategory.SPENDING, cache_hit=False)["period"] is None


def test_ask_returns_period_and_breakdown(monkeypatch, transactions_for):
    import jwt
    from fastapi.testclient import TestClient

    from app import data_access, main

    class MemoryRedis:
        def __init__(self):
            self.values = {}
        def get(self, key):
            return self.values.get(key)
        def set(self, key, value, ex=None):
            self.values[key] = value
        def incr(self, key):
            self.values[key] = self.values.get(key, 0) + 1
            return self.values[key]
        def expire(self, key, seconds):
            pass

    main.app.dependency_overrides[main.get_redis_client] = lambda: MemoryRedis()
    monkeypatch.setattr(data_access, "get_transactions", lambda user: transactions_for("usr_anon_i07sal"))
    monkeypatch.setattr(data_access, "get_budgets", lambda user: [])
    monkeypatch.setattr(main, "classify_question",
                        Mock(return_value=ClassificationResult(category=QuestionCategory.COMPARISON, confidence=1.0)))
    monkeypatch.setattr(main.router, "call_model", Mock(return_value=json.dumps({
        "answer": "June was **₦5,188.31** lower than May.", "explanation": "From monthly totals.",
        "period": "May-June 2026", "confidence": 0.9, "sources": ["category_deltas"]})))
    token = jwt.encode({"sub": "usr_anon_i07sal", "tier": "tester"}, main.get_settings().jwt_secret, algorithm="HS256")
    try:
        response = TestClient(main.app).post(
            "/ask", headers={"Authorization": f"Bearer {token}"},
            json={"user_id": "usr_anon_i07sal", "question": "How does my June spending compare to May?"},
        )
    finally:
        main.app.dependency_overrides.clear()
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "May-June 2026"
    assert body["breakdown"]["columns"][1:3] == ["May 2026", "Jun 2026"]


# --- Tester tier --------------------------------------------------------------


def test_tester_tier_is_recognised_with_its_own_limit():
    from app import gating
    assert gating._TIER_LIMITS["tester"] > gating._TIER_LIMITS["premium"]


# --- Prompt context trimming ----------------------------------------------------

_PERSONAS = [
    "usr_anon_t01wbl", "usr_anon_t02jbl", "usr_anon_t03jab", "usr_anon_t04cab", "usr_anon_t05rse",
    "usr_anon_t06usd", "usr_anon_i07sal", "usr_anon_i08frl", "usr_anon_i09sid", "usr_anon_i10gap",
    "usr_anon_b11sea", "usr_anon_b12mch", "usr_anon_b13sub", "usr_anon_b14bet", "usr_anon_g15par",
    "usr_anon_g16trk", "usr_anon_g17rsk", "usr_anon_g18ovr", "usr_anon_g19rnt", "usr_anon_s20new",
]


@pytest.mark.parametrize("user", _PERSONAS)
def test_trimmed_spending_context_keeps_every_category(user, transactions_for):
    """Regression: the trimmer used to cut the all-time category list to a
    single entry for 18 of 20 personas, so "how much on food?" found no food."""
    from app import prompt_builder
    from app.context_builders import spending

    full = spending.build(transactions_for(user))
    content = prompt_builder.build_messages(QuestionCategory.SPENDING, "How much on food?", full)[0]["content"]
    sent = json.loads(content[content.index("<context>") + 9:content.index("</context>")])

    assert len(sent["category_breakdown"]) == len(full["category_breakdown"])
    assert len(sent["monthly_breakdown"]) >= min(4, len(full["monthly_breakdown"]))
    assert sent["monthly_breakdown"][-1]["month"] == full["monthly_breakdown"][-1]["month"]
    assert len(content) // 4 <= prompt_builder._HAIKU_CONTEXT_TOKEN_CAP + 100


def test_cache_key_changes_when_prompt_changes():
    from app import cache
    base = cache.fingerprint(QuestionCategory.SPENDING, "q", {"a": 1}, prompt="v1")
    assert base == cache.fingerprint(QuestionCategory.SPENDING, "q", {"a": 1}, prompt="v1")
    assert base != cache.fingerprint(QuestionCategory.SPENDING, "q", {"a": 1}, prompt="v2")


@pytest.mark.parametrize("answer, period, expected", [
    ("**₦586,770.25**", "3 Jun 2025 - 20 Jul 2026", "**₦586,770.25** (3 Jun 2025 - 20 Jul 2026)"),
    ("You spent ₦41,801.32 on food in June 2026.", "June 2026", "You spent ₦41,801.32 on food in June 2026."),
    ("You spent ₦5,000 this month.", "July 2026", "You spent ₦5,000 this month."),
    ("You don't have a food budget.", "July 2026", "You don't have a food budget."),
    ("₦5,000", None, "₦5,000"),
])
def test_amounts_always_carry_a_period(answer, period, expected):
    raw = json.dumps({"answer": answer, "explanation": "x", "period": period, "confidence": 1, "sources": []})
    assert response_parser.parse(raw, question_type=QuestionCategory.SPENDING, cache_hit=False)["answer"] == expected


def test_period_goes_before_the_disclaimer_and_monthly_counts_as_time():
    from app.prompts.budget_system_prompt import BUDGET_RECOMMEND_DISCLAIMER as D
    raw = json.dumps({"answer": f"- Needs: ₦120,000.00 {D}", "explanation": "x", "period": "July 2026",
                      "confidence": 1, "sources": []})
    answer = response_parser.parse(raw, question_type=QuestionCategory.BUDGET_RECOMMEND, cache_hit=False)["answer"]
    assert answer.endswith(D) and answer.count(D) == 1 and "(July 2026)" in answer
    raw = json.dumps({"answer": "Needs: ₦120,000 monthly", "explanation": "x", "period": "monthly",
                      "confidence": 1, "sources": []})
    assert response_parser.parse(raw, question_type=QuestionCategory.SPENDING, cache_hit=False)["answer"] == \
        "Needs: ₦120,000 monthly"


def test_classifier_retries_an_invalid_tool_call_then_falls_back(monkeypatch):
    import httpx
    from groq import BadRequestError

    from app import classifier

    def bad_request():
        response = httpx.Response(400, request=httpx.Request("POST", "https://api.groq.com"))
        return BadRequestError("tool_use_failed", response=response, body=None)

    replies = [bad_request(), {"intent": "income", "confidence": 0.9}]

    def fake_call(question):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(classifier, "_call_classifier_model", fake_call)
    assert classifier.classify_question("Was there any month I didn't get paid?").category == QuestionCategory.INCOME

    replies[:] = [bad_request(), bad_request()]
    result = classifier.classify_question("Was there any month I didn't get paid?")
    assert result.category == QuestionCategory.GENERAL and result.confidence == 0.0


def test_subscriptions_use_the_mirror_flag_not_the_amount_pattern(transactions_for):
    from app.context_builders import merchant
    context = merchant.build(transactions_for("usr_anon_b13sub"))
    gt = json.loads((REPO_ROOT / "moniger_copilot_tests" / "ground_truth.json").read_text(encoding="utf-8"))
    assert context["recurring_merchants"] == gt["personas"]["usr_anon_b13sub"]["recurring_merchants"]
    assert "BOLT" not in context["recurring_merchants"]


def test_rent_is_a_fixed_cost_and_not_projected_at_daily_pace():
    from datetime import date

    from app.context_builders import budget
    rows = [json.loads(line) for line in
            (REPO_ROOT / "moniger_copilot_fixtures_v2" / "copilot_budgets.jsonl").read_text(encoding="utf-8").splitlines()]
    budgets = [_decode_document(b) for b in rows if b["userToken"] == "usr_anon_g19rnt"]
    context = budget.build_tracking([], budgets, date(2026, 7, 20))
    housing = next(c for c in context["current_month_category_status"] if c["category_name"] == "Housing")
    assert housing["is_fixed_cost"] is True
    assert housing["projected_end_of_period_spend_naira"] == housing["spent_naira"] == 280_000.0
    assert housing["status"] == "at_risk"  # unchanged: matches ground truth


def test_income_lists_months_without_pay(transactions_for):
    from app.context_builders import income
    context = income.build(transactions_for("usr_anon_i10gap"))
    assert context["complete_months_without_income"] == ["2026-04", "2026-05"]
    assert context["current_month_in_progress"] == "2026-07"
    gt = json.loads((REPO_ROOT / "moniger_copilot_tests" / "ground_truth.json").read_text(encoding="utf-8"))
    by_month = {m["month"]: m["income_naira"] for m in context["monthly_income"] if m["income_naira"]}
    assert by_month == gt["personas"]["usr_anon_i10gap"]["income_by_month_naira"]


def test_budget_status_table():
    from datetime import date

    from app.context_builders import budget
    rows = [json.loads(line) for line in
            (REPO_ROOT / "moniger_copilot_fixtures_v2" / "copilot_budgets.jsonl").read_text(encoding="utf-8").splitlines()]
    def table_for(user):
        budgets = [_decode_document(b) for b in rows if b["userToken"] == user]
        return build_breakdown(QuestionCategory.BUDGET_TRACK, budget.build_tracking([], budgets, date(2026, 7, 20)))
    over = table_for("usr_anon_g18ovr")
    assert over["rows"][0] == ["Food & Drinks", "₦25,000.00", "₦30,719.45", "122.9%", "Over budget"]
    assert "64.5% of Jul 2026 has passed" in over["note"]
    assert any(r[-1] == "At risk (fixed cost)" for r in table_for("usr_anon_g19rnt")["rows"])
    assert table_for("usr_anon_i08frl") is None  # no budget
