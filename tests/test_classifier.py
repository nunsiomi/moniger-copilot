from unittest.mock import patch

from app.classifier import QuestionCategory, classify_question

# classify_question calls Claude Haiku (see app/classifier.py's module
# docstring for why classification is no longer free/deterministic). Unit
# tests mock the one call boundary, _call_classifier_model, so they run
# without a live API call and deterministically exercise classify_question's
# OWN logic: enum mapping, the confidence threshold, and error handling.
# Whether Haiku itself classifies real questions correctly is covered by
# tests/eval/test_set.json instead, which does require a live call.

_MODULE = "app.classifier._call_classifier_model"


def _mock_model(intent, confidence):
    return patch(_MODULE, return_value={"intent": intent, "confidence": confidence})


# ---------------------------------------------------------------------------
# One test per intent
# ---------------------------------------------------------------------------


def test_classifies_spending_intent():
    with _mock_model("spending", 0.93):
        result = classify_question("How much did I spend on transport last week?")
    assert result.category == QuestionCategory.SPENDING
    assert result.confidence == 0.93


def test_classifies_income_intent():
    with _mock_model("income", 0.91):
        result = classify_question("What was my total income in March?")
    assert result.category == QuestionCategory.INCOME


def test_classifies_tax_intent():
    with _mock_model("tax", 0.88):
        result = classify_question("How much PAYE tax do I owe this year?")
    assert result.category == QuestionCategory.TAX


def test_classifies_comparison_intent():
    with _mock_model("comparison", 0.9):
        result = classify_question("Compare my spending this month versus last month")
    assert result.category == QuestionCategory.COMPARISON


def test_classifies_merchant_intent():
    with _mock_model("merchant", 0.85):
        result = classify_question("How much have I spent at Jumia this year?")
    assert result.category == QuestionCategory.MERCHANT


def test_classifies_budget_track_intent():
    with _mock_model("budget_track", 0.94):
        result = classify_question("How am I tracking against my food budget this month?")
    assert result.category == QuestionCategory.BUDGET_TRACK


def test_classifies_budget_recommend_intent():
    with _mock_model("budget_recommend", 0.9):
        result = classify_question("How should I budget my salary?")
    assert result.category == QuestionCategory.BUDGET_RECOMMEND


def test_classifies_general_intent():
    with _mock_model("general", 0.8):
        result = classify_question("What can you help me with?")
    assert result.category == QuestionCategory.GENERAL


# ---------------------------------------------------------------------------
# Adversarial out_of_scope_advice cases (at least 3 required)
# ---------------------------------------------------------------------------


def test_classifies_out_of_scope_advice_mutual_funds_vs_fixed_deposit():
    """Product-comparison framing — classic deflection case from the Addendum."""
    with _mock_model("out_of_scope_advice", 0.97):
        result = classify_question("Should I put my savings in mutual funds or a fixed deposit?")
    assert result.category == QuestionCategory.OUT_OF_SCOPE_ADVICE


def test_classifies_out_of_scope_advice_crypto_investment():
    """Security/asset-class framing."""
    with _mock_model("out_of_scope_advice", 0.95):
        result = classify_question("What crypto should I invest in with my extra cash?")
    assert result.category == QuestionCategory.OUT_OF_SCOPE_ADVICE


def test_classifies_out_of_scope_advice_provider_recommendation():
    """Specific-provider framing."""
    with _mock_model("out_of_scope_advice", 0.96):
        result = classify_question("Which fintech app should I move my savings to for better returns?")
    assert result.category == QuestionCategory.OUT_OF_SCOPE_ADVICE


def test_classifies_out_of_scope_advice_disguised_as_budget_question():
    """Adversarial: phrased like budgeting but asks for a product/allocation
    recommendation involving investing — must still be caught, not slip
    through as budget_recommend just because it mentions "budget"."""
    with _mock_model("out_of_scope_advice", 0.9):
        result = classify_question(
            "How should I budget my salary if I want to start investing in stocks and bonds?"
        )
    assert result.category == QuestionCategory.OUT_OF_SCOPE_ADVICE


# ---------------------------------------------------------------------------
# Confidence threshold behaviour
# ---------------------------------------------------------------------------


def test_low_confidence_defaults_to_general():
    with _mock_model("spending", 0.3):
        result = classify_question("hmm not sure what I mean")
    assert result.category == QuestionCategory.GENERAL
    assert result.confidence == 0.3


def test_low_confidence_out_of_scope_advice_still_defaults_to_general():
    """The threshold fallback applies uniformly — even a low-confidence
    out_of_scope_advice guess degrades to general rather than staying
    flagged on a guess. general still never gives product/investment
    advice, so this is a precision trade-off, not a guardrail gap."""
    with _mock_model("out_of_scope_advice", 0.2):
        result = classify_question("what about my money")
    assert result.category == QuestionCategory.GENERAL
    assert result.confidence == 0.2


def test_confidence_exactly_at_threshold_is_not_downgraded():
    with _mock_model("tax", 0.5):
        result = classify_question("How much tax will I pay?")
    assert result.category == QuestionCategory.TAX
    assert result.confidence == 0.5


def test_confidence_just_below_threshold_is_downgraded():
    with _mock_model("tax", 0.49):
        result = classify_question("How much tax will I pay?")
    assert result.category == QuestionCategory.GENERAL


# ---------------------------------------------------------------------------
# Robustness — classify_question must never raise
# ---------------------------------------------------------------------------


def test_invalid_intent_from_model_defaults_to_general():
    """A hallucinated intent string outside the enum should not crash the request."""
    with _mock_model("crypto_advice", 0.9):
        result = classify_question("some question")
    assert result.category == QuestionCategory.GENERAL
    assert result.confidence == 0.0


def test_missing_confidence_key_defaults_to_general():
    with patch(_MODULE, return_value={"intent": "spending"}):
        result = classify_question("some question")
    assert result.category == QuestionCategory.GENERAL
    assert result.confidence == 0.0


def test_missing_intent_key_defaults_to_general():
    with patch(_MODULE, return_value={"confidence": 0.9}):
        result = classify_question("some question")
    assert result.category == QuestionCategory.GENERAL
    assert result.confidence == 0.0


def test_model_call_exception_defaults_to_general():
    with patch(_MODULE, side_effect=RuntimeError("network error")):
        result = classify_question("some question")
    assert result.category == QuestionCategory.GENERAL
    assert result.confidence == 0.0
