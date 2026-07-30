from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bookkeeper.categorize.models import CategorizationInput, LedgerContext, Tier
from bookkeeper.categorize.rules import RuleCategorizer, RuleError

ACCOUNTS = ("Expenses:Utilities:Electric", "Expenses:Food:Groceries", "Income:Salary")


def _txn(
    description: str = "some charge",
    amount: str = "-10.00",
    payee: str | None = None,
    asset_account: str = "Assets:Checking",
) -> CategorizationInput:
    return CategorizationInput(
        description=description,
        amount=Decimal(amount),
        posted_date=date(2026, 1, 5),
        asset_account=asset_account,
        simplefin_id="TXN-1",
        payee=payee,
    )


def _write_rules(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_rules_file_means_no_rules(tmp_path):
    cat = RuleCategorizer(path=tmp_path / "rules.yaml")
    ctx = LedgerContext(accounts=ACCOUNTS)
    assert cat.predict(_txn("PG&E WEB ONLINE"), ctx) is None


def test_matches_pattern_against_description(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: PG&E
  pattern: "PG&E"
  account: "Expenses:Utilities:Electric"
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    prediction = cat.predict(_txn("ACH DEBIT - PG&E WEB ONLINE"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Utilities:Electric"
    assert prediction.tier == Tier.RULE
    assert prediction.confidence == 1.0
    assert "PG&E" in prediction.rationale


def test_matches_pattern_against_payee_too(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: Trader Joes
  pattern: "Trader Joe"
  account: "Expenses:Food:Groceries"
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    prediction = cat.predict(_txn("STORE 123", payee="Trader Joe's"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Groceries"


def test_first_matching_rule_wins_in_file_order(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: general grocery
  pattern: "STORE"
  account: "Expenses:Food:Groceries"
- name: specific pge
  pattern: "STORE"
  account: "Expenses:Utilities:Electric"
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    prediction = cat.predict(_txn("STORE 123"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Groceries"


def test_sign_predicate_restricts_to_spending(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: pge
  pattern: "PG&E"
  account: "Expenses:Utilities:Electric"
  sign: negative
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    assert cat.predict(_txn("PG&E REFUND", amount="15.00"), ctx) is None
    assert cat.predict(_txn("PG&E CHARGE", amount="-15.00"), ctx) is not None


def test_amount_min_max_predicates(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: big grocery run
  pattern: "MARKET"
  account: "Expenses:Food:Groceries"
  amount_min: -100
  amount_max: -20
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    assert cat.predict(_txn("MARKET", amount="-50.00"), ctx) is not None
    assert cat.predict(_txn("MARKET", amount="-5.00"), ctx) is None  # below amount_min bound
    assert cat.predict(_txn("MARKET", amount="-150.00"), ctx) is None  # above amount_max bound


def test_asset_account_predicate(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: checking only
  pattern: "STORE"
  account: "Expenses:Food:Groceries"
  asset_account: "Assets:Checking"
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    assert cat.predict(_txn("STORE", asset_account="Assets:Checking"), ctx) is not None
    assert cat.predict(_txn("STORE", asset_account="Assets:Savings"), ctx) is None


def test_no_match_abstains(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: pge
  pattern: "PG&E"
  account: "Expenses:Utilities:Electric"
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)
    assert cat.predict(_txn("SOMETHING ELSE ENTIRELY"), ctx) is None


def test_invalid_regex_raises_naming_the_rule(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: broken
  pattern: "["
  account: "Expenses:Food:Groceries"
""",
    )
    with pytest.raises(RuleError) as exc_info:
        RuleCategorizer(path=path)
    assert "broken" in str(exc_info.value)


def test_account_outside_ledger_raises_naming_the_rule(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: nonexistent target
  pattern: "PG&E"
  account: "Expenses:Does:Not:Exist"
""",
    )
    cat = RuleCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    with pytest.raises(RuleError) as exc_info:
        cat.predict(_txn("PG&E WEB ONLINE"), ctx)
    assert "nonexistent target" in str(exc_info.value)
    assert "Expenses:Does:Not:Exist" in str(exc_info.value)


def test_missing_pattern_field_raises(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: incomplete
  account: "Expenses:Food:Groceries"
""",
    )
    with pytest.raises(RuleError):
        RuleCategorizer(path=path)


def test_missing_account_field_raises(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: incomplete
  pattern: "STORE"
""",
    )
    with pytest.raises(RuleError):
        RuleCategorizer(path=path)


def test_invalid_sign_value_raises(tmp_path):
    path = _write_rules(
        tmp_path / "rules.yaml",
        """
- name: bad sign
  pattern: "STORE"
  account: "Expenses:Food:Groceries"
  sign: "sideways"
""",
    )
    with pytest.raises(RuleError):
        RuleCategorizer(path=path)


def test_non_list_yaml_raises(tmp_path):
    path = _write_rules(tmp_path / "rules.yaml", "not-a-list: true\n")
    with pytest.raises(RuleError):
        RuleCategorizer(path=path)


def test_starter_rules_yaml_is_valid_against_real_chart_of_accounts():
    # The shipped data/rules.yaml must load and validate cleanly against
    # the actual accounts.beancount chart -- this is the "committed
    # starter file must be valid" requirement, not just a schema check.
    from bookkeeper import paths

    root_rules_path = paths.data_dir() / "rules.yaml"
    assert root_rules_path.exists()
    cat = RuleCategorizer(path=root_rules_path)
    ctx = LedgerContext(
        accounts=(
            "Expenses:Utilities:Electric",
            "Expenses:Utilities:Water",
            "Expenses:Utilities:Internet",
            "Expenses:Food:Groceries",
        )
    )
    prediction = cat.predict(_txn("ACH DEBIT - PG&E WEB ONLINE"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Utilities:Electric"
