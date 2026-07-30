from __future__ import annotations

from bookkeeper.categorize.normalize import normalize_description


def test_same_merchant_different_trailing_store_numbers_collide():
    # PLAN.md §5.4's own example: the same coffee shop, two different
    # trailing store/transaction numbers.
    a = normalize_description("SQ *COFFEE 4TH ST 8829")
    b = normalize_description("SQ *COFFEE 4TH ST 8831")
    assert a == b
    assert a == "coffee 4th st"


def test_ach_debit_prefix_and_bare_description_collide():
    a = normalize_description("PG&E WEB ONLINE")
    b = normalize_description("ACH DEBIT - PG&E WEB ONLINE")
    assert a == b


def test_distinct_merchants_stay_distinct():
    # Negative test: normalization must not be so aggressive that two
    # genuinely different merchants collapse into the same key.
    coffee = normalize_description("SQ *COFFEE 4TH ST 8829")
    bagels = normalize_description("SQ *BAGEL SHOP 4471")
    assert coffee != bagels


def test_pos_debit_prefix_stripped():
    a = normalize_description("POS DEBIT AMAZON MARKETPLACE")
    b = normalize_description("AMAZON MARKETPLACE")
    assert a == b


def test_checkcard_prefix_stripped():
    a = normalize_description("CHECKCARD TARGET STORES")
    b = normalize_description("TARGET STORES")
    assert a == b


def test_recurring_payment_prefix_stripped():
    a = normalize_description("RECURRING PAYMENT NETFLIX.COM")
    b = normalize_description("NETFLIX.COM")
    assert a == b


def test_purchase_authorized_on_date_prefix_stripped():
    a = normalize_description("PURCHASE AUTHORIZED ON 12/01 TRADER JOES 123")
    b = normalize_description("TRADER JOES")
    assert a == b


def test_chained_prefixes_all_stripped():
    # A description carrying more than one rail-noise prefix in sequence.
    a = normalize_description("PURCHASE AUTHORIZED ON 12/01 SQ *COFFEE 4TH ST 8829")
    assert a == "coffee 4th st"


def test_tst_star_prefix_stripped():
    a = normalize_description("TST* TASTY DINER")
    b = normalize_description("TASTY DINER")
    assert a == b


def test_short_digit_runs_in_merchant_name_survive():
    # 1-2 digit runs are part of merchant identity, not noise -- must not
    # be stripped the way a 3+ digit store number is.
    assert "7 eleven" in normalize_description("7-ELEVEN")
    assert "76" in normalize_description("76 GAS STATION")


def test_case_and_punctuation_are_normalized():
    a = normalize_description("Pg&e Web Online")
    b = normalize_description("PG&E WEB ONLINE")
    assert a == b


def test_whitespace_is_collapsed():
    a = normalize_description("COFFEE    SHOP")
    b = normalize_description("COFFEE SHOP")
    assert a == b


def test_idempotent():
    once = normalize_description("SQ *COFFEE 4TH ST 8829")
    twice = normalize_description(once)
    assert once == twice
