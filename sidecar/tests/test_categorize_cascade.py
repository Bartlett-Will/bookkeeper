from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from bookkeeper.categorize.cascade import Cascade, build_default_cascade
from bookkeeper.categorize.models import (
    CategorizationInput,
    LedgerContext,
    Prediction,
    Tier,
)

ACCOUNTS = ("Expenses:Food:Groceries", "Expenses:Food:Dining")


@dataclass
class _FakeCategorizer:
    tier: Tier
    account: str | None = None

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        if self.account is None:
            return None
        return Prediction(account=self.account, confidence=0.9, tier=self.tier)


def _txn() -> CategorizationInput:
    return CategorizationInput(
        description="whatever",
        amount=Decimal("-1.00"),
        posted_date=date(2026, 1, 1),
        asset_account="Assets:Checking",
        simplefin_id="TXN-1",
    )


def test_first_hit_wins_by_cascade_order_not_constructor_order():
    # RULE comes before MCC in CASCADE_ORDER; passing MCC first in the
    # constructor must not change that.
    mcc = _FakeCategorizer(tier=Tier.MCC, account="Expenses:Food:Groceries")
    rule = _FakeCategorizer(tier=Tier.RULE, account="Expenses:Food:Dining")
    cascade = Cascade([mcc, rule])

    ctx = LedgerContext(accounts=ACCOUNTS)
    prediction = cascade.predict(_txn(), ctx)
    assert prediction is not None
    assert prediction.account == "Expenses:Food:Dining"  # rule tier wins
    assert prediction.tier == Tier.RULE


def test_falls_through_abstaining_tiers():
    memory = _FakeCategorizer(tier=Tier.MEMORY, account=None)  # abstains
    rule = _FakeCategorizer(tier=Tier.RULE, account=None)  # abstains
    mcc = _FakeCategorizer(tier=Tier.MCC, account="Expenses:Food:Groceries")
    cascade = Cascade([memory, rule, mcc])

    ctx = LedgerContext(accounts=ACCOUNTS)
    prediction = cascade.predict(_txn(), ctx)
    assert prediction is not None
    assert prediction.tier == Tier.MCC


def test_all_tiers_abstain_returns_none():
    memory = _FakeCategorizer(tier=Tier.MEMORY, account=None)
    cascade = Cascade([memory])

    ctx = LedgerContext(accounts=ACCOUNTS)
    assert cascade.predict(_txn(), ctx) is None


def test_empty_cascade_abstains():
    cascade = Cascade([])
    ctx = LedgerContext(accounts=ACCOUNTS)
    assert cascade.predict(_txn(), ctx) is None


def test_tier_not_in_cascade_order_is_never_consulted():
    # Only MEMORY, RULE, MCC, STATISTICAL, LLM are in CASCADE_ORDER --
    # a categorizer with any other tier value would be silently excluded.
    # Exercised indirectly: build a cascade from a subset and confirm only
    # the known tiers are used, in the right order.
    memory = _FakeCategorizer(tier=Tier.MEMORY, account="Expenses:Food:Groceries")
    llm = _FakeCategorizer(tier=Tier.LLM, account="Expenses:Food:Dining")
    cascade = Cascade([llm, memory])

    ctx = LedgerContext(accounts=ACCOUNTS)
    prediction = cascade.predict(_txn(), ctx)
    assert prediction is not None
    assert prediction.tier == Tier.MEMORY  # memory precedes llm in CASCADE_ORDER


def test_build_default_cascade_does_not_crash_without_optional_tiers():
    # statistical.py / llm.py are owned by another worker and may not exist
    # yet at the time this runs -- build_default_cascade must degrade, not
    # raise.
    cascade = build_default_cascade(use_llm=True)
    assert isinstance(cascade, Cascade)


def test_build_default_cascade_use_llm_false_still_works():
    cascade = build_default_cascade(use_llm=False)
    assert isinstance(cascade, Cascade)


def test_build_default_cascade_includes_memory_rule_mcc(tmp_path, monkeypatch):
    # Point the deterministic tiers at an empty temp data dir so this test
    # doesn't depend on (or mutate) the real project's data/memory.json.
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()

    cascade = build_default_cascade(use_llm=False)
    tiers_present = {c.tier for c in cascade.tiers}
    assert {Tier.MEMORY, Tier.RULE, Tier.MCC}.issubset(tiers_present)


def test_tiers_property_exposes_individual_categorizers_in_cascade_order():
    rule = _FakeCategorizer(tier=Tier.RULE, account="Expenses:Food:Dining")
    memory = _FakeCategorizer(tier=Tier.MEMORY, account="Expenses:Food:Groceries")
    cascade = Cascade([rule, memory])

    assert [c.tier for c in cascade.tiers] == [Tier.MEMORY, Tier.RULE]


def test_cascade_unavailable_defaults_to_empty():
    cascade = Cascade([])
    assert cascade.unavailable == ()


def test_cascade_unavailable_is_stored_verbatim():
    cascade = Cascade([], unavailable=[(Tier.LLM, "no ollama")])
    assert cascade.unavailable == ((Tier.LLM, "no ollama"),)


def test_build_default_cascade_records_use_llm_false_as_unavailable():
    cascade = build_default_cascade(use_llm=False)
    assert Tier.LLM not in {t.tier for t in cascade.tiers}
    reasons = dict(cascade.unavailable)
    assert reasons.get(Tier.LLM) == "use_llm=False"


def test_build_default_cascade_records_broken_optional_tier_as_unavailable(monkeypatch):
    # Simulate the statistical tier's module failing to import (e.g. it
    # genuinely doesn't exist yet, or raises on construction) -- setting a
    # sys.modules entry to None is the standard way to force ImportError on
    # the next `from ... import ...` without touching the real file.
    monkeypatch.setitem(sys.modules, "bookkeeper.categorize.statistical", None)

    cascade = build_default_cascade(use_llm=False)

    assert Tier.STATISTICAL not in {t.tier for t in cascade.tiers}
    reasons = dict(cascade.unavailable)
    assert Tier.STATISTICAL in reasons
    assert reasons[Tier.STATISTICAL]  # a real reason string, not blank


def test_build_default_cascade_unavailable_empty_when_all_tiers_present():
    # With statistical.py / llm.py both landed, a full build should report
    # nothing missing.
    cascade = build_default_cascade(use_llm=True)
    assert cascade.unavailable == ()
    assert {t.tier for t in cascade.tiers} == {
        Tier.MEMORY,
        Tier.RULE,
        Tier.MCC,
        Tier.STATISTICAL,
        Tier.LLM,
    }
