import copy
import json
from pathlib import Path

from app.anonymiser import (
    anonymise,
    anonymise_many,
    anonymise_transaction,
    anonymise_transactions,
)

# The real poisoned fixture — 8 distinct upstream-mirror-failure cases (see
# moniger_copilot_fixtures_v2's README and CLAUDE.md's PII handling notes).
# Kept separate from the main transaction fixture by the fixtures README's
# own instruction; used here as the actual assertion-layer test the project
# owner asked for, not just synthetic cases invented in this test file.
_POISONED_FIXTURE = (
    Path(__file__).resolve().parent.parent
    / "moniger_copilot_fixtures_v2"
    / "copilot_transactions_POISONED.jsonl"
)


def _load_poisoned_transactions() -> list[dict]:
    with open(_POISONED_FIXTURE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

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
# Transaction record anonymisation (moniger_copilot_fixtures_v2 mirror schema)
# ---------------------------------------------------------------------------


def _mirror_transaction(**overrides):
    """A clean transaction in the real mirror shape (see
    moniger_copilot_fixtures_v2/copilot_transactions.jsonl)."""
    txn = {
        "userToken": "usr_anon_t01wbl",
        "date": "2025-08-06T14:35:00.000Z",
        "amountKobo": {"$numberLong": "1069006"},
        "direction": "debit",
        "status": "success",
        "categoryId": "6a00162930c1dd1fd5281700",
        "categoryName": "Transfers & Money Movement",
        "fiCategory": "Transfers & Money Movement",
        "fiCategoryCode": "TRF",
        "fiSubcategory": "P2P Outgoing (Gifts)",
        "fiConfidence": "High",
        "fiClassificationStage": "1",
        "fiIsRecurring": False,
        "merchantLabel": "SHOPRITE",
        "channel": "opay",
        "moneySource": "observed_external",
        "isBillPayment": False,
        "isWalletToWallet": False,
        "isExternalTransfer": True,
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


def test_retains_all_listed_safe_fields():
    """Every explicitly safe field should survive with its original value."""
    original = _mirror_transaction()
    sanitized = anonymise_transaction(original)

    assert sanitized["userToken"] == "usr_anon_t01wbl"
    assert sanitized["direction"] == "debit"
    assert sanitized["status"] == "success"
    assert sanitized["categoryId"] == "6a00162930c1dd1fd5281700"
    assert sanitized["fiCategoryCode"] == "TRF"
    assert sanitized["fiSubcategory"] == "P2P Outgoing (Gifts)"
    assert sanitized["fiIsRecurring"] is False
    assert sanitized["channel"] == "opay"
    assert sanitized["moneySource"] == "observed_external"
    assert sanitized["merchantLabel"] == "SHOPRITE"


def test_unlisted_field_does_not_leak_through():
    """A field that's neither retained nor in the known removal list should still be dropped."""
    txn = _mirror_transaction(someBrandNewField="unexpected-value")
    sanitized = anonymise_transaction(txn)

    assert "someBrandNewField" not in sanitized
    assert "unexpected-value" not in _flatten_values(sanitized)


def test_malformed_transaction_is_skipped_with_warning(caplog):
    """A non-object entry in the batch should be skipped and logged, not raise."""
    good = _mirror_transaction()
    with caplog.at_level("WARNING"):
        sanitized = anonymise_transactions([good, "not a transaction", None, 42])

    assert len(sanitized) == 1
    assert sanitized[0]["merchantLabel"] == "SHOPRITE"
    assert "malformed" in caplog.text.lower()


def test_missing_optional_fields_do_not_crash():
    """A transaction missing most optional fields entirely should process cleanly."""
    minimal = {"userToken": "usr_anon_s20new", "amountKobo": 5000, "direction": "debit"}
    sanitized = anonymise_transaction(minimal)

    assert sanitized["userToken"] == "usr_anon_s20new"
    assert sanitized["amountKobo"] == 5000
    assert "merchantLabel" not in sanitized


def test_empty_transactions_list_returns_empty_list():
    """An empty input list should return an empty output list."""
    assert anonymise_transactions([]) == []


def test_anonymise_transaction_does_not_mutate_input():
    """Sanitizing a transaction should never modify the caller's original object."""
    original = _mirror_transaction()
    snapshot = copy.deepcopy(original)

    anonymise_transaction(original)

    assert original == snapshot


# --- userToken shape validation ---------------------------------------------


def test_object_id_user_token_is_rejected():
    """A raw Mongo ObjectId used as userToken must drop the whole record
    (POISON-04) rather than let an internal database id travel downstream."""
    txn = _mirror_transaction(userToken="6a285f12c1dd1fd52817f913")
    assert anonymise_transaction(txn) is None


def test_well_formed_user_token_is_kept():
    assert anonymise_transaction(_mirror_transaction())["userToken"] == "usr_anon_t01wbl"


# --- merchantLabel scrubbing --------------------------------------------------


def test_merchant_name_without_transfer_keyword_is_not_tokenized():
    """A real merchant label with no TO/FRM direction keyword and no PII-shaped
    digits should be left alone -- this is the common case and must never be
    mangled (a heuristic that flags bare labels as names produces unacceptable
    false positives against real merchant/place names -- see the module
    docstring on _sanitize_merchant_label)."""
    txn = _mirror_transaction(merchantLabel="CHICKEN REPUBLIC DELIVERY")
    sanitized = anonymise_transaction(txn)
    assert sanitized["merchantLabel"] == "CHICKEN REPUBLIC DELIVERY"


def test_merchant_label_with_transfer_keyword_is_tokenized():
    txn = _mirror_transaction(merchantLabel="TRF TO CHIOMA EZE")
    sanitized = anonymise_transaction(txn)
    assert "CHIOMA" not in sanitized["merchantLabel"]
    assert sanitized["merchantLabel"] == "TRF TO PERSON_A"


def test_merchant_label_digit_pii_is_redacted():
    """A PII-shaped digit run embedded in an otherwise-legitimate merchant
    label should be redacted without discarding the rest of the label."""
    txn = _mirror_transaction(merchantLabel="MTN AIRTIME 08031234567")
    sanitized = anonymise_transaction(txn)
    assert "08031234567" not in sanitized["merchantLabel"]
    assert sanitized["merchantLabel"] == "MTN AIRTIME [REDACTED]"


def test_same_name_maps_to_same_token_across_transactions():
    """The same counterparty name across a batch should get one consistent token."""
    txn1 = _mirror_transaction(merchantLabel="TRF TO ADAEZE OKAFOR")
    txn2 = _mirror_transaction(merchantLabel="TRF TO ADAEZE OKAFOR")

    sanitized = anonymise_transactions([txn1, txn2])

    assert sanitized[0]["merchantLabel"] == sanitized[1]["merchantLabel"] == "TRF TO PERSON_A"


def test_different_names_get_sequential_tokens():
    """Distinct names encountered in order should get PERSON_A, PERSON_B, ..."""
    txn1 = _mirror_transaction(merchantLabel="TRF TO ADAEZE OKAFOR")
    txn2 = _mirror_transaction(merchantLabel="TRF FRM TOLUWANI BABATUNDE")

    sanitized = anonymise_transactions([txn1, txn2])

    assert "PERSON_A" in sanitized[0]["merchantLabel"]
    assert "PERSON_B" in sanitized[1]["merchantLabel"]
    assert "TOLUWANI" not in sanitized[1]["merchantLabel"]


def test_token_allocation_resets_between_calls():
    """The name -> token map must be local per call, not persisted across calls."""
    txn = _mirror_transaction(merchantLabel="TRF TO ADAEZE OKAFOR")

    first_call = anonymise_transactions([txn])
    second_call = anonymise_transactions([txn])

    assert first_call[0]["merchantLabel"] == second_call[0]["merchantLabel"] == "TRF TO PERSON_A"


# --- the real poisoned fixture: the actual assertion-layer test ------------


def test_poisoned_fixture_has_eight_distinct_cases():
    docs = _load_poisoned_transactions()
    assert len(docs) == 8
    assert len({d["_testCase"] for d in docs}) == 8


def test_poisoned_fixture_object_id_user_token_is_dropped():
    docs = _load_poisoned_transactions()
    poisoned_token_doc = next(d for d in docs if d["_testCase"].startswith("POISON-04"))
    assert anonymise_transaction(poisoned_token_doc) is None


def test_poisoned_fixture_produces_no_pii_leak():
    """Run every poisoned record (except the rejected-userToken one) through
    anonymise_transactions and assert none of the classic PII shapes survive
    anywhere in the output -- the actual test of "your assertion layer" the
    fixtures README describes, not just a check that the fixture itself is
    dirty."""
    docs = [d for d in _load_poisoned_transactions() if not d["_testCase"].startswith("POISON-04")]
    sanitized = anonymise_transactions(docs)

    assert len(sanitized) == len(docs)
    blob = json.dumps(sanitized)
    assert "@" not in blob, "an email survived sanitisation"
    assert "chidinma" not in blob.lower(), "a leaked name survived sanitisation"
    assert "0123456789" not in blob, "a leaked account/phone number survived sanitisation"
    assert "22000000021" not in blob, "a leaked BVN survived sanitisation"
    assert "narration" not in sanitized[0] and all("narration" not in d for d in sanitized)
    assert all("paymentDetails" not in d for d in sanitized)
    assert all("bvn" not in d and "contact" not in d for d in sanitized)
