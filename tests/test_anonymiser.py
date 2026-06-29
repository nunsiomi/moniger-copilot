from app.anonymiser import anonymise, anonymise_many


def test_email_is_replaced():
    result = anonymise("Please email me at jane.doe@example.com about this")
    assert "jane.doe@example.com" not in result.text
    assert "[EMAIL_1]" in result.text


def test_phone_number_is_replaced():
    result = anonymise("Call me on 08031234567 tomorrow")
    assert "08031234567" not in result.text
    assert "[PHONE_1]" in result.text


def test_nuban_account_number_is_replaced():
    result = anonymise("Send it to account 0123456789 please")
    assert "0123456789" not in result.text
    assert "[ACCOUNT_1]" in result.text


def test_bvn_or_nin_is_replaced():
    result = anonymise("My BVN is 12345678901")
    assert "12345678901" not in result.text
    assert "[BVN_NIN_1]" in result.text


def test_repeated_value_maps_to_same_placeholder():
    result = anonymise("My account 0123456789 and again 0123456789")
    assert result.text.count("[ACCOUNT_1]") == 2
    assert "[ACCOUNT_2]" not in result.text


def test_restore_recovers_original_value():
    original = "Please email me at jane.doe@example.com"
    result = anonymise(original)
    assert result.restore(result.text) == original


def test_restore_recovers_value_in_llm_style_response():
    result = anonymise("My account is 0123456789")
    llm_response = "Your account [ACCOUNT_1] has sufficient balance."
    assert result.restore(llm_response) == "Your account 0123456789 has sufficient balance."


def test_non_pii_text_is_left_unchanged():
    text = "How much did I spend on groceries this month?"
    result = anonymise(text)
    assert result.text == text
    assert result.mapping == {}


def test_anonymise_many_shares_mapping_across_texts():
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
