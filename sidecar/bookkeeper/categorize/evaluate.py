"""Accuracy measurement for the tiered categorizer (PLAN.md §5.5).

This module exists to answer one question: *is the cascade trustworthy enough
to be allowed to modify financial records without a human looking?* Everything
here is shaped by that. In particular:

- **Coverage and precision are reported separately, never merged.** A tier that
  abstains is silent, not wrong. Folding abstentions into an accuracy number
  makes a careful tier look terrible and a guessing tier look good, which is
  exactly backwards for deciding what to trust.
- **Self-reported confidence is not believed.** §5.5 is explicit that it is a
  weak signal. It is used only as an *ordering* over predictions; the number
  that matters is the precision we measure inside each confidence bucket.
- **"No safe threshold" is a valid, expected result.** If no confidence band
  clears the target precision on enough samples, this reports auto-apply OFF.
  There is no fallback that invents a threshold, because an untrustworthy
  auto-apply is worse than none.

Leakage is the failure mode that would silently invalidate all of it: a memory
tier that has already seen the held-out set measures nothing. So the harness
never calls `build_ledger_context()` -- it constructs `LedgerContext` itself,
with `examples` derived from the *training* split alone, and
`tests/test_categorize_eval.py` asserts that property directly.

The corpus is synthetic (see the `_readme` in the fixture). These numbers bound
the cascade's mechanics, not real-world accuracy; that is a Phase 6 question.
"""

from __future__ import annotations

import json
import math
import random
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from bookkeeper.categorize.models import (
    CASCADE_ORDER,
    CategorizationInput,
    Categorizer,
    LabeledExample,
    LedgerContext,
    Prediction,
    Tier,
)

#: Committed under `tests/fixtures/`, deliberately not `data/eval/`: that path
#: is gitignored, so a corpus placed there would leave CI with nothing to run
#: and make the §5.5 regression gate decorative.
DEFAULT_CORPUS_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "merchant_corpus.json"
)

#: §5.5's "random ~20% withheld from tiers 1-3 training".
DEFAULT_HOLDOUT_FRACTION = 0.2

#: Fixed so the reported numbers do not wobble between runs. A CI gate that
#: moves on its own is not a gate.
DEFAULT_SEED = 20260730

#: §5.5: "the auto-apply threshold is set where measured precision clears a
#: target (start at 95%)".
TARGET_PRECISION = 0.95

#: A threshold justified by five samples is not justified. Below this many
#: predictions in the qualifying band, the honest answer is "not enough
#: evidence", which lands in the same place as failing the target: off.
MIN_THRESHOLD_SUPPORT = 20

#: z for a 95% two-sided normal interval, used by the Wilson score bound.
WILSON_Z = 1.96

#: Confidence bucket edges. The upper bands are narrow because that is where
#: the auto-apply decision actually gets made -- resolution below 0.5 is
#: worthless for it.
BUCKET_EDGES: tuple[float, ...] = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0)

#: A tier is "deterministic or statistical" -- i.e. one of PLAN.md §5.4's
#: tiers 1-3, plus the §3.1 MCC amendment tier -- if it is not the LLM. The
#: Phase 3 exit criterion "tiers 1-3 alone beat a majority-class baseline" is
#: measured against exactly this subset: the cascade as it runs with no model.
NON_LLM_TIERS: frozenset[Tier] = frozenset(t for t in CASCADE_ORDER if t is not Tier.LLM)


@dataclass(frozen=True)
class CorpusEntry:
    """One labeled transaction: an input plus its ground-truth account."""

    entry_id: str
    posted_date: date
    description: str
    amount: Decimal
    account: str
    mcc: str | None = None
    payee: str | None = None
    #: Pinned to the held-out side by the corpus author rather than by the
    #: RNG. Used for novel one-off merchants that must be unseen by
    #: construction. This makes the test set *harder* than a purely random
    #: one, which is the conservative direction for an autonomy decision.
    holdout: bool = False

    def to_input(self, asset_account: str = "Assets:Checking") -> CategorizationInput:
        return CategorizationInput(
            description=self.description,
            amount=self.amount,
            posted_date=self.posted_date,
            asset_account=asset_account,
            simplefin_id=self.entry_id,
            mcc=self.mcc,
            payee=self.payee,
        )


@dataclass(frozen=True)
class Corpus:
    accounts: tuple[str, ...]
    entries: tuple[CorpusEntry, ...]
    path: Path | None = None


def load_corpus(path: str | Path | None = None) -> Corpus:
    """Read a labeled corpus JSON. `None` means the committed fixture."""
    resolved = Path(path) if path is not None else DEFAULT_CORPUS_PATH
    with resolved.open() as fh:
        doc = json.load(fh)
    entries = tuple(
        CorpusEntry(
            entry_id=raw["id"],
            posted_date=date.fromisoformat(raw["date"]),
            description=raw["description"],
            amount=Decimal(raw["amount"]),
            account=raw["account"],
            mcc=raw.get("mcc"),
            payee=raw.get("payee"),
            holdout=bool(raw.get("holdout", False)),
        )
        for raw in doc["entries"]
    )
    return Corpus(accounts=tuple(doc["accounts"]), entries=entries, path=resolved)


def split_corpus(
    corpus: Corpus,
    *,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    seed: int = DEFAULT_SEED,
) -> tuple[tuple[CorpusEntry, ...], tuple[CorpusEntry, ...]]:
    """Split into `(train, test)`, deterministically.

    Entries flagged `holdout` go to the test side unconditionally; the rest
    are shuffled with a fixed seed and drawn from until the test side reaches
    `holdout_fraction` of the corpus. Sorting by id before shuffling makes the
    result independent of the order entries happen to appear in the file.

    The split is per-transaction, not per-merchant, because that is what §5.5
    describes ("as the user confirms categorizations, a random ~20% is
    withheld"). A merchant appearing on both sides is the intended, realistic
    case -- it is how the memory tier is supposed to earn its keep.
    """
    if not 0.0 < holdout_fraction < 1.0:
        raise ValueError(f"holdout_fraction must be in (0, 1), got {holdout_fraction}")

    pinned = [e for e in corpus.entries if e.holdout]
    pool = sorted((e for e in corpus.entries if not e.holdout), key=lambda e: e.entry_id)
    random.Random(seed).shuffle(pool)

    target = round(len(corpus.entries) * holdout_fraction)
    take = max(0, target - len(pinned))
    test = pinned + pool[:take]
    train = pool[take:]

    def by_date(entry: CorpusEntry) -> tuple[date, str]:
        return entry.posted_date, entry.entry_id

    return tuple(sorted(train, key=by_date)), tuple(sorted(test, key=by_date))


#: Turns a raw bank description into the memory tier's lookup key. Injectable
#: so the harness is testable before `categorize.normalize` exists and so a
#: change to normalization can be measured rather than assumed.
Normalizer = Callable[[str], str]

#: Builds the tiers under test from the training-only context. Injectable
#: because per-tier measurement requires the individual `Categorizer`s, and
#: because tests need to drive the harness with stubs.
TierFactory = Callable[[LedgerContext], Sequence[Categorizer]]


def _default_normalizer() -> Normalizer:
    from bookkeeper.categorize.normalize import normalize_description

    return normalize_description


def seed_memory_from_context(ctx: LedgerContext, directory: Path) -> Categorizer:
    """A `MemoryCategorizer` holding exactly the training split's confirmations.

    `MemoryCategorizer` is backed by `data/memory.json` rather than by
    `ctx.examples` -- a deliberate choice on its side, since memory is meant
    to grow only through explicit human confirmation. But that makes it read
    *global mutable state*, which breaks measurement in both directions:

    - In CI, where the file does not exist, the tier abstains on everything
      and reports 0% coverage forever. That is not a measurement of the tier.
    - On a developer machine where `bookkeeper categorize --apply` has run,
      the file may already contain the held-out transactions, and the tier
      scores 100% by having been told the answers. Measured on this corpus,
      that inflation is 63.3% coverage to 100.0% -- silent and large.

    So the harness builds the tier's state itself from the training split
    only. The held-out discipline of §5.5 is not optional, and it cannot be
    enforced on a tier that reads whatever happens to be on disk.

    Seeding goes through the tier's own `record_confirmation` rather than by
    writing its JSON directly: the on-disk layout belongs to the memory tier,
    and a harness that hand-rolled that format would keep reporting confident
    numbers after the format changed under it.
    """
    from bookkeeper.categorize.memory import MemoryCategorizer

    tier = MemoryCategorizer(path=directory / "memory.json")
    for example in ctx.examples:
        tier.record_confirmation(example.normalized_description, example.account)
    return tier


class _DefaultTierFactory:
    """The production cascade's tiers, with memory reseeded from training data.

    A class rather than a closure so the caller can read back which tiers the
    cascade could not assemble. A tier missing from the report's table means
    something quite different from a tier measuring badly, and the difference
    has to survive out to the rendered output.
    """

    def __init__(self, *, use_llm: bool, memory_dir: Path | None = None) -> None:
        self._use_llm = use_llm
        self._memory_dir = memory_dir
        self.unavailable: tuple[tuple[Tier, str], ...] = ()

    def __call__(self, ctx: LedgerContext) -> Sequence[Categorizer]:
        from bookkeeper.categorize.cascade import build_default_cascade

        cascade = build_default_cascade(use_llm=self._use_llm)
        tiers = getattr(cascade, "tiers", None)
        if tiers is None:
            raise AttributeError(
                "Cascade exposes no `tiers`; per-tier metrics (§5.5) need the "
                "individual Categorizers, not just the first-hit-wins result."
            )
        self.unavailable = tuple(getattr(cascade, "unavailable", ()))
        if self._memory_dir is None:
            return tiers

        # The rule tier reads `data/rules.yaml` off disk, exactly as the memory
        # tier reads `data/memory.json` -- so the same argument applies and was
        # only half-applied. The corpus has its own ten-account label set, and a
        # rule naming an account that is open in the *real* ledger but not in
        # the corpus raises `RuleError`, turning "Accuracy regression gate"
        # red with a message that reads as a ledger problem when it is a
        # mismatch between two unrelated account sets. Adding a rule is the
        # documented purpose of that file, and `suggest-rules` exists to
        # produce them, so this fires the first time anyone acts on its output.
        #
        # Pointed at an empty file rather than the real one: the gate measures
        # the cascade against a fixed corpus, and a tier reading mutable local
        # state cannot be part of a regression bound that is supposed to mean
        # the same thing on two machines.
        empty_rules = self._memory_dir / "rules-for-eval.yaml"
        if not empty_rules.exists():
            empty_rules.write_text("[]\n", encoding="utf-8")

        from bookkeeper.categorize.rules import RuleCategorizer

        seeded = seed_memory_from_context(ctx, self._memory_dir)
        isolated_rules = RuleCategorizer(path=empty_rules)
        swapped: list[Categorizer] = []
        for tier in tiers:
            if tier.tier is Tier.MEMORY:
                swapped.append(seeded)
            elif tier.tier is Tier.RULE:
                swapped.append(isolated_rules)
            else:
                swapped.append(tier)
        return tuple(swapped)


def build_examples(
    entries: Sequence[CorpusEntry], normalize: Normalizer
) -> tuple[LabeledExample, ...]:
    """Confirmed history, as tiers 1 and 3 get to see it.

    Only ever called on the training split. This function is the single point
    where corpus rows become training signal, which is what makes the leakage
    property assertable in one place.
    """
    return tuple(
        LabeledExample(
            normalized_description=normalize(e.description),
            account=e.account,
            description=e.description,
            amount=e.amount,
            posted_date=e.posted_date,
            mcc=e.mcc,
            payee=e.payee,
        )
        for e in entries
    )


@dataclass(frozen=True)
class TierMetrics:
    """Per-tier outcome counts. Rates are derived, never stored rounded."""

    name: str
    total: int
    answered: int
    correct: int

    @property
    def coverage(self) -> float:
        """Share of held-out transactions this tier answered at all."""
        return self.answered / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        """Correct / answered. Undefined-as-0 when the tier never answered."""
        return self.correct / self.answered if self.answered else 0.0

    @property
    def accuracy(self) -> float:
        """Top-1 over the whole held-out set: abstaining does not count.

        Reported *alongside* coverage rather than instead of it. On its own
        this number punishes a tier for correctly declining to guess.
        """
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class ConfidenceBucket:
    lo: float
    hi: float
    n: int
    correct: int

    @property
    def precision(self) -> float:
        return self.correct / self.n if self.n else 0.0

    @property
    def label(self) -> str:
        closing = "]" if self.hi >= 1.0 else ")"
        return f"[{self.lo:.2f}, {self.hi:.2f}{closing}"


def wilson_lower_bound(correct: int, n: int, z: float = WILSON_Z) -> float:
    """Lower end of the Wilson score interval for a proportion.

    Why the raw precision is not enough on its own: 44 correct out of 45 is a
    97.8% point estimate, but the sample is small enough that the true rate
    could comfortably be 88%. Recommending unattended writes to a ledger off
    that number would be reading noise as evidence. The Wilson bound answers
    the question actually being asked -- "can this sample *establish* 95%
    precision?" -- and it degrades sensibly at small n and at p near 1, where
    the textbook normal interval falls apart.

    Pure arithmetic: no scipy, no numpy, per the dependency budget.
    """
    if n <= 0:
        return 0.0
    p = correct / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return max(0.0, (centre - margin) / denominator)


@dataclass(frozen=True)
class ThresholdRecommendation:
    """The auto-apply decision, and why.

    `threshold is None` means auto-apply stays off. §5.5 names that an
    acceptable outcome, so nothing here degrades to a made-up number when the
    evidence is absent.

    `measured_precision` and `lower_bound` are both reported even when the
    recommendation is "off", because they fail differently: a low point
    estimate means the cascade is not accurate enough, while a good point
    estimate with a weak lower bound means there is not yet enough held-out
    data to prove it. The first is a modelling problem, the second is a
    Phase 6 data problem, and collapsing them would send someone off to fix
    the wrong thing.
    """

    threshold: float | None
    measured_precision: float | None
    support: int
    reason: str
    lower_bound: float | None = None

    @property
    def auto_apply(self) -> bool:
        return self.threshold is not None


@dataclass(frozen=True)
class EvalReport:
    """The evidence for an autonomy decision. `render()` is the artifact."""

    corpus_path: str
    n_total: int
    n_train: int
    n_test: int
    seed: int
    used_llm: bool
    baseline_account: str
    baseline_correct: int
    tiers: tuple[TierMetrics, ...]
    cascade: TierMetrics
    cascade_non_llm: TierMetrics
    buckets: tuple[ConfidenceBucket, ...]
    recommendation: ThresholdRecommendation
    accounts: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def baseline_accuracy(self) -> float:
        """Always predict the most frequent *training* account. The floor."""
        return self.baseline_correct / self.n_test if self.n_test else 0.0

    @property
    def ok(self) -> bool:
        """The Phase 3 exit criterion, as a boolean.

        "Tiers 1-3 alone beat a majority-class baseline" -- measured on the
        no-LLM cascade, so a passing result never depends on a model being
        installed. The LLM tier can only add on top of this.
        """
        return self.cascade_non_llm.accuracy > self.baseline_accuracy

    def render(self) -> str:
        w = 78
        out: list[str] = []
        out.append("=" * w)
        out.append("CATEGORIZATION ACCURACY (PLAN.md §5.5)")
        out.append("=" * w)
        out.append("")
        out.append("SYNTHETIC CORPUS. These numbers bound the cascade's mechanics, not")
        out.append("real-world accuracy. Real accuracy is a Phase 6 question against real")
        out.append("bank data. Do not cite these as product accuracy.")
        out.append("")
        out.append(f"corpus      {self.corpus_path}")
        out.append(
            f"split       {self.n_train} train / {self.n_test} held out "
            f"of {self.n_total} (seed {self.seed})"
        )
        out.append(f"label set   {len(self.accounts)} accounts")
        out.append(f"llm tier    {'included' if self.used_llm else 'excluded'}")
        out.append("")

        out.append("-" * w)
        out.append("PER-TIER, run independently over the held-out set")
        out.append("-" * w)
        out.append(f"{'tier':<14}{'coverage':>11}{'precision':>12}{'top-1 acc':>12}{'ans':>7}{'ok':>6}")
        for m in self.tiers:
            out.append(
                f"{m.name:<14}{m.coverage:>10.1%} {m.precision:>11.1%} "
                f"{m.accuracy:>11.1%} {m.answered:>6} {m.correct:>5}"
            )
        out.append("")
        out.append("coverage  = share of held-out transactions the tier answered at all")
        out.append("precision = correct / answered. Abstention is silence, not error.")
        out.append("top-1 acc = correct / all held out. Penalizes abstention; read with")
        out.append("            coverage, never alone.")
        out.append("")

        out.append("-" * w)
        out.append("CASCADE, first-hit-wins, vs baseline")
        out.append("-" * w)
        base = self.baseline_accuracy
        out.append(
            f"{'majority baseline':<26}{base:>9.1%}   always predict {self.baseline_account}"
        )
        n = self.cascade_non_llm
        out.append(
            f"{'cascade (no LLM)':<26}{n.accuracy:>9.1%}   "
            f"coverage {n.coverage:.1%}, precision {n.precision:.1%}"
        )
        c = self.cascade
        if self.used_llm:
            out.append(
                f"{'cascade (with LLM)':<26}{c.accuracy:>9.1%}   "
                f"coverage {c.coverage:.1%}, precision {c.precision:.1%}"
            )
        delta = n.accuracy - base
        verdict = "BEATS baseline" if delta > 0 else "DOES NOT beat baseline"
        out.append("")
        out.append(f"Exit criterion (tiers 1-3 alone beat majority class): {verdict}")
        out.append(f"  margin {delta:+.1%}")
        out.append("")

        out.append("-" * w)
        out.append("CALIBRATION: measured precision per self-reported confidence bucket")
        out.append("-" * w)
        out.append(f"{'bucket':<16}{'n':>6}{'correct':>10}{'measured prec':>16}")
        for b in self.buckets:
            if b.n == 0:
                out.append(f"{b.label:<16}{0:>6}{'-':>10}{'-':>16}")
            else:
                out.append(f"{b.label:<16}{b.n:>6}{b.correct:>10}{b.precision:>15.1%}")
        out.append("")
        out.append("Self-reported confidence is NOT trusted as a probability (§5.5). The")
        out.append("only number that counts is the measured precision in each band.")
        out.append("")

        out.append("-" * w)
        out.append("AUTO-APPLY DECISION")
        out.append("-" * w)
        r = self.recommendation
        if r.auto_apply:
            out.append(f"  RECOMMENDATION: auto-apply at confidence >= {r.threshold:.2f}")
            out.append(
                f"  measured precision {r.measured_precision:.1%} over {r.support} "
                f"held-out predictions, 95% lower bound {r.lower_bound:.1%} "
                f"(target {TARGET_PRECISION:.0%})"
            )
        else:
            out.append("  RECOMMENDATION: AUTO-APPLY STAYS OFF. Review everything.")
            if r.measured_precision is not None:
                out.append(
                    f"  best band: {r.measured_precision:.1%} measured over {r.support} "
                    f"held-out predictions, 95% lower bound "
                    f"{r.lower_bound:.1%} (target {TARGET_PRECISION:.0%})"
                )
        for line in _wrap(r.reason, 74):
            out.append(f"  {line}")
        out.append("")
        for note in self.notes:
            wrapped = _wrap(note, 72)
            out.append(f"note: {wrapped[0]}")
            out.extend(f"      {line}" for line in wrapped[1:])
        if self.notes:
            out.append("")
        out.append("=" * w)
        return "\n".join(out)


def _wrap(text: str, width: int) -> list[str]:
    """Greedy wrap. The reasons are long by design -- they carry the argument."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _bucket_predictions(
    scored: Sequence[tuple[Prediction, bool]],
) -> tuple[ConfidenceBucket, ...]:
    buckets: list[ConfidenceBucket] = []
    for lo, hi in pairwise(BUCKET_EDGES):
        last = hi >= 1.0
        members = [
            hit
            for pred, hit in scored
            if lo <= pred.confidence and (pred.confidence <= hi if last else pred.confidence < hi)
        ]
        buckets.append(
            ConfidenceBucket(lo=lo, hi=hi, n=len(members), correct=sum(members))
        )
    return tuple(buckets)


def recommend_threshold(
    scored: Sequence[tuple[Prediction, bool]],
    *,
    target: float = TARGET_PRECISION,
    min_support: int = MIN_THRESHOLD_SUPPORT,
) -> ThresholdRecommendation:
    """Lowest confidence cutoff whose *tail* measures at or above `target`.

    Evaluated cumulatively (everything at or above the cutoff), not
    bucket-by-bucket, because that is what auto-apply would actually do: it
    applies the whole tail. A single bucket clearing 95% while the band above
    it does not would be a measurement artifact, not a safe cutoff.

    Lowest qualifying cutoff wins, since among cutoffs that are equally safe
    the one that automates the most work is the useful one.

    Cutoff 0.0 is deliberately not a candidate. It would mean "apply
    everything regardless of confidence", which is not a calibrated threshold
    but the absence of one, and it can be selected by accident whenever every
    prediction in a small run happens to be right. The lowest cutoff that can
    be recommended is 0.5: auto-applying a prediction the tier itself calls
    less than half-confident is not a decision this report will endorse.
    """
    if not scored:
        return ThresholdRecommendation(
            None, None, 0, "No predictions were made on the held-out set at all."
        )

    best_seen: tuple[float, float, float, int] | None = None
    for cutoff in BUCKET_EDGES[1:]:
        tail = [hit for pred, hit in scored if pred.confidence >= cutoff]
        if not tail:
            continue
        n = len(tail)
        correct = sum(tail)
        precision = correct / n
        lower = wilson_lower_bound(correct, n)
        if best_seen is None or precision > best_seen[1]:
            best_seen = (cutoff, precision, lower, n)
        if lower >= target and n >= min_support:
            return ThresholdRecommendation(
                threshold=cutoff,
                measured_precision=precision,
                lower_bound=lower,
                support=n,
                reason=(
                    f"Every held-out prediction at confidence >= {cutoff:.2f} was "
                    f"measured at {precision:.1%} precision over {n} samples, with a "
                    f"95% lower bound of {lower:.1%} -- clearing the {target:.0%} "
                    f"target even at the pessimistic end of the interval."
                ),
            )

    # Explain which of the two gates failed -- "not accurate enough" and "not
    # enough data yet" call for different follow-up.
    if best_seen is None:
        return ThresholdRecommendation(None, None, 0, "No predictions to calibrate against.")
    cutoff, precision, lower, support = best_seen
    if precision >= target:
        reason = (
            f"The best band (confidence >= {cutoff:.2f}) measured {precision:.1%} over "
            f"{support} held-out predictions, which clears the {target:.0%} target as a "
            f"point estimate -- but its 95% lower bound is only {lower:.1%}. A sample "
            f"this small cannot establish {target:.0%} precision, and the gap is "
            f"sampling noise, not measured accuracy. This is a data-volume limit, not "
            f"a cascade failure: the fix is more confirmed real transactions (Phase 6), "
            f"not a tuning change."
        )
    else:
        reason = (
            f"No confidence band clears {target:.0%}: the best measured precision "
            f"over any tail was {precision:.1%} (confidence >= {cutoff:.2f}, "
            f"{support} samples, 95% lower bound {lower:.1%}). §5.5 names this an "
            f"acceptable outcome -- an untrustworthy auto-apply silently corrupts "
            f"the ledger, which is worse than reviewing everything."
        )
    return ThresholdRecommendation(
        None, precision, support, reason, lower_bound=lower
    )


def _first_hit(
    tiers: Sequence[Categorizer],
    txn: CategorizationInput,
    ctx: LedgerContext,
    allowed: frozenset[Tier] | None = None,
) -> Prediction | None:
    for tier in tiers:
        if allowed is not None and tier.tier not in allowed:
            continue
        prediction = tier.predict(txn, ctx)
        if prediction is not None:
            return prediction
    return None


def evaluate_corpus(
    corpus: Corpus,
    *,
    tier_factory: TierFactory,
    normalize: Normalizer,
    holdout_fraction: float = DEFAULT_HOLDOUT_FRACTION,
    seed: int = DEFAULT_SEED,
    used_llm: bool = False,
    notes: Sequence[str] = (),
) -> EvalReport:
    """Measure `tier_factory`'s tiers against `corpus`. The whole of §5.5.

    Split first, build training signal from the train side only, then never
    look at the train side again. `tier_factory` receives exactly one
    `LedgerContext` and that context is the only history any tier is handed.
    """
    train, test = split_corpus(corpus, holdout_fraction=holdout_fraction, seed=seed)
    if not test:
        raise ValueError("Held-out split is empty; nothing to measure.")

    ctx = LedgerContext(accounts=corpus.accounts, examples=build_examples(train, normalize))
    tiers = tuple(tier_factory(ctx))

    # Majority class is taken from the *training* labels: at decision time
    # nobody knows the held-out distribution, and a baseline fitted on the
    # test set would be an unfairly strong floor.
    counts: dict[str, int] = {}
    for entry in train:
        counts[entry.account] = counts.get(entry.account, 0) + 1
    baseline_account = max(sorted(counts), key=lambda a: counts[a]) if counts else ""
    baseline_correct = sum(1 for e in test if e.account == baseline_account)

    per_tier: dict[Tier, list[int]] = {t.tier: [0, 0] for t in tiers}
    cascade_answered = cascade_correct = 0
    non_llm_answered = non_llm_correct = 0
    scored: list[tuple[Prediction, bool]] = []

    for entry in test:
        txn = entry.to_input()
        for tier in tiers:
            prediction = tier.predict(txn, ctx)
            if prediction is None:
                continue
            stats = per_tier[tier.tier]
            stats[0] += 1
            stats[1] += int(prediction.account == entry.account)

        hit = _first_hit(tiers, txn, ctx)
        if hit is not None:
            cascade_answered += 1
            correct = hit.account == entry.account
            cascade_correct += int(correct)
            scored.append((hit, correct))

        non_llm = _first_hit(tiers, txn, ctx, allowed=NON_LLM_TIERS)
        if non_llm is not None:
            non_llm_answered += 1
            non_llm_correct += int(non_llm.account == entry.account)

    n_test = len(test)
    ordered = [t for t in CASCADE_ORDER if t in per_tier]
    tier_metrics = tuple(
        TierMetrics(
            name=t.value, total=n_test, answered=per_tier[t][0], correct=per_tier[t][1]
        )
        for t in ordered
    )

    # A tier that answered nothing is a fact about the *tier*, not a measured
    # rate, and a reader scanning the table will otherwise read "0.0%" as
    # "measured and bad" rather than "never fired".
    silent = [m.name for m in tier_metrics if m.answered == 0]
    extra_notes = list(notes)
    if silent:
        extra_notes.append(
            f"Tier(s) {', '.join(silent)} answered zero held-out transactions. "
            f"A 0.0% row is 'never fired', not 'measured and inaccurate' -- "
            f"check the tier is actually wired up before reading anything into it."
        )

    return EvalReport(
        corpus_path=str(corpus.path) if corpus.path else "<in-memory>",
        n_total=len(corpus.entries),
        n_train=len(train),
        n_test=n_test,
        seed=seed,
        used_llm=used_llm,
        baseline_account=baseline_account,
        baseline_correct=baseline_correct,
        tiers=tier_metrics,
        cascade=TierMetrics("cascade", n_test, cascade_answered, cascade_correct),
        cascade_non_llm=TierMetrics("cascade-no-llm", n_test, non_llm_answered, non_llm_correct),
        buckets=_bucket_predictions(scored),
        recommendation=recommend_threshold(scored),
        accounts=corpus.accounts,
        notes=tuple(extra_notes),
    )


def run_eval(corpus: str | None = None, use_llm: bool = False) -> EvalReport:
    """Entry point for `bookkeeper eval` (wired in `cli.py`).

    `use_llm` defaults to False so the command, and the CI regression gate
    built on it, stay hermetic and offline -- the LLM tier needs a running
    Ollama and takes seconds per transaction.
    """
    loaded = load_corpus(corpus)
    notes = [
        (
            "The memory tier is measured against a table seeded from the training "
            "split only, not from data/memory.json. Measuring it against real "
            "on-disk memory would let already-confirmed held-out transactions "
            "answer their own questions."
        ),
    ]
    if not use_llm:
        notes.append(
            "LLM tier excluded (--llm to include). The novel one-off merchants in "
            "the corpus are unreachable for tiers 1-3 by construction, so the "
            "numbers above are a floor, not a ceiling."
        )
    with tempfile.TemporaryDirectory(prefix="bookkeeper-eval-") as tmp:
        factory = _DefaultTierFactory(use_llm=use_llm, memory_dir=Path(tmp))
        report = evaluate_corpus(
            loaded,
            tier_factory=factory,
            normalize=_default_normalizer(),
            used_llm=use_llm,
            notes=notes,
        )

    # A tier the cascade could not build is absent from the table entirely,
    # which reads as "not measured" when it actually means "not running in
    # production either". Say so rather than letting the gap speak.
    unbuilt = [
        f"{tier.value} ({reason})"
        for tier, reason in factory.unavailable
        if not (tier is Tier.LLM and not use_llm)
    ]
    if unbuilt:
        return replace(
            report,
            notes=report.notes
            + (
                (
                    f"Tier(s) the cascade could NOT assemble, and which are therefore "
                    f"missing from the table rather than scoring badly in it: "
                    f"{', '.join(unbuilt)}. These are not running in production either."
                ),
            ),
        )
    return report
