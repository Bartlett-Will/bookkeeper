from __future__ import annotations

from datetime import date
from decimal import Decimal

from bookkeeper.categorize.mcc import MccCategorizer
from bookkeeper.categorize.models import CategorizationInput, LedgerContext, Tier


def _txn(mcc: str | None) -> CategorizationInput:
    return CategorizationInput(
        description="some charge",
        amount=Decimal("-10.00"),
        posted_date=date(2026, 1, 5),
        asset_account="Assets:Checking",
        simplefin_id="TXN-1",
        mcc=mcc,
    )


def test_abstains_when_no_mcc(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries",))
    assert cat.predict(_txn(None), ctx) is None


def test_abstains_when_mcc_unknown_to_table(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries",))
    assert cat.predict(_txn("9999"), ctx) is None


def test_groceries_mcc_matches_single_account(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries", "Expenses:Food:Dining"))

    prediction = cat.predict(_txn("5411"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Groceries"
    assert prediction.tier == Tier.MCC
    assert 0.0 < prediction.confidence < 1.0


def test_restaurant_mcc_matches_dining(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries", "Expenses:Food:Dining"))

    prediction = cat.predict(_txn("5812"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Dining"


def test_gas_mcc_matches_transport(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Transport:Gas",))

    prediction = cat.predict(_txn("5541"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Transport:Gas"


def test_pharmacy_mcc_matches_health(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Health",))

    prediction = cat.predict(_txn("5912"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Health"


def test_abstains_when_no_account_matches_intent(tmp_path):
    # Groceries MCC, but the ledger has no grocery-shaped account at all.
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Housing:Rent",))

    assert cat.predict(_txn("5411"), ctx) is None


def test_abstains_on_ambiguous_utilities_match(tmp_path):
    # This project's real chart of accounts: no single "Utilities" account,
    # three siblings under Expenses:Utilities:* instead -- generic-utility
    # MCC 4900 must abstain rather than guess among them.
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(
        accounts=(
            "Expenses:Utilities:Electric",
            "Expenses:Utilities:Water",
            "Expenses:Utilities:Internet",
        )
    )
    assert cat.predict(_txn("4900"), ctx) is None


def test_unambiguous_utilities_match_when_only_one_candidate(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Utilities",))

    prediction = cat.predict(_txn("4900"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Utilities"


def test_demo_server_restaurant_mislabeling_still_only_yields_lower_confidence(tmp_path):
    # Documented live finding: the demo bridge tags a bait-and-tackle shop
    # with MCC 5812 (Eating Places). This tier can't detect that -- MCC
    # trust is the whole point of testing it -- but its confidence for
    # restaurant codes must be honestly below the confident tiers'.
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    ctx = LedgerContext(accounts=("Expenses:Food:Dining",))

    prediction = cat.predict(_txn("5812"), ctx)
    assert prediction is not None
    assert prediction.confidence < 0.85


def test_user_override_takes_precedence_over_builtin_table(tmp_path):
    override_path = tmp_path / "mcc-map.yaml"
    override_path.write_text("'5812': Expenses:Food:Groceries\n", encoding="utf-8")
    cat = MccCategorizer(overrides_path=override_path)
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries", "Expenses:Food:Dining"))

    prediction = cat.predict(_txn("5812"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Groceries"  # override, not the builtin dining match
    assert "override" in prediction.rationale


def test_user_override_covers_mcc_missing_from_builtin_table(tmp_path):
    override_path = tmp_path / "mcc-map.yaml"
    override_path.write_text("'1234': Expenses:Food:Groceries\n", encoding="utf-8")
    cat = MccCategorizer(overrides_path=override_path)
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries",))

    prediction = cat.predict(_txn("1234"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Groceries"


def test_user_override_abstains_when_target_account_not_open(tmp_path):
    override_path = tmp_path / "mcc-map.yaml"
    override_path.write_text("'5812': Expenses:Does:Not:Exist\n", encoding="utf-8")
    cat = MccCategorizer(overrides_path=override_path)
    ctx = LedgerContext(accounts=("Expenses:Food:Dining",))

    assert cat.predict(_txn("5812"), ctx) is None


def test_missing_override_file_is_fine(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "does-not-exist.yaml")
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries",))
    prediction = cat.predict(_txn("5411"), ctx)
    assert prediction is not None


def test_tier_attribute_is_mcc(tmp_path):
    cat = MccCategorizer(overrides_path=tmp_path / "mcc-map.yaml")
    assert cat.tier == Tier.MCC
