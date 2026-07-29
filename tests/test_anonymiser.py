import copy

from app.anonymiser import (
    anonymise,
    anonymise_many,
    anonymise_transaction,
    anonymise_transactions,
)

# ---------------------------------------------------------------------------
# Free-text question anonymisation
# ---------------------------------------------------------------------------


def test_email_is_replaced():
    """An email address should be replaced with a stable [EMAIL_n] placeholder."""
    result = anonymise("Please email me at jane.doe@example.com about this")
    assert "jane.doe@example.com" not in result.text
    assert "[EMAIL_1]" in result.text


def test_phone_number_is_replaced():
    """A Nigerian mobile number should be replaced with a [PHONE_n] placeholder."""
    result = anonymise("Call me on 08031234567 tomorrow")
    assert "08031234567" not in result.text
    assert "[PHONE_1]" in result.text


def test_nuban_account_number_is_replaced():
    """A 10-digit NUBAN account number should be replaced with an [ACCOUNT_n] placeholder."""
    result = anonymise("Send it to account 0123456789 please")
    assert "0123456789" not in result.text
    assert "[ACCOUNT_1]" in result.text


def test_bvn_or_nin_is_replaced():
    """An 11-digit BVN/NIN should be replaced with a [BVN_NIN_n] placeholder."""
    result = anonymise("My BVN is 12345678901")
    assert "12345678901" not in result.text
    assert "[BVN_NIN_1]" in result.text


def test_repeated_value_maps_to_same_placeholder():
    """The same PII value mentioned twice should map to the same placeholder."""
    result = anonymise("My account 0123456789 and again 0123456789")
    assert result.text.count("[ACCOUNT_1]") == 2
    assert "[ACCOUNT_2]" not in result.text


def test_restore_recovers_original_value():
    """restore() on the anonymised text should recover the original string."""
    original = "Please email me at jane.doe@example.com"
    result = anonymise(original)
    assert result.restore(result.text) == original


def test_restore_recovers_value_in_llm_style_response():
    """restore() should work on a placeholder embedded in an arbitrary LLM response."""
    result = anonymise("My account is 0123456789")
    llm_response = "Your account [ACCOUNT_1] has sufficient balance."
    assert result.restore(llm_response) == "Your account 0123456789 has sufficient balance."


def test_non_pii_text_is_left_unchanged():
    """Text with no PII should pass through unchanged with an empty mapping."""
    text = "How much did I spend on groceries this month?"
    result = anonymise(text)
    assert result.text == text
    assert result.mapping == {}


def test_anonymise_many_shares_mapping_across_texts():
    """anonymise_many should share one placeholder mapping across all input texts."""
    batch = anonymise_many(
        [
            "My account is 0123456789",
            "Confirm transfer to 0123456789 was successful",
        ]
    )
    assert "[ACCOUNT_1]" in batch.texts[0]
    assert "[ACCOUNT_1]" in batch.texts[1]
    assert "[ACCOUNT_2]" not in batch.texts[1]
    assert batch.mapping == {"[ACCOUNT_1]": "0123456789"}


# ---------------------------------------------------------------------------
# Transaction record anonymisation
# ---------------------------------------------------------------------------


def _pii_laden_transaction(**overrides):
    txn = {
        "amount": 100000,
        "createdAt": "2026-06-05T09:00:00Z",
        "transactionType": "debit",
        "channel": "mobile",
        "isBillPayment": False,
        "isExternalBankTransfer": True,
        "narration": "NIP TRF TO ADAEZE OKAFOR - RENT JUN",
        "fi": {
            "category": "Rent",
            "subcategory": None,
            "is_recurring": True,
            "confidence_score": 0.97,
        },
        "paymentDetails": {
            "currency": "NGN",
            "charges": 26.88,
            "narration": "NIP TRF TO ADAEZE OKAFOR - RENT JUN",
            "accountName": "Chinedu Okoro",
            "accountNumber": "0123456789",
            "metadata": {
                "accountNumber": "0123456789",
                "nameEnquiryRef": "NER-abc123",
            },
        },
        "accountNumber": "0123456789",
        "BankVerificationCode": "22212345678",
        "beneficiaryAccountName": "Adaeze Okafor",
        "beneficiaryAccountNumber": "0987654321",
        "beneficiaryBankVerificationNumber": "22298765432",
        "originatorAccountName": "Chinedu Okoro",
        "originatorAccountNumber": "0123456789",
        "originatorBankVerificationNumber": "22212345678",
    }
    txn.update(overrides)
    return txn


def _flatten_values(obj) -> list:
    """Collect every leaf value in a nested dict/list, for PII-leak assertions."""
    values = []
    if isinstance(obj, dict):
        for v in obj.values():
            values.extend(_flatten_values(v))
    elif isinstance(obj, list):
        for item in obj:
            values.extend(_flatten_values(item))
    else:
        values.append(obj)
    return values


def test_removes_all_listed_pii_fields():
    """None of the explicitly PII-bearing fields should survive anonymisation."""
    original = _pii_laden_transaction()
    sanitized = anonymise_transaction(original)

    leaked_values = {
        "Chinedu Okoro", "0123456789", "22212345678", "Adaeze Okafor",
        "0987654321", "22298765432", "NER-abc123",
    }
    values = _flatten_values(sanitized)
    for leaked in leaked_values:
        assert leaked not in values, f"{leaked!r} leaked into sanitized output"

    assert "accountNumber" not in sanitized
    assert "BankVerificationCode" not in sanitized
    assert "beneficiaryAccountName" not in sanitized
    assert "beneficiaryAccountNumber" not in sanitized
    assert "beneficiaryBankVerificationNumber" not in sanitized
    assert "originatorAccountName" not in sanitized
    assert "originatorAccountNumber" not in sanitized
    assert "originatorBankVerificationNumber" not in sanitized
    assert "accountName" not in sanitized.get("paymentDetails", {})
    assert "metadata" not in sanitized.get("paymentDetails", {})


def test_retains_all_listed_safe_fields():
    """Every explicitly safe field should survive with its original value."""
    original = _pii_laden_transaction()
    sanitized = anonymise_transaction(original)

    assert sanitized["amount"] == 100000
    assert sanitized["createdAt"] == "2026-06-05T09:00:00Z"
    assert sanitized["transactionType"] == "debit"
    assert sanitized["channel"] == "mobile"
    assert sanitized["isBillPayment"] is False
    assert sanitized["isExternalBankTransfer"] is True
    assert sanitized["fi"]["category"] == "Rent"
    assert sanitized["fi"]["subcategory"] is None
    assert sanitized["fi"]["is_recurring"] is True
    assert sanitized["fi"]["confidence_score"] == 0.97
    assert sanitized["paymentDetails"]["currency"] == "NGN"
    assert sanitized["paymentDetails"]["charges"] == 26.88


def test_narration_person_name_is_tokenized():
    """A person's name after TO/FRM in the top-level narration should become PERSON_A."""
    original = _pii_laden_transaction()
    sanitized = anonymise_transaction(original)

    assert "ADAEZE" not in sanitized["narration"]
    assert "OKAFOR" not in sanitized["narration"]
    assert "PERSON_A" in sanitized["narration"]
    assert sanitized["narration"] == "NIP TRF TO PERSON_A - RENT JUN"


def test_payment_details_narration_is_tokenized():
    """paymentDetails.narration should be tokenised the same way as top-level narration."""
    original = _pii_laden_transaction()
    sanitized = anonymise_transaction(original)

    assert "ADAEZE" not in sanitized["paymentDetails"]["narration"]
    assert "PERSON_A" in sanitized["paymentDetails"]["narration"]


def test_same_name_maps_to_same_token_across_transactions():
    """The same counterparty name across a batch should get one consistent token."""
    txn1 = _pii_laden_transaction()
    txn2 = _pii_laden_transaction(narration="NIP TRF TO ADAEZE OKAFOR - RENT JUL")
    txn2["paymentDetails"] = dict(txn2["paymentDetails"], narration="NIP TRF TO ADAEZE OKAFOR - RENT JUL")

    sanitized = anonymise_transactions([txn1, txn2])

    assert sanitized[0]["narration"] == "NIP TRF TO PERSON_A - RENT JUN"
    assert sanitized[1]["narration"] == "NIP TRF TO PERSON_A - RENT JUL"


def test_different_names_get_sequential_tokens():
    """Distinct names encountered in order should get PERSON_A, PERSON_B, ..."""
    txn1 = _pii_laden_transaction()
    txn2 = _pii_laden_transaction(narration="NIP TRF FRM TOLUWANI BABATUNDE - LOGO DESIGN")
    txn2["paymentDetails"] = dict(txn2["paymentDetails"], narration="NIP TRF FRM TOLUWANI BABATUNDE - LOGO DESIGN")

    sanitized = anonymise_transactions([txn1, txn2])

    assert "PERSON_A" in sanitized[0]["narration"]
    assert "PERSON_B" in sanitized[1]["narration"]
    assert "TOLUWANI" not in sanitized[1]["narration"]


def test_merchant_name_without_transfer_keyword_is_not_tokenized():
    """A merchant narration with no TO/FRM direction keyword should be left alone."""
    txn = _pii_laden_transaction(narration="POS PURCHASE CHICKEN REPUBLIC")
    txn["paymentDetails"] = dict(txn["paymentDetails"], narration="POS PURCHASE CHICKEN REPUBLIC")

    sanitized = anonymise_transaction(txn)

    assert sanitized["narration"] == "POS PURCHASE CHICKEN REPUBLIC"
    assert "PERSON_" not in sanitized["narration"]


def test_malformed_transaction_is_skipped_with_warning(caplog):
    """A non-object entry in the batch should be skipped and logged, not raise."""
    good = _pii_laden_transaction()
    with caplog.at_level("WARNING"):
        sanitized = anonymise_transactions([good, "not a transaction", None, 42])

    assert len(sanitized) == 1
    assert sanitized[0]["amount"] == 100000
    assert "malformed" in caplog.text.lower()


def test_unlisted_field_does_not_leak_through():
    """A field that's neither retained nor in the known removal list should still be dropped."""
    txn = _pii_laden_transaction(someBrandNewField="unexpected-value")
    sanitized = anonymise_transaction(txn)

    assert "someBrandNewField" not in sanitized
    assert "unexpected-value" not in _flatten_values(sanitized)


def test_missing_optional_nested_fields_do_not_crash():
    """A transaction missing the fi/paymentDetails blocks entirely should process cleanly."""
    minimal = {"amount": 5000, "narration": "NIP TRF TO CHIOMA EZE - GIFT"}
    sanitized = anonymise_transaction(minimal)

    assert sanitized["amount"] == 5000
    assert "fi" not in sanitized
    assert "paymentDetails" not in sanitized
    assert "PERSON_A" in sanitized["narration"]


def test_empty_transactions_list_returns_empty_list():
    """An empty input list should return an empty output list."""
    assert anonymise_transactions([]) == []


def test_token_allocation_resets_between_calls():
    """The name -> token map must be local per call, not persisted across calls."""
    txn = _pii_laden_transaction()

    first_call = anonymise_transactions([txn])
    second_call = anonymise_transactions([txn])

    assert first_call[0]["narration"] == second_call[0]["narration"] == "NIP TRF TO PERSON_A - RENT JUN"


def test_anonymise_transaction_does_not_mutate_input():
    """Sanitizing a transaction should never modify the caller's original object."""
    original = _pii_laden_transaction()
    snapshot = copy.deepcopy(original)

    anonymise_transaction(original)

    assert original == snapshot
