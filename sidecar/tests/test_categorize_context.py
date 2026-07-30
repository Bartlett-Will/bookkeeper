"""Tests for bookkeeper.categorize.context -- the shared ledger reader that
workers 3 and 4 build on top of.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from beancount import loader

from bookkeeper.categorize.context import build_ledger_context, uncategorized_transactions

_LEDGER = """
2026-01-01 open Assets:Checking                USD
2026-01-01 open Assets:Savings                 USD
2026-01-01 open Expenses:Unknown               USD
2026-01-01 open Income:Unknown                 USD
2026-01-01 open Expenses:Food:Groceries        USD
2026-01-01 open Expenses:Food:Dining           USD
2026-01-01 open Income:Salary                  USD

2026-01-05 * "SQ *COFFEE 4TH ST 8829"
  simplefin-id: "TXN-1"
  simplefin-account: "Assets:Checking"
  simplefin-mcc: "5812"
  simplefin-payee: "Coffee Co"
  Assets:Checking   -4.50 USD
  Expenses:Unknown

2026-01-06 * "TRADER JOES 123"
  simplefin-id: "TXN-2"
  simplefin-account: "Assets:Checking"
  Assets:Checking   -62.10 USD
  Expenses:Food:Groceries

2026-01-07 * "PAYROLL DEPOSIT"
  simplefin-id: "TXN-3"
  simplefin-account: "Assets:Checking"
  Assets:Checking   2500.00 USD
  Income:Unknown

2026-01-08 * "PAYROLL DEPOSIT 2"
  simplefin-id: "TXN-4"
  simplefin-account: "Assets:Checking"
  Assets:Checking   2500.00 USD
  Income:Salary

2026-01-09 * "TRANSFER TO SAVINGS"
  Assets:Checking   -100.00 USD
  Assets:Savings     100.00 USD
"""


def _load():
    entries, errors, _options = loader.load_string(_LEDGER)
    assert not errors, errors
    return entries


def test_build_ledger_context_accounts_excludes_unknown_and_asset_accounts():
    entries = _load()
    ctx = build_ledger_context(entries)

    assert "Expenses:Unknown" not in ctx.accounts
    assert "Income:Unknown" not in ctx.accounts
    assert "Assets:Checking" not in ctx.accounts
    assert "Assets:Savings" not in ctx.accounts
    assert set(ctx.accounts) == {
        "Expenses:Food:Groceries",
        "Expenses:Food:Dining",
        "Income:Salary",
    }


def test_build_ledger_context_accounts_sorted():
    entries = _load()
    ctx = build_ledger_context(entries)
    assert ctx.accounts == tuple(sorted(ctx.accounts))


def test_build_ledger_context_examples_only_include_categorized_postings():
    entries = _load()
    ctx = build_ledger_context(entries)

    accounts_seen = {ex.account for ex in ctx.examples}
    # The coffee shop and both payroll deposits into *Unknown* must not
    # appear as "confirmed" examples -- only the real Groceries and Salary
    # postings do.
    assert accounts_seen == {"Expenses:Food:Groceries", "Income:Salary"}


def test_build_ledger_context_examples_carry_normalized_description():
    entries = _load()
    ctx = build_ledger_context(entries)

    groceries_example = next(ex for ex in ctx.examples if ex.account == "Expenses:Food:Groceries")
    assert groceries_example.normalized_description == "trader joes"
    # This is the posting's own booked amount, standard beancount sign
    # convention (an Expenses account posting is positive) -- not the
    # asset-side-signed convention CategorizationInput.amount uses.
    assert groceries_example.amount == Decimal("62.10")
    assert groceries_example.posted_date == date(2026, 1, 6)


def test_uncategorized_transactions_finds_expense_and_income_unknown():
    entries = _load()
    inputs = uncategorized_transactions(entries)

    ids = {i.simplefin_id for i in inputs}
    assert ids == {"TXN-1", "TXN-3"}


def test_uncategorized_transactions_amount_is_signed_asset_side():
    entries = _load()
    inputs = uncategorized_transactions(entries)
    by_id = {i.simplefin_id: i for i in inputs}

    assert by_id["TXN-1"].amount == Decimal("-4.50")
    assert by_id["TXN-1"].asset_account == "Assets:Checking"
    assert by_id["TXN-1"].currency == "USD"
    assert by_id["TXN-3"].amount == Decimal("2500.00")


def test_uncategorized_transactions_carries_optional_metadata():
    entries = _load()
    inputs = uncategorized_transactions(entries)
    by_id = {i.simplefin_id: i for i in inputs}

    coffee = by_id["TXN-1"]
    assert coffee.mcc == "5812"
    assert coffee.payee == "Coffee Co"
    assert coffee.description == "SQ *COFFEE 4TH ST 8829"

    payroll = by_id["TXN-3"]
    assert payroll.mcc is None
    assert payroll.payee is None
    assert payroll.memo is None


def test_uncategorized_transactions_excludes_already_categorized():
    entries = _load()
    inputs = uncategorized_transactions(entries)
    ids = {i.simplefin_id for i in inputs}
    assert "TXN-2" not in ids
    assert "TXN-4" not in ids


def test_build_ledger_context_defaults_to_loading_real_ledger(fixture_root):
    # entries=None must fall through to bookkeeper.envelope.compute.load_ledger()
    fixture_root("basic")
    ctx = build_ledger_context()
    assert isinstance(ctx.accounts, tuple)


def test_uncategorized_transactions_defaults_to_loading_real_ledger(fixture_root):
    fixture_root("basic")
    result = uncategorized_transactions()
    assert isinstance(result, list)
