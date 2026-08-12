from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bookkeeper.ingest.normalize import (
    AssetAccountCollisionError,
    check_no_asset_account_collisions,
    discover_asset_accounts,
    normalize_account_set,
    normalize_balances,
    simplefin_asset_account,
)
from bookkeeper.simplefin.models import AccountSet

PAYLOAD = {
    "errlist": [],
    "accounts": [
        {
            "id": "ACT-1",
            "name": "My Checking Account",
            "currency": "USD",
            "balance": "1234.56",
            "balance-date": 1_700_000_000,  # 2023-11-14T22:13:20Z
            "transactions": [
                {
                    "id": "TXN-1",
                    "posted": 1_699_900_000,
                    "amount": "-42.10",
                    "description": "SQ *COFFEE 4TH ST",
                },
                {
                    "id": "TXN-2",
                    "posted": 1_699_950_000,
                    "amount": "2500.00",
                    "description": "PAYROLL DEPOSIT",
                },
                {
                    "id": "TXN-3-PENDING",
                    "posted": 1_699_960_000,
                    "amount": "-10.00",
                    "description": "STILL PENDING",
                    "pending": True,
                },
            ],
        }
    ],
}


def test_simplefin_asset_account_is_deterministic_and_namespaced():
    # Namespaced under Assets:SimpleFIN: (decided 2026-07-30):
    # ingest owns opening these accounts itself (accounts-simplefin.beancount),
    # so accounts.beancount never has to hand-open one per bank connection.
    account_set = AccountSet.model_validate(PAYLOAD)
    account = account_set.accounts[0]
    name_first_call = simplefin_asset_account(account)
    name_second_call = simplefin_asset_account(account)
    assert name_first_call == name_second_call
    assert name_first_call == "Assets:SimpleFIN:MyCheckingAccount"


def test_simplefin_asset_account_strips_live_demo_branding_prefix():
    # Live-observed (2026-07-30): the demo server's real account names are
    # "SimpleFIN Savings" / "SimpleFIN Checking" / "SimpleFIN Empty
    # Account" -- not "Savings"/"Checking" outright. Stripping the brand
    # prefix keeps the namespaced name from doubling up as
    # Assets:SimpleFIN:SimplefinSavings.
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "Demo Savings",
                "name": "SimpleFIN Savings",
                "currency": "USD",
                "balance": "100.00",
                "balance-date": 1_700_000_000,
            },
            {
                "id": "Demo Empty Account",
                "name": "SimpleFIN Empty Account",
                "currency": "USD",
                "balance": "0.00",
                "balance-date": 1_700_000_000,
            },
        ],
    }
    account_set = AccountSet.model_validate(payload)
    assert simplefin_asset_account(account_set.accounts[0]) == "Assets:SimpleFIN:Savings"
    assert simplefin_asset_account(account_set.accounts[1]) == "Assets:SimpleFIN:EmptyAccount"


def test_simplefin_asset_account_collides_on_shared_display_name():
    # The naming function itself just sanitizes -- it doesn't know about
    # other accounts, so two accounts with the same display name DO
    # produce the same raw name. Enforcement that this must not happen
    # silently lives in check_no_asset_account_collisions, tested below.
    account_set = AccountSet.model_validate(PAYLOAD)
    account = account_set.accounts[0]
    twin = account.model_copy(update={"id": "ACT-2"})  # same name, different id
    assert simplefin_asset_account(account) == simplefin_asset_account(twin)


def test_check_no_asset_account_collisions_raises_naming_both_ids():
    account_set = AccountSet.model_validate(PAYLOAD)
    account = account_set.accounts[0]
    twin = account.model_copy(update={"id": "ACT-2"})  # same name, different id

    with pytest.raises(AssetAccountCollisionError) as exc_info:
        check_no_asset_account_collisions([account, twin])

    assert "ACT-1" in str(exc_info.value)
    assert "ACT-2" in str(exc_info.value)


def test_check_no_asset_account_collisions_allows_distinct_names():
    account_set = AccountSet.model_validate(PAYLOAD)
    check_no_asset_account_collisions(account_set.accounts)  # no raise


def test_check_no_asset_account_collisions_same_account_twice_is_fine():
    # Not a collision: literally the same account.id appearing twice
    # (e.g. present in both errlist-adjacent bookkeeping) must not
    # false-positive.
    account_set = AccountSet.model_validate(PAYLOAD)
    account = account_set.accounts[0]
    check_no_asset_account_collisions([account, account])  # no raise


def test_discover_asset_accounts_sorted_and_deduplicated():
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-B",
                "name": "SimpleFIN Savings",
                "currency": "USD",
                "balance": "1.00",
                "balance-date": 1_700_000_000,
            },
            {
                "id": "ACT-A",
                "name": "SimpleFIN Checking",
                "currency": "USD",
                "balance": "2.00",
                "balance-date": 1_700_000_000,
            },
        ],
    }
    account_set = AccountSet.model_validate(payload)
    opens = discover_asset_accounts(account_set)

    assert [o.asset_account for o in opens] == [
        "Assets:SimpleFIN:Checking",
        "Assets:SimpleFIN:Savings",
    ]  # sorted by name, not response order
    assert all(o.currency == "USD" for o in opens)


def test_normalize_account_set_excludes_pending_and_counts_it():
    account_set = AccountSet.model_validate(PAYLOAD)
    txns, pending_skipped = normalize_account_set(account_set)

    assert pending_skipped == 1
    assert {t.simplefin_id for t in txns} == {"TXN-1", "TXN-2"}
    assert all(not t.simplefin_id.endswith("PENDING") for t in txns)


def test_normalize_account_set_preserves_decimal_amounts_and_dates():
    account_set = AccountSet.model_validate(PAYLOAD)
    txns, _ = normalize_account_set(account_set)
    by_id = {t.simplefin_id: t for t in txns}

    assert by_id["TXN-1"].amount == Decimal("-42.10")
    assert by_id["TXN-1"].currency == "USD"
    assert isinstance(by_id["TXN-1"].posted_date, date)


def test_normalize_account_set_propagates_undocumented_live_fields():
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-1",
                "name": "Checking",
                "currency": "USD",
                "balance": "100.00",
                "balance-date": 1_700_000_000,
                "transactions": [
                    {
                        "id": "TXN-1",
                        "posted": 1_700_000_000,
                        "amount": "-5.00",
                        "description": "x",
                        "mcc": "5411",
                        "payee": "Square Inc",
                        "memo": "note",
                        "extra": {"category": "food"},
                    }
                ],
            }
        ],
    }
    account_set = AccountSet.model_validate(payload)
    txns, _ = normalize_account_set(account_set)

    assert txns[0].mcc == "5411"
    assert txns[0].payee == "Square Inc"
    assert txns[0].memo == "note"
    assert txns[0].extra == {"category": "food"}


def test_normalize_account_set_leaves_undocumented_fields_none_when_absent():
    account_set = AccountSet.model_validate(PAYLOAD)  # PAYLOAD sends none of them
    txns, _ = normalize_account_set(account_set)

    assert all(t.mcc is None and t.payee is None and t.memo is None for t in txns)


def test_normalize_balances_asserts_the_day_after_balance_date():
    account_set = AccountSet.model_validate(PAYLOAD)
    balances = normalize_balances(account_set)

    assert len(balances) == 1
    bal = balances[0]
    assert bal.amount == Decimal("1234.56")
    assert bal.assertion_date == date(2023, 11, 15)  # balance-date (11-14) + 1 day
