"""Tests for the §5.5 accuracy harness, plus the CI regression gate.

Most of these drive `evaluate_corpus` with stub tiers rather than the real
cascade. That is deliberate: the harness's own correctness -- that the split
is deterministic, that held-out data never reaches the tiers, that abstention
is scored as silence rather than error, that a weak model gets auto-apply
turned off -- has to be established independently of whether any particular
tier is good. A harness that is wrong in the optimistic direction is worse
than no harness, because it would produce a confident, wrong argument for
letting software edit financial records unattended.

`test_regression_gate_*` is the exception: it runs the real cascade and is the
CI gate §5.5 asks for.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from bookkeeper.categorize.evaluate import (
    DEFAULT_CORPUS_PATH,
    MIN_THRESHOLD_SUPPORT,
    TARGET_PRECISION,
    Corpus,
    CorpusEntry,
    LedgerContext,
    build_examples,
    evaluate_corpus,
    load_corpus,
    recommend_threshold,
    run_eval,
    seed_memory_from_context,
    split_corpus,
    wilson_lower_bound,
)
from bookkeeper.categorize.models import (
    UNKNOWN_ACCOUNTS,
    CategorizationInput,
    Prediction,
    Tier,
    predict_is_valid,
)

#: Committed accuracy floor for the CI regression gate. Set from a measured
#: run with headroom, NOT aspirationally -- see docs/phase3-accuracy.md. A
#: drop below this fails the build.
#: Measured 90.0% on 2026-07-30 (238 train / 60 held out, seed 20260730).
#: Floor set 5 points below with headroom for tier tuning; it is a floor, not
#: a target, and lowering it needs a recorded reason in the report.
REGRESSION_FLOOR_NON_LLM_ACCURACY = 0.85

UPPER = str.upper


class _MappedTier:
    """A tier that answers from a fixed id -> (account, confidence) table.

    Lets a test state exactly which transactions a tier answers and how
    confidently, so the metric arithmetic can be checked against numbers
    computed by hand.
    """

    def __init__(self, tier: Tier, answers: dict[str, tuple[str, float]]) -> None:
        self.tier = tier
        self._answers = answers

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        got = self._answers.get(txn.simplefin_id)
        if got is None:
            return None
        account, confidence = got
        return Prediction(account=account, confidence=confidence, tier=self.tier)


def _entry(entry_id: str, account: str, day: int = 1, **kw) -> CorpusEntry:
    from datetime import date

    return CorpusEntry(
        entry_id=entry_id,
        posted_date=date(2026, 1, day),
        description=kw.pop("description", f"MERCHANT {entry_id}"),
        amount=kw.pop("amount", Decimal("-10.00")),
        account=account,
        **kw,
    )


def _toy_corpus(n: int = 100) -> Corpus:
    accounts = ("Expenses:Food:Groceries", "Expenses:Food:Dining")
    entries = tuple(
        _entry(f"t{i:03d}", accounts[i % 2], day=(i % 28) + 1) for i in range(n)
    )
    return Corpus(accounts=accounts, entries=entries)


# --------------------------------------------------------------------------
# The corpus fixture itself
# --------------------------------------------------------------------------


def test_corpus_fixture_is_committed_not_gitignored():
    """`data/eval/` is gitignored; a corpus there would leave CI nothing to run."""
    assert DEFAULT_CORPUS_PATH.exists()
    assert "tests/fixtures" in DEFAULT_CORPUS_PATH.as_posix()


def test_corpus_labels_are_all_in_the_closed_label_set():
    corpus = load_corpus()
    for entry in corpus.entries:
        assert entry.account in corpus.accounts
        assert entry.account not in UNKNOWN_ACCOUNTS


def test_corpus_is_not_degenerate():
    """Guards the fixture against being 'simplified' into the demo server.

    The demo server's 338 transactions carry 3 distinct descriptions and a
    48.5% majority class, which makes 'beats the majority-class baseline' pass
    without measuring anything. If a future edit collapses this corpus toward
    that shape, the exit criterion goes quietly vacuous -- so assert the
    properties that keep it meaningful.
    """
    corpus = load_corpus()
    descriptions = {e.description for e in corpus.entries}
    assert len(corpus.entries) >= 250
    assert len(descriptions) >= 200, "too few distinct surface forms to be a real test"
    assert len(corpus.accounts) >= 8

    counts: dict[str, int] = {}
    for entry in corpus.entries:
        counts[entry.account] = counts.get(entry.account, 0) + 1
    majority = max(counts.values()) / len(corpus.entries)
    assert majority < 0.40, f"majority class {majority:.1%} makes the baseline too strong"

    with_mcc = sum(1 for e in corpus.entries if e.mcc)
    assert 0 < with_mcc < len(corpus.entries), "MCC must be present on some, absent on others"


def test_corpus_header_admits_it_is_synthetic():
    """The fixture must not be mistakable for real measured data."""
    with DEFAULT_CORPUS_PATH.open() as fh:
        doc = json.load(fh)
    readme = " ".join(doc["_readme"]).lower()
    assert "synthetic" in readme
    assert "not real-world accuracy" in readme or "not real bank data" in readme


# --------------------------------------------------------------------------
# The split
# --------------------------------------------------------------------------


def test_split_is_deterministic():
    corpus = load_corpus()
    a_train, a_test = split_corpus(corpus)
    b_train, b_test = split_corpus(corpus)
    assert [e.entry_id for e in a_test] == [e.entry_id for e in b_test]
    assert [e.entry_id for e in a_train] == [e.entry_id for e in b_train]


def test_split_is_a_partition_of_roughly_the_right_size():
    corpus = load_corpus()
    train, test = split_corpus(corpus, holdout_fraction=0.2)
    train_ids = {e.entry_id for e in train}
    test_ids = {e.entry_id for e in test}
    assert train_ids.isdisjoint(test_ids)
    assert train_ids | test_ids == {e.entry_id for e in corpus.entries}
    assert abs(len(test) / len(corpus.entries) - 0.2) < 0.02


def test_split_is_insensitive_to_file_ordering():
    """Shuffling by sorted id, not file order, so re-sorting the fixture is safe."""
    corpus = load_corpus()
    reversed_corpus = Corpus(
        accounts=corpus.accounts, entries=tuple(reversed(corpus.entries)), path=corpus.path
    )
    _, test_a = split_corpus(corpus)
    _, test_b = split_corpus(reversed_corpus)
    assert {e.entry_id for e in test_a} == {e.entry_id for e in test_b}


def test_pinned_holdout_entries_are_never_in_training():
    corpus = load_corpus()
    train, test = split_corpus(corpus)
    pinned = {e.entry_id for e in corpus.entries if e.holdout}
    assert pinned, "the corpus is supposed to pin novel one-off merchants"
    assert pinned <= {e.entry_id for e in test}
    assert pinned.isdisjoint({e.entry_id for e in train})


def test_split_rejects_a_degenerate_fraction():
    with pytest.raises(ValueError):
        split_corpus(load_corpus(), holdout_fraction=1.0)


# --------------------------------------------------------------------------
# Leakage: the failure that would invalidate everything else
# --------------------------------------------------------------------------


def test_tiers_never_see_the_held_out_set():
    """The single easiest thing to get silently wrong here.

    A memory tier that has already been shown the test set scores 100% and
    measures nothing. Assert on the actual `LedgerContext` handed to the tier
    factory: every example must trace back to a training row, and no held-out
    row may be reconstructable from it.
    """
    corpus = load_corpus()
    train, test = split_corpus(corpus)
    captured: list[LedgerContext] = []

    def factory(ctx: LedgerContext):
        captured.append(ctx)
        return [_MappedTier(Tier.MEMORY, {})]

    evaluate_corpus(corpus, tier_factory=factory, normalize=UPPER)

    assert len(captured) == 1
    ctx = captured[0]
    train_keys = {(e.description, e.amount, e.posted_date) for e in train}
    test_keys = {(e.description, e.amount, e.posted_date) for e in test}
    example_keys = {(x.description, x.amount, x.posted_date) for x in ctx.examples}

    assert example_keys <= train_keys, "an example was built from outside the training split"
    assert example_keys.isdisjoint(test_keys - train_keys), "held-out data reached the tiers"
    assert len(ctx.examples) == len(train)


def test_seeded_memory_contains_no_held_out_descriptions(tmp_path):
    """The memory tier is the one place leakage would be invisible.

    It is backed by `data/memory.json`, i.e. global mutable state that the
    harness cannot otherwise control: in CI the file is absent and the tier
    scores 0% forever, while on a machine where `categorize --apply` has run
    it may already contain the held-out rows and score 100% by having been
    told the answers. So the harness seeds the table itself, and this asserts
    the seed is training-only.
    """
    from bookkeeper.categorize.evaluate import _default_normalizer

    corpus = load_corpus()
    normalize = _default_normalizer()
    train, test = split_corpus(corpus)
    ctx = LedgerContext(accounts=corpus.accounts, examples=build_examples(train, normalize))

    tier = seed_memory_from_context(ctx, tmp_path)
    table = json.loads((tmp_path / "memory.json").read_text())
    train_keys = {normalize(e.description) for e in train}
    assert set(table) <= train_keys

    # Held-out descriptions that never appear in training must be absent, or
    # the memory tier is answering from data it was never supposed to see.
    unseen = {normalize(e.description) for e in test} - train_keys
    assert unseen, "the corpus should contain held-out merchants unseen in training"
    assert not (set(table) & unseen)

    # And the seeded tier must actually abstain on them, not merely lack the key.
    for entry in test:
        if normalize(entry.description) in unseen:
            assert tier.predict(entry.to_input(), ctx) is None


def test_run_eval_does_not_read_the_real_memory_file(tmp_path, monkeypatch):
    """Eval results must not depend on the developer's local confirmations."""
    from bookkeeper.categorize import memory as memory_module

    contaminated = tmp_path / "memory.json"
    corpus = load_corpus()
    _, test = split_corpus(corpus)
    from bookkeeper.categorize.evaluate import _default_normalizer

    normalize = _default_normalizer()
    contaminated.write_text(
        json.dumps({normalize(e.description): {e.account: 99} for e in test})
    )
    monkeypatch.setattr(memory_module, "memory_path", lambda: contaminated)

    report = run_eval()
    memory = next(m for m in report.tiers if m.name == "memory")
    assert memory.coverage < 1.0, (
        "the memory tier answered every held-out transaction, which means it "
        "read the contaminated on-disk table instead of the seeded one"
    )


def test_build_examples_uses_the_supplied_normalizer():
    """Memory-tier lookups only match if both sides normalize identically."""
    entries = [_entry("a", "Expenses:Food:Dining", description="SQ *COFFEE 4TH ST 8829")]
    examples = build_examples(entries, lambda s: s.split()[0].lower())
    assert examples[0].normalized_description == "sq"
    assert examples[0].account == "Expenses:Food:Dining"


def test_label_set_is_the_corpus_label_set():
    """Tiers must be handed the closed set, so predictions can be validated."""
    corpus = load_corpus()
    captured: list[LedgerContext] = []

    def factory(ctx: LedgerContext):
        captured.append(ctx)
        return []

    evaluate_corpus(corpus, tier_factory=factory, normalize=UPPER)
    assert captured[0].accounts == corpus.accounts
    assert not set(captured[0].accounts) & UNKNOWN_ACCOUNTS


# --------------------------------------------------------------------------
# Metric arithmetic
# --------------------------------------------------------------------------


def test_abstention_lowers_coverage_but_not_precision():
    """The distinction the whole report is built on.

    A tier that answers 10 of 20 and gets all 10 right is 100% precise with
    50% coverage. Reporting it as 50% "accuracy" alone would make careful
    abstention look like failure.
    """
    corpus = _toy_corpus(100)
    _, test = split_corpus(corpus)
    answered = {e.entry_id: (e.account, 0.9) for e in test[: len(test) // 2]}

    report = evaluate_corpus(
        corpus,
        tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, answered)],
        normalize=UPPER,
    )
    metrics = report.tiers[0]
    assert metrics.answered == len(answered)
    assert metrics.precision == 1.0
    assert metrics.coverage == pytest.approx(len(answered) / report.n_test)
    assert metrics.accuracy == pytest.approx(len(answered) / report.n_test)


def test_a_wrong_answer_is_scored_worse_than_an_abstention():
    corpus = _toy_corpus(100)
    _, test = split_corpus(corpus)
    wrong = {e.entry_id: ("Expenses:Food:Dining", 0.9) for e in test}
    other = "Expenses:Food:Groceries"

    guessing = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, wrong)], normalize=UPPER
    )
    silent = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, {})], normalize=UPPER
    )
    assert guessing.tiers[0].coverage == 1.0
    assert guessing.tiers[0].precision < 1.0
    assert silent.tiers[0].coverage == 0.0
    # Silence is not scored as precision 100%; it is scored as no evidence.
    assert silent.tiers[0].precision == 0.0
    assert other in corpus.accounts


def test_cascade_is_first_hit_wins_and_a_later_tier_cannot_override():
    corpus = _toy_corpus(60)
    _, test = split_corpus(corpus)
    ids = [e.entry_id for e in test]
    first = _MappedTier(Tier.MEMORY, {i: ("Expenses:Food:Dining", 1.0) for i in ids})
    second = _MappedTier(Tier.RULE, {i: ("Expenses:Food:Groceries", 1.0) for i in ids})

    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [first, second], normalize=UPPER
    )
    truth = {e.entry_id: e.account for e in test}
    expected = sum(1 for i in ids if truth[i] == "Expenses:Food:Dining")
    assert report.cascade.correct == expected
    assert report.cascade.coverage == 1.0


def test_per_tier_metrics_are_independent_of_cascade_position():
    """A tier shadowed by an earlier one still gets its own measurement.

    Required for the MCC-vs-statistical ordering in models.py to be an
    overturnable hypothesis rather than a self-fulfilling one.
    """
    corpus = _toy_corpus(60)
    _, test = split_corpus(corpus)
    truth = {e.entry_id: (e.account, 0.8) for e in test}
    shadowed = _MappedTier(Tier.MCC, truth)
    always_first = _MappedTier(Tier.MEMORY, {i: ("Expenses:Food:Dining", 1.0) for i in truth})

    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [always_first, shadowed], normalize=UPPER
    )
    by_name = {m.name: m for m in report.tiers}
    assert by_name["mcc"].coverage == 1.0
    assert by_name["mcc"].precision == 1.0


def test_tiers_are_reported_in_cascade_order():
    corpus = _toy_corpus(40)
    report = evaluate_corpus(
        corpus,
        tier_factory=lambda ctx: [
            _MappedTier(Tier.STATISTICAL, {}),
            _MappedTier(Tier.MEMORY, {}),
        ],
        normalize=UPPER,
    )
    assert [m.name for m in report.tiers] == ["memory", "statistical"]


# --------------------------------------------------------------------------
# Baseline and the exit criterion
# --------------------------------------------------------------------------


def test_baseline_is_the_training_majority_class():
    accounts = ("Expenses:Food:Groceries", "Expenses:Food:Dining")
    entries = tuple(
        _entry(f"t{i:03d}", accounts[0] if i % 4 else accounts[1], day=(i % 28) + 1)
        for i in range(120)
    )
    corpus = Corpus(accounts=accounts, entries=entries)
    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, {})], normalize=UPPER
    )
    assert report.baseline_account == accounts[0]
    assert report.baseline_accuracy > 0.5


def test_ok_is_false_when_the_non_llm_cascade_does_not_beat_the_baseline():
    corpus = _toy_corpus(100)
    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, {})], normalize=UPPER
    )
    assert report.cascade_non_llm.accuracy == 0.0
    assert report.ok is False


def test_ok_is_true_only_on_the_non_llm_path():
    """A passing exit criterion must not depend on Ollama being installed."""
    corpus = _toy_corpus(100)
    _, test = split_corpus(corpus)
    truth = {e.entry_id: (e.account, 1.0) for e in test}

    llm_only = evaluate_corpus(
        corpus,
        tier_factory=lambda ctx: [_MappedTier(Tier.LLM, truth)],
        normalize=UPPER,
        used_llm=True,
    )
    assert llm_only.cascade.accuracy == 1.0
    assert llm_only.cascade_non_llm.accuracy == 0.0
    assert llm_only.ok is False

    deterministic = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.RULE, truth)], normalize=UPPER
    )
    assert deterministic.ok is True


# --------------------------------------------------------------------------
# Calibration and the auto-apply threshold
# --------------------------------------------------------------------------


def _scored(pairs: list[tuple[float, bool]]) -> list[tuple[Prediction, bool]]:
    return [
        (Prediction(account="Expenses:Food:Dining", confidence=c, tier=Tier.LLM), hit)
        for c, hit in pairs
    ]


def test_no_threshold_is_invented_when_nothing_clears_the_target():
    """§5.5 names 'auto-apply stays off' an acceptable outcome. Honour it."""
    rec = recommend_threshold(_scored([(0.99, i % 3 != 0) for i in range(120)]))
    assert rec.threshold is None
    assert rec.auto_apply is False
    assert f"{TARGET_PRECISION:.0%}" in rec.reason


def test_threshold_is_recommended_when_a_tail_measurably_clears_the_target():
    pairs = [(0.99, True) for _ in range(200)] + [(0.55, False) for _ in range(40)]
    rec = recommend_threshold(_scored(pairs))
    # 0.6 is the lowest cutoff that excludes every wrong answer, so it is the
    # right recommendation even though the correct predictions sat at 0.99.
    assert rec.threshold == 0.6
    assert rec.measured_precision == 1.0
    assert rec.support == 200
    assert rec.lower_bound is not None and rec.lower_bound >= TARGET_PRECISION


def test_threshold_is_the_lowest_safe_cutoff_not_the_highest():
    """Among equally safe cutoffs, the one that automates the most work wins."""
    pairs = [(0.72, True) for _ in range(80)] + [(0.55, False) for _ in range(40)]
    rec = recommend_threshold(_scored(pairs))
    assert rec.threshold == 0.6


def test_a_cutoff_of_zero_is_never_recommended():
    """0.0 is the absence of a threshold, not a calibrated one.

    Everything being right in one run must not be reported as "auto-apply
    regardless of confidence".
    """
    rec = recommend_threshold(_scored([(0.72, True) for _ in range(80)]))
    assert rec.threshold == 0.5


def test_a_clean_but_tiny_sample_does_not_earn_a_threshold():
    """Perfect precision over a handful of rows is not evidence."""
    pairs = [(0.99, True) for _ in range(MIN_THRESHOLD_SUPPORT - 1)]
    pairs += [(0.55, False) for _ in range(60)]
    rec = recommend_threshold(_scored(pairs))
    assert rec.threshold is None
    # The reason must distinguish "not accurate enough" from "not enough data
    # yet" -- they call for different follow-up.
    assert str(MIN_THRESHOLD_SUPPORT - 1) in rec.reason
    assert "lower bound" in rec.reason
    assert rec.measured_precision == 1.0
    assert rec.lower_bound is not None and rec.lower_bound < TARGET_PRECISION


def test_threshold_uses_the_cumulative_tail_not_a_single_bucket():
    """A good bucket under a bad one must not license auto-applying the tail.

    Auto-apply at t applies *everything* at or above t, so a bucket measured
    in isolation is the wrong unit.
    """
    pairs = [(0.92, True) for _ in range(40)] + [(0.99, False) for _ in range(40)]
    rec = recommend_threshold(_scored(pairs))
    assert rec.threshold is None


def test_wilson_bound_is_below_the_point_estimate_and_tightens_with_n():
    """The property the auto-apply gate depends on."""
    assert wilson_lower_bound(0, 0) == 0.0
    tight = wilson_lower_bound(1000, 1000)
    loose = wilson_lower_bound(10, 10)
    assert 0.0 < loose < tight < 1.0
    assert wilson_lower_bound(44, 45) < 44 / 45


def test_a_perfect_but_small_sample_cannot_establish_the_target():
    """44/45 is a 97.8% point estimate that does not license unattended writes.

    This is the case the harness actually hit on the committed corpus. The
    point estimate clears 95%; the interval around it does not, and a ledger
    is not the place to round that in our own favour.
    """
    rec = recommend_threshold(_scored([(0.99, i != 0) for i in range(45)]))
    assert rec.measured_precision is not None
    assert rec.measured_precision > TARGET_PRECISION
    assert rec.threshold is None


def test_calibration_buckets_partition_the_predictions():
    corpus = _toy_corpus(100)
    _, test = split_corpus(corpus)
    answers = {
        e.entry_id: (e.account, 0.5 + (i % 5) / 10) for i, e in enumerate(test)
    }
    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, answers)], normalize=UPPER
    )
    assert sum(b.n for b in report.buckets) == report.cascade.answered
    assert sum(b.correct for b in report.buckets) == report.cascade.correct


def test_confidence_of_exactly_one_lands_in_the_top_bucket():
    """Off-by-one at the closed upper edge would drop the most-trusted rows."""
    corpus = _toy_corpus(60)
    _, test = split_corpus(corpus)
    answers = {e.entry_id: (e.account, 1.0) for e in test}
    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, answers)], normalize=UPPER
    )
    assert report.buckets[-1].n == report.cascade.answered


def test_empty_predictions_leave_auto_apply_off():
    rec = recommend_threshold([])
    assert rec.threshold is None
    assert rec.reason


# --------------------------------------------------------------------------
# The rendered report
# --------------------------------------------------------------------------


def test_render_states_prominently_that_the_corpus_is_synthetic():
    """This output is the argument for unattended writes to a ledger.

    It must not be quotable as a real-world accuracy claim.
    """
    corpus = _toy_corpus(100)
    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, {})], normalize=UPPER
    )
    text = report.render()
    assert "SYNTHETIC CORPUS" in text
    assert "Phase 6" in text
    assert "AUTO-APPLY STAYS OFF" in text


def test_render_shows_coverage_precision_and_the_baseline():
    corpus = _toy_corpus(100)
    _, test = split_corpus(corpus)
    answers = {e.entry_id: (e.account, 0.8) for e in test}
    report = evaluate_corpus(
        corpus, tier_factory=lambda ctx: [_MappedTier(Tier.MEMORY, answers)], normalize=UPPER
    )
    text = report.render()
    assert "coverage" in text
    assert "precision" in text
    assert "majority baseline" in text
    assert "BEATS baseline" in text


def test_evaluate_rejects_an_empty_holdout():
    corpus = Corpus(accounts=("Expenses:Food:Dining",), entries=())
    with pytest.raises(ValueError):
        evaluate_corpus(corpus, tier_factory=lambda ctx: [], normalize=UPPER)


# --------------------------------------------------------------------------
# The real cascade: CI regression gate (§5.5)
# --------------------------------------------------------------------------


def test_run_eval_loads_the_committed_corpus_by_default():
    report = run_eval()
    assert report.n_total >= 250
    assert report.n_test > 0
    assert Path(report.corpus_path) == DEFAULT_CORPUS_PATH
    assert report.used_llm is False


def test_every_tier_prediction_names_an_account_in_the_closed_set():
    """`predict_is_valid`'s invariant, measured over the whole held-out set."""
    from bookkeeper.categorize.evaluate import _default_normalizer, _DefaultTierFactory

    corpus = load_corpus()
    train, test = split_corpus(corpus)
    ctx = LedgerContext(
        accounts=corpus.accounts, examples=build_examples(train, _default_normalizer())
    )
    for tier in _DefaultTierFactory(use_llm=False)(ctx):
        for entry in test:
            prediction = tier.predict(entry.to_input(), ctx)
            assert predict_is_valid(prediction, ctx), (
                f"{tier.tier.value} returned an out-of-set account: {prediction}"
            )
            assert prediction is None or prediction.account not in UNKNOWN_ACCOUNTS


def test_regression_gate_non_llm_cascade_beats_the_majority_baseline():
    """The Phase 3 exit criterion, enforced. Offline: no Ollama required."""
    report = run_eval()
    assert report.cascade_non_llm.accuracy > report.baseline_accuracy, report.render()
    assert report.ok is True


def test_regression_gate_accuracy_does_not_drop_below_the_committed_floor():
    """§5.5's 'an accuracy drop fails the build'.

    The floor is a measured number with a little headroom, not a target. If
    this fails, the cascade got worse -- move the floor down only with a
    recorded reason in docs/phase3-accuracy.md.
    """
    report = run_eval()
    assert report.cascade_non_llm.accuracy >= REGRESSION_FLOOR_NON_LLM_ACCURACY, report.render()
