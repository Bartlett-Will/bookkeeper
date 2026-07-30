from __future__ import annotations

from datetime import date
from decimal import Decimal

from bookkeeper.categorize import statistical
from bookkeeper.categorize.models import (
    CategorizationInput,
    LabeledExample,
    LedgerContext,
    Tier,
    predict_is_valid,
)
from bookkeeper.categorize.statistical import StatisticalCategorizer

ACCOUNTS = (
    "Expenses:Food:Coffee",
    "Expenses:Food:Groceries",
    "Expenses:Home:Utilities",
    "Expenses:Auto:Fuel",
)

#: Merchant strings in the bank-mangled shapes PLAN.md §3.1 calls out, with
#: Groceries deliberately the majority class so the baseline this tier has to
#: beat is a real one rather than an even split.
MERCHANTS: dict[str, str] = {
    "SQ *COFFEE 4TH ST": "Expenses:Food:Coffee",
    "STARBUCKS STORE": "Expenses:Food:Coffee",
    "SAFEWAY GROCERY": "Expenses:Food:Groceries",
    "TRADER JOES MARKET": "Expenses:Food:Groceries",
    "WHOLE FOODS MKT": "Expenses:Food:Groceries",
    "COSTCO WHSE": "Expenses:Food:Groceries",
    "ACH DEBIT - PG&E WEB ONLINE": "Expenses:Home:Utilities",
    "COMCAST XFINITY": "Expenses:Auto:Fuel",
    "SHELL OIL": "Expenses:Auto:Fuel",
}


def trivial_normalizer(text: str) -> str:
    """Case-fold and collapse whitespace, and nothing else.

    Deliberately weaker than the real description normalizer: it leaves the
    store numbers in, so these tests measure the classifier's own ability to
    generalize across them rather than the normalizer's ability to strip them.
    """
    return " ".join(text.upper().split())


def make_categorizer() -> StatisticalCategorizer:
    return StatisticalCategorizer(normalizer=trivial_normalizer)


def variant(merchant: str, index: int) -> str:
    return f"{merchant} {1000 + index * 37:04d}"


def corpus(per_merchant: int, *, start: int = 0) -> tuple[LabeledExample, ...]:
    return tuple(
        LabeledExample(
            normalized_description=trivial_normalizer(variant(merchant, start + i)),
            account=account,
            description=variant(merchant, start + i),
        )
        for merchant, account in MERCHANTS.items()
        for i in range(per_merchant)
    )


def txn(description: str) -> CategorizationInput:
    return CategorizationInput(
        description=description,
        amount=Decimal("-12.34"),
        posted_date=date(2026, 7, 30),
        asset_account="Assets:SimpleFIN:Checking",
        simplefin_id=f"TXN-{description}",
    )


def test_constructs_without_an_injected_normalizer():
    # The default normalizer is resolved by a deferred import, so nothing but
    # a test constructing one this way notices if that import target moves.
    categorizer = StatisticalCategorizer()

    assert isinstance(categorizer._normalize("SQ *COFFEE 4TH ST 8829"), str)


def test_fits_and_predicts_a_known_merchant():
    ctx = LedgerContext(accounts=ACCOUNTS, examples=corpus(4))

    prediction = make_categorizer().predict(txn("SQ *COFFEE 4TH ST 8829"), ctx)

    assert prediction is not None
    assert prediction.account == "Expenses:Food:Coffee"
    assert prediction.tier is Tier.STATISTICAL
    assert predict_is_valid(prediction, ctx)
    assert "confirmed examples" in prediction.rationale


def test_abstains_on_a_corpus_too_small_to_fit():
    ctx = LedgerContext(
        accounts=ACCOUNTS,
        examples=(
            LabeledExample("SAFEWAY GROCERY 1000", "Expenses:Food:Groceries"),
            LabeledExample("SQ *COFFEE 4TH ST 1037", "Expenses:Food:Coffee"),
        ),
    )

    assert make_categorizer().predict(txn("SAFEWAY GROCERY 2200"), ctx) is None


def test_abstains_when_every_example_shares_one_account():
    # One class makes every posterior 1.0 by construction. That is the corpus
    # agreeing with itself, not a prediction.
    ctx = LedgerContext(
        accounts=ACCOUNTS,
        examples=tuple(
            LabeledExample(
                trivial_normalizer(variant("SAFEWAY GROCERY", i)),
                "Expenses:Food:Groceries",
            )
            for i in range(20)
        ),
    )

    assert make_categorizer().predict(txn("SAFEWAY GROCERY 9999"), ctx) is None


def test_abstains_when_nothing_in_the_description_was_ever_seen():
    ctx = LedgerContext(accounts=ACCOUNTS, examples=corpus(4))

    assert make_categorizer().predict(txn("ZZZZZ QQQQQ"), ctx) is None


def test_abstains_when_the_description_has_no_features():
    ctx = LedgerContext(accounts=ACCOUNTS, examples=corpus(4))

    assert make_categorizer().predict(txn("   ---   "), ctx) is None


def test_never_predicts_an_account_outside_the_closed_set():
    # A corpus carrying labels for accounts that are not open (a closed
    # account, or the Unknown catch-all that is an abstention rather than a
    # label) must not be able to produce one as an answer.
    polluted = corpus(4) + tuple(
        LabeledExample(f"MYSTERY CHARGE {i:04d}", account)
        for i, account in enumerate(["Expenses:Unknown"] * 10 + ["Expenses:Closed"] * 10)
    )
    ctx = LedgerContext(accounts=ACCOUNTS, examples=polluted)
    categorizer = make_categorizer()

    for description in ("MYSTERY CHARGE 0003", "SHELL OIL 7000", "SAFEWAY GROCERY 4"):
        prediction = categorizer.predict(txn(description), ctx)
        assert predict_is_valid(prediction, ctx)
        assert prediction is None or prediction.account in ACCOUNTS


def test_beats_a_majority_class_baseline_on_held_out_variants():
    train = corpus(6)
    ctx = LedgerContext(accounts=ACCOUNTS, examples=train)
    categorizer = make_categorizer()

    held_out = [
        (variant(merchant, 500 + i), account)
        for merchant, account in MERCHANTS.items()
        for i in range(4)
    ]
    majority_account = "Expenses:Food:Groceries"
    baseline = sum(1 for _, account in held_out if account == majority_account) / len(
        held_out
    )

    correct = 0
    answered = 0
    for description, expected in held_out:
        prediction = categorizer.predict(txn(description), ctx)
        if prediction is None:
            continue
        answered += 1
        correct += prediction.account == expected

    accuracy = correct / len(held_out)
    assert baseline > 0.3, "the baseline must be a real majority to be worth beating"
    assert answered == len(held_out), "every held-out variant shares n-grams with training"
    assert accuracy > baseline
    assert accuracy >= 0.9


def test_confidence_is_a_normalized_posterior_that_discriminates():
    ctx = LedgerContext(accounts=ACCOUNTS, examples=corpus(6))
    categorizer = make_categorizer()

    clear = categorizer.predict(txn("ACH DEBIT - PG&E WEB ONLINE 3300"), ctx)
    # "GROCERY" is shared vocabulary between two merchants in the corpus that
    # carry different accounts, so this one should not come back as certain.
    ambiguous = categorizer.predict(txn("WHOLE GROCERY OIL STORE"), ctx)

    assert clear is not None and ambiguous is not None
    assert 0.0 <= ambiguous.confidence < clear.confidence <= 1.0
    assert clear.confidence > 0.5
    # Not pinned at 1.0: §5.5 buckets predictions by confidence and calibrates
    # the auto-apply threshold against measured precision per bucket, which
    # needs the scores to actually spread out. Near-duplicate strings are the
    # easiest case this tier ever sees; if even they saturate, nothing sorts.
    assert clear.confidence < 0.99


def test_repeated_predicts_reuse_the_cached_fit(monkeypatch):
    fits: list[int] = []
    real_fit = statistical._fit_model

    def counting_fit(examples, accounts):
        fits.append(len(examples))
        return real_fit(examples, accounts)

    monkeypatch.setattr(statistical, "_fit_model", counting_fit)

    ctx = LedgerContext(accounts=ACCOUNTS, examples=corpus(4))
    categorizer = make_categorizer()
    for i in range(25):
        categorizer.predict(txn(variant("SAFEWAY GROCERY", 900 + i)), ctx)

    assert len(fits) == 1


def test_an_unfittable_corpus_is_diagnosed_once_not_per_transaction(monkeypatch):
    fits: list[int] = []
    real_fit = statistical._fit_model

    def counting_fit(examples, accounts):
        fits.append(len(examples))
        return real_fit(examples, accounts)

    monkeypatch.setattr(statistical, "_fit_model", counting_fit)

    ctx = LedgerContext(accounts=ACCOUNTS, examples=())
    categorizer = make_categorizer()
    for i in range(10):
        assert categorizer.predict(txn(f"ANYTHING {i}"), ctx) is None

    assert len(fits) == 1


def test_a_changed_corpus_refits():
    categorizer = make_categorizer()
    small = LedgerContext(accounts=ACCOUNTS, examples=corpus(1)[:3])
    assert categorizer.predict(txn("SHELL OIL 5000"), small) is None

    grown = LedgerContext(accounts=ACCOUNTS, examples=corpus(4))
    prediction = categorizer.predict(txn("SHELL OIL 5000"), grown)

    assert prediction is not None
    assert prediction.account == "Expenses:Auto:Fuel"


def test_a_changed_account_set_refits():
    # `accounts` decides which examples are eligible, so closing an account
    # changes the fit even though not one example moved.
    examples = corpus(4)
    categorizer = make_categorizer()
    assert (
        categorizer.predict(txn("SHELL OIL 5000"), LedgerContext(ACCOUNTS, examples))
        is not None
    )

    without_fuel = tuple(a for a in ACCOUNTS if a != "Expenses:Auto:Fuel")
    prediction = categorizer.predict(
        txn("SHELL OIL 5000"), LedgerContext(without_fuel, examples)
    )

    assert prediction is None or prediction.account != "Expenses:Auto:Fuel"


def test_equal_corpora_from_different_objects_share_the_fit(monkeypatch):
    fits: list[int] = []
    real_fit = statistical._fit_model

    def counting_fit(examples, accounts):
        fits.append(len(examples))
        return real_fit(examples, accounts)

    monkeypatch.setattr(statistical, "_fit_model", counting_fit)

    categorizer = make_categorizer()
    categorizer.predict(txn("SHELL OIL 1"), LedgerContext(ACCOUNTS, corpus(4)))
    categorizer.predict(txn("SHELL OIL 2"), LedgerContext(ACCOUNTS, corpus(4)))

    assert len(fits) == 1
