from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

from bookkeeper.categorize.memory import MemoryCategorizer
from bookkeeper.categorize.models import CategorizationInput, LedgerContext, Tier

ACCOUNTS = ("Expenses:Food:Groceries", "Expenses:Food:Dining", "Expenses:Utilities:Electric")


def _txn(description: str, amount: str = "-10.00") -> CategorizationInput:
    return CategorizationInput(
        description=description,
        amount=Decimal(amount),
        posted_date=date(2026, 1, 5),
        asset_account="Assets:Checking",
        simplefin_id="TXN-1",
    )


def test_abstains_when_description_never_seen(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "memory.json")
    ctx = LedgerContext(accounts=ACCOUNTS)
    assert cat.predict(_txn("SQ *COFFEE 4TH ST 8829"), ctx) is None


def test_predicts_after_confirmation(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "memory.json")
    ctx = LedgerContext(accounts=ACCOUNTS)
    cat.record_confirmation("coffee 4th st", "Expenses:Food:Dining")

    prediction = cat.predict(_txn("SQ *COFFEE 4TH ST 8829"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Dining"
    assert prediction.tier == Tier.MEMORY
    assert prediction.confidence == 1.0


def test_confidence_reflects_agreement_ratio_on_split_history(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "memory.json")
    ctx = LedgerContext(accounts=ACCOUNTS)
    cat.record_confirmation("pge web online", "Expenses:Utilities:Electric")
    cat.record_confirmation("pge web online", "Expenses:Utilities:Electric")
    cat.record_confirmation("pge web online", "Expenses:Utilities:Electric")
    cat.record_confirmation("pge web online", "Expenses:Food:Dining")

    prediction = cat.predict(_txn("PGE WEB ONLINE"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Utilities:Electric"  # majority wins
    assert prediction.confidence == 0.75


def test_abstains_on_genuine_tie(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "memory.json")
    ctx = LedgerContext(accounts=ACCOUNTS)
    cat.record_confirmation("ambiguous merchant", "Expenses:Food:Groceries")
    cat.record_confirmation("ambiguous merchant", "Expenses:Food:Dining")

    prediction = cat.predict(_txn("AMBIGUOUS MERCHANT"), ctx)
    assert prediction is None


def test_abstains_when_remembered_account_no_longer_open(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "memory.json")
    ctx = LedgerContext(accounts=("Expenses:Food:Groceries",))  # Dining not open
    cat.record_confirmation("some merchant", "Expenses:Food:Dining")

    prediction = cat.predict(_txn("SOME MERCHANT"), ctx)
    assert prediction is None


def test_record_confirmation_persists_atomically_to_disk(tmp_path):
    path = tmp_path / "memory.json"
    cat = MemoryCategorizer(path=path)
    cat.record_confirmation("coffee 4th st", "Expenses:Food:Dining")

    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk == {"coffee 4th st": {"Expenses:Food:Dining": 1}}

    # No leftover temp file.
    leftovers = [p for p in tmp_path.iterdir() if p != path]
    assert leftovers == []


def test_loads_existing_memory_file_on_construction(tmp_path):
    path = tmp_path / "memory.json"
    path.write_text(
        json.dumps({"coffee 4th st": {"Expenses:Food:Dining": 5}}), encoding="utf-8"
    )
    cat = MemoryCategorizer(path=path)
    ctx = LedgerContext(accounts=ACCOUNTS)

    prediction = cat.predict(_txn("SQ *COFFEE 4TH ST 8829"), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Dining"
    assert prediction.confidence == 1.0


def test_missing_file_starts_empty(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "does-not-exist.json")
    ctx = LedgerContext(accounts=ACCOUNTS)
    assert cat.predict(_txn("ANYTHING"), ctx) is None


def test_tier_attribute_is_memory(tmp_path):
    cat = MemoryCategorizer(path=tmp_path / "memory.json")
    assert cat.tier == Tier.MEMORY
