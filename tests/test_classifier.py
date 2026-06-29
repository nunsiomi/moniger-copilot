from app.classifier import QuestionCategory, classify_question


def test_spending_question():
    assert classify_question("How much did I spend on transport last week?") == QuestionCategory.SPENDING


def test_income_question():
    assert classify_question("What was my total income in March?") == QuestionCategory.INCOME


def test_tax_question():
    assert classify_question("How much PAYE tax do I owe this year?") == QuestionCategory.TAX


def test_comparison_question():
    assert (
        classify_question("Compare my spending this month versus last month")
        == QuestionCategory.COMPARISON
    )


def test_merchant_question():
    assert classify_question("How much have I spent at Jumia this year?") == QuestionCategory.MERCHANT


def test_unmatched_question_falls_back_to_general():
    assert classify_question("What's the weather like today?") == QuestionCategory.GENERAL


def test_comparison_keywords_take_priority_over_spending():
    # "spending" alone would match SPENDING, but "compare" should win because
    # comparison keywords are checked first.
    assert (
        classify_question("Compare my spending across categories")
        == QuestionCategory.COMPARISON
    )


def test_classification_is_case_insensitive():
    assert classify_question("HOW MUCH DID I SPEND ON FOOD") == QuestionCategory.SPENDING
