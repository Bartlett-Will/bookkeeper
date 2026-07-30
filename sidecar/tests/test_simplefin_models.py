from __future__ import annotations

from decimal import Decimal

import pydantic
import pytest

from bookkeeper.simplefin.models import Account, AccountSet, Transaction


def test_transaction_parses_only_documented_fields():
    txn = Transaction.model_validate(
        {
            "id": "TXN-1",
            "posted": 1_700_000_000,
            "amount": "-42.10",
            "description": "SQ *COFFEE 4TH ST",
        }
    )
    assert txn.amount == Decimal("-42.10")
    assert isinstance(txn.amount, Decimal)
    assert txn.transacted_at is None
    assert txn.pending is None
    assert txn.extra is None


def test_transaction_rejects_undocumented_fields():
    with pytest.raises(pydantic.ValidationError):
        Transaction.model_validate(
            {
                "id": "TXN-1",
                "posted": 1_700_000_000,
                "amount": "-42.10",
                "description": "x",
                "merchant_category_code": "5812",  # not part of the protocol
            }
        )


def test_transaction_amount_rejects_json_float():
    with pytest.raises(pydantic.ValidationError, match="floats"):
        Transaction.model_validate(
            {
                "id": "TXN-1",
                "posted": 1_700_000_000,
                "amount": -42.10,  # bare JSON number, not a decimal string
                "description": "x",
            }
        )


def test_account_balance_rejects_json_float():
    with pytest.raises(pydantic.ValidationError, match="floats"):
        Account.model_validate(
            {
                "id": "ACT-1",
                "name": "Checking",
                "currency": "USD",
                "balance": 1234.56,
                "balance-date": 1_700_000_000,
            }
        )


def test_account_parses_hyphenated_aliases_and_preserves_decimal_precision():
    account = Account.model_validate(
        {
            "id": "ACT-1",
            "name": "Checking",
            "currency": "USD",
            "balance": "1234.50",
            "available-balance": "1200.00",
            "balance-date": 1_700_000_000,
        }
    )
    assert account.balance == Decimal("1234.50")
    assert str(account.balance) == "1234.50"  # trailing zero preserved, no float roundoff
    assert account.available_balance == Decimal("1200.00")


def test_account_set_parses_pending_flag_and_defaults():
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
                        "pending": True,
                    }
                ],
            }
        ],
    }
    account_set = AccountSet.model_validate(payload)
    assert len(account_set.accounts) == 1
    assert account_set.accounts[0].transactions[0].pending is True


def test_transaction_accepts_live_demo_fields_not_in_the_spec():
    # mcc/payee/memo: confirmed present on the live demo server
    # (team-lead, 2026-07-30) despite not appearing in
    # simplefin.org/protocol.html. Must not be rejected by extra="forbid".
    txn = Transaction.model_validate(
        {
            "id": "TXN-1",
            "posted": 1_700_000_000,
            "amount": "-42.10",
            "description": "SQ *COFFEE 4TH ST",
            "mcc": "5411",
            "payee": "Square Inc",
            "memo": "in-store purchase",
        }
    )
    assert txn.mcc == "5411"
    assert txn.payee == "Square Inc"
    assert txn.memo == "in-store purchase"


def test_transaction_still_rejects_truly_unknown_fields():
    # The strictness gate must still catch a field we haven't reviewed --
    # mcc/payee/memo are allow-listed by name, not by opening the gate.
    with pytest.raises(pydantic.ValidationError):
        Transaction.model_validate(
            {
                "id": "TXN-1",
                "posted": 1_700_000_000,
                "amount": "-42.10",
                "description": "x",
                "some_future_field_we_have_not_reviewed": "x",
            }
        )


def test_transaction_extra_captures_documented_example_verbatim():
    # The protocol doc's own example shows extra carrying category data
    # (`"extra": {"category": "food"}`) -- must be captured, not discarded.
    txn = Transaction.model_validate(
        {
            "id": "TXN-1",
            "posted": 1_700_000_000,
            "amount": "-42.10",
            "description": "x",
            "extra": {"category": "food"},
        }
    )
    assert txn.extra == {"category": "food"}


def test_account_set_parses_top_level_errors_list():
    # Not documented in the protocol spec, but live-observed: a >90-day
    # start-date/end-date window comes back as a soft error here (HTTP 200,
    # data still attached), separate from the documented `errlist`.
    payload = {
        "errlist": [],
        "accounts": [],
        "errors": ["Requested date range exceeds limit of 90 days and was capped."],
    }
    account_set = AccountSet.model_validate(payload)
    assert account_set.errors == ["Requested date range exceeds limit of 90 days and was capped."]


def test_account_set_errors_defaults_to_empty():
    account_set = AccountSet.model_validate({"errlist": [], "accounts": []})
    assert account_set.errors == []
