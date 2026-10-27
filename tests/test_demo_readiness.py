"""Regression checks for the demo fixes. No network or model calls."""
import copy
import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import data_access
from app.context_builders import budget
from app.mirror_validation import MirrorValidationError, validate_transactions, validate_budgets

FIXTURES = Path(__file__).resolve().parents[1] / "moniger_copilot_fixtures_v2"


def docs(name):
    return [data_access._decode_document(json.loads(s))
            for s in (FIXTURES / name).read_text(encoding="utf-8").splitlines()]


@pytest.fixture(autouse=True)
def catalog(monkeypatch):
    monkeypatch.setenv("MERCHANT_LABELS_FILE", str(FIXTURES / "merchant_labels.json"))


@pytest.mark.parametrize("index", range(8))
def test_each_poison_is_rejected_without_mutation(index):
    doc = docs("copilot_transactions_POISONED.jsonl")[index]
    # Test the actual contamination, not the fixture's annotation field.
    doc.pop("_testCase")
    before = copy.deepcopy(doc)
    with pytest.raises(MirrorValidationError):
        validate_transactions([doc], "usr_anon_t01wbl" if index == 3 else doc["userToken"])
    assert doc == before


def test_every_clean_fixture_passes_without_mutation():
    for filename, validator in [("copilot_transactions.jsonl", validate_transactions),
                                ("copilot_budgets.jsonl", validate_budgets)]:
        rows = docs(filename)
        before = copy.deepcopy(rows)
        for user in {d["userToken"] for d in rows}:
            validator([d for d in rows if d["userToken"] == user], user)
        assert rows == before


def test_budget_rejects_pii_nested_in_subcategory():
    b = docs("copilot_budgets.jsonl")[0]
    b["expenseBreakdown"][0]["subcategories"][0]["name"] = "test@example.com"
    with pytest.raises(MirrorValidationError):
        validate_budgets([b], b["userToken"])


def test_unknown_merchant_fails_closed(monkeypatch):
    t = docs("copilot_transactions.jsonl")[0]
    t["merchantLabel"] = "UNVERIFIED PERSON"
    with pytest.raises(MirrorValidationError):
        validate_transactions([t], t["userToken"])


@pytest.mark.parametrize("allocated,spent,expected", [(0,10000,"over_budget"),
    (0,0,"on_track"), (100000,100001,"over_budget"), (100000,50000,"on_track")])
def test_budget_status_uses_actual_kobo(allocated, spent, expected):
    categories, subs, _ = budget._category_and_subcategory_status({"expenseBreakdown": [{
        "total_allocated": allocated,
        "subcategories": [{"allocatedAmount": allocated, "spentAmount": spent}],
    }]}, 64.5)
    assert categories[0]["status"] == expected
    assert subs[0]["status"] == expected


def test_utc_budget_dates_equal_naive_dates():
    b = {"periodStart": "2026-07-01T00:00:00", "periodEnd": "2026-07-31T23:59:59"}
    utc = {k: v + "Z" for k, v in b.items()}
    assert budget._percent_elapsed(utc, date(2026,7,20)) == budget._percent_elapsed(b, date(2026,7,20)) == 64.5


def test_recommendation_uses_declared_income_and_exact_allocations():
    tx = [t for t in docs("copilot_transactions.jsonl") if t["userToken"] == "usr_anon_t05rse"]
    bs = [b for b in docs("copilot_budgets.jsonl") if b["userToken"] == "usr_anon_t05rse"]
    result = budget.build_recommend(tx, bs, date(2026,7,20))
    assert result["budget_income_naira"] == 120000
    assert result["income_basis"] == "declared_budget"
    assert result["framework_50_30_20"] == {"needs_naira": 60000, "wants_naira": 36000,
                                          "remaining_for_savings_or_debt_naira": 24000}
    assert result["average_monthly_income_naira"] != result["budget_income_naira"]


def test_declared_zero_income_is_not_replaced_by_history():
    b = docs("copilot_budgets.jsonl")[0]
    b.update(budgetMonth="2026-07-01", totalMonthlyIncome=0)
    result = budget.build_recommend(docs("copilot_transactions.jsonl"), [b], date(2026,7,20))
    assert result["budget_income_naira"] == 0
    assert result["income_basis"] == "declared_budget"


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


@pytest.fixture
def api(monkeypatch):
    import jwt
    from fastapi.testclient import TestClient
    from app import main
    r = MemoryRedis()
    main.app.dependency_overrides[main.get_redis_client] = lambda: r
    monkeypatch.setattr(data_access, "get_transactions", lambda user: [])
    monkeypatch.setattr(data_access, "get_budgets", lambda user: [])
    token = jwt.encode({"sub":"usr_anon_test", "tier":"premium"},
                       main.get_settings().jwt_secret, algorithm="HS256")
    client = TestClient(main.app)
    yield client, {"Authorization": "Bearer " + token}, main
    main.app.dependency_overrides.clear()


def test_mirror_failure_calls_no_models(api, monkeypatch):
    client, headers, main = api
    classify = Mock()
    answer = Mock()
    monkeypatch.setattr(main, "classify_question", classify)
    monkeypatch.setattr(main.router, "call_model", answer)
    monkeypatch.setattr(data_access, "get_transactions", Mock(side_effect=MirrorValidationError("unsafe")))
    result = client.post("/ask", headers=headers, json={"user_id":"usr_anon_test", "question":"My budget?"})
    assert result.status_code == 503
    assert result.json()["error"] == "ERR_MIRROR_VALIDATION"
    classify.assert_not_called()
    answer.assert_not_called()


def test_repeat_question_uses_no_additional_model_calls(api, monkeypatch):
    from app.classifier import ClassificationResult, QuestionCategory
    client, headers, main = api
    classify = Mock(return_value=ClassificationResult(category=QuestionCategory.SPENDING, confidence=1.0))
    answer = Mock(return_value=json.dumps({"answer":"No spending recorded.", "explanation":"No transactions.",
                                         "confidence":1.0,"sources":[]}))
    monkeypatch.setattr(main, "classify_question", classify)
    monkeypatch.setattr(main.router, "call_model", answer)
    payload = {"user_id":"usr_anon_test", "question":"How much did I spend?"}
    first = client.post("/ask", headers=headers, json=payload)
    second = client.post("/ask", headers=headers, json=payload)
    assert first.status_code == second.status_code == 200
    assert first.json()["cache_hit"] is False
    assert second.json()["cache_hit"] is True
    classify.assert_called_once()
    answer.assert_called_once()


def test_provider_failure_does_not_make_second_call(api, monkeypatch):
    client, headers, main = api
    classify = Mock(side_effect=main.ClassifierUnavailableError("unavailable"))
    answer = Mock()
    monkeypatch.setattr(main, "classify_question", classify)
    monkeypatch.setattr(main.router, "call_model", answer)
    r = client.post("/ask", headers=headers, json={"user_id":"usr_anon_test", "question":"My spending?"})
    assert r.status_code == 503
    assert r.json()["error"] == "ERR_MODEL_UNAVAILABLE"
    answer.assert_not_called()


def test_preview_auth_and_no_model_calls(api, monkeypatch):
    client, headers, main = api
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setattr(data_access, "get_as_of_date", lambda: date(2026,7,20))
    classify, answer = Mock(), Mock()
    monkeypatch.setattr(main, "classify_question", classify)
    monkeypatch.setattr(main.router, "call_model", answer)
    r = client.get("/demo/summary?user_id=usr_anon_test", headers=headers)
    assert r.status_code == 200
    assert r.json()["mode"] == "data_preview_no_ai"
    assert client.get("/demo/summary?user_id=usr_anon_other", headers=headers).status_code == 403
    assert client.get("/demo/summary?user_id=usr_anon_test").status_code == 401
    classify.assert_not_called()
    answer.assert_not_called()
    monkeypatch.setenv("APP_ENV", "production")
    assert client.get("/demo/summary?user_id=usr_anon_test", headers=headers).status_code == 404


def test_nested_pii_cannot_hide_inside_scalar_field():
    t = docs("copilot_transactions.jsonl")[0]
    t["categoryName"] = {"email": "test@example.com"}
    with pytest.raises(MirrorValidationError):
        validate_transactions([t], t["userToken"])


@pytest.mark.parametrize("provider_status,expected_code,http_status", [
    (429, "ERR_MODEL_RATE_LIMIT", 429), (400, "ERR_MODEL_REQUEST", 503),
    (403, "ERR_MODEL_UNAVAILABLE", 503),
])
def test_provider_errors_are_actionable_without_exposing_body(api, monkeypatch, caplog,
                                                            provider_status, expected_code, http_status):
    import httpx
    from groq import APIStatusError
    client, headers, main = api
    response = httpx.Response(provider_status, request=httpx.Request("POST", "https://example.test"))
    cause = APIStatusError("private prompt content", response=response, body={"secret": "private prompt content"})
    wrapped = main.ClassifierUnavailableError("unavailable")
    wrapped.__cause__ = cause
    monkeypatch.setattr(main, "classify_question", Mock(side_effect=wrapped))
    r = client.post("/ask", headers=headers, json={"user_id":"usr_anon_test", "question":"My spending?"})
    assert r.status_code == http_status
    assert r.json()["error"] == expected_code
    assert "private prompt content" not in r.text + caplog.text
