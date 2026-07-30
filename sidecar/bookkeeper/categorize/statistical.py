"""Tier 3: a self-contained multinomial naive Bayes over confirmed history.

PLAN.md §5.4 calls this tier a "smart_importer-style classifier". It is
implemented here from scratch rather than by depending on smart_importer or
scikit-learn: §3.2 leaves ~8-9GB for the model and counts the sidecar's own
footprint against that budget, and pulling a ~100MB numeric stack in to
classify a few hundred short strings spends that budget on the wrong thing.
Multinomial naive Bayes over bag-of-features is about forty lines of
arithmetic and needs nothing but the standard library.

Its job per §5.4 is to be **the floor the LLM must beat to justify its cost**,
which puts two demands on it that a plain accuracy score does not capture:

- Its confidence has to mean something, because §5.5 buckets predictions by
  confidence and calibrates the auto-apply threshold against measured
  precision per bucket. A tier that reports 0.99 for everything makes those
  buckets empty of information.
- It has to *abstain* rather than answer off a corpus too small to support an
  estimate. Two confirmed examples do not make a classifier, and a confident
  wrong answer at tier 3 short-circuits the LLM that would have got it right.

Features are word tokens plus character 3- and 4-grams. Character n-grams are
what make this work on the bank-mangled strings of §3.1: `SQ *COFFEE 4TH ST
8829` and `SQ *COFFEE 4TH ST 1174` share no whole token beyond the merchant
words, but they share nearly every trigram, so a store number the normalizer
did not strip costs a little signal instead of all of it.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from bookkeeper.categorize.models import (
    UNKNOWN_ACCOUNTS,
    CategorizationInput,
    LabeledExample,
    LedgerContext,
    Prediction,
    Tier,
)

#: Below this many usable confirmed examples the corpus cannot support an
#: estimate and the tier abstains outright. Not a tuned number -- it is the
#: point below which "the classifier said so" is indistinguishable from "the
#: only thing it has ever seen said so".
_MIN_EXAMPLES = 8

#: With one class every posterior is 1.0 by construction, which is not a
#: prediction, it is the corpus talking to itself.
_MIN_CLASSES = 2

#: Add-alpha (Lidstone) smoothing. Below 1.0 because the feature space is
#: character n-grams: vocabulary is large relative to the corpus, and full
#: Laplace add-one would hand most of each class's probability mass to
#: features it has never seen.
_ALPHA = 0.2

_CHAR_NGRAM_SIZES = (3, 4)

_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def _default_normalizer() -> Callable[[str], str]:
    """Resolve the shared description normalizer at construction time.

    Deferred rather than imported at module scope so that a caller supplying
    its own normalizer -- the eval harness holding normalization fixed while
    it swaps corpora, or a unit test isolating the classifier from the
    normalizer's behaviour -- never pulls in the shared module at all.
    """
    from bookkeeper.categorize.normalize import normalize_description

    return normalize_description


def _features(text: str) -> list[str]:
    """Word tokens plus padded character 3-/4-grams, namespaced by kind.

    Namespacing keeps a token and an n-gram that happen to spell the same
    thing from being counted as one feature. The padding spaces make word
    boundaries visible to the n-grams, so a merchant name at the start of a
    description is a different feature than the same letters mid-string.
    """
    tokens = [token for token in _TOKEN_SPLIT_RE.split(text.lower()) if token]
    if not tokens:
        return []
    features = [f"w:{token}" for token in tokens]
    padded = f" {' '.join(tokens)} "
    for size in _CHAR_NGRAM_SIZES:
        features.extend(
            f"c{size}:{padded[i : i + size]}" for i in range(len(padded) - size + 1)
        )
    return features


@dataclass(frozen=True)
class _Model:
    """A fitted classifier: log-space parameters and the vocabulary it saw.

    Everything is stored as logs because a prediction sums a hundred-odd
    feature probabilities and the product of a hundred numbers below 1.0
    underflows to zero long before it finishes.
    """

    log_prior: dict[str, float]
    log_likelihood: dict[str, dict[str, float]]
    #: Per-class probability mass reserved by smoothing for a feature that
    #: class never saw. Class-specific because it depends on that class's
    #: total feature count.
    log_unseen: dict[str, float]
    vocabulary: frozenset[str]
    example_count: int


def _fit_model(
    examples: Sequence[LabeledExample], accounts: Sequence[str]
) -> _Model | None:
    """Fit over `examples`, or return `None` if the corpus cannot support one.

    Examples labelled with an account that is not open in the ledger (or with
    one of the `Unknown` catch-alls, which are abstentions rather than labels)
    are dropped rather than fitted and filtered at predict time -- they would
    otherwise distort the priors of the classes that remain.
    """
    allowed = {account for account in accounts if account not in UNKNOWN_ACCOUNTS}

    feature_counts: dict[str, Counter[str]] = defaultdict(Counter)
    document_counts: Counter[str] = Counter()
    for example in examples:
        if example.account not in allowed:
            continue
        features = _features(example.normalized_description)
        if not features:
            continue
        document_counts[example.account] += 1
        feature_counts[example.account].update(features)

    total_documents = sum(document_counts.values())
    if total_documents < _MIN_EXAMPLES or len(document_counts) < _MIN_CLASSES:
        return None

    vocabulary = frozenset(
        feature for counts in feature_counts.values() for feature in counts
    )
    vocabulary_size = len(vocabulary)

    log_prior = {
        account: math.log(count / total_documents)
        for account, count in document_counts.items()
    }
    log_likelihood: dict[str, dict[str, float]] = {}
    log_unseen: dict[str, float] = {}
    for account, counts in feature_counts.items():
        denominator = sum(counts.values()) + _ALPHA * vocabulary_size
        log_likelihood[account] = {
            feature: math.log((count + _ALPHA) / denominator)
            for feature, count in counts.items()
        }
        log_unseen[account] = math.log(_ALPHA / denominator)

    return _Model(
        log_prior=log_prior,
        log_likelihood=log_likelihood,
        log_unseen=log_unseen,
        vocabulary=vocabulary,
        example_count=total_documents,
    )


def _posterior(model: _Model, features: Sequence[str]) -> dict[str, float]:
    """Normalized class posterior for one transaction's in-vocabulary features.

    Two departures from textbook multinomial naive Bayes, both about making
    the *confidence* honest -- neither can change which class wins, since one
    drops features scored identically against every class and the other
    divides every class's score by the same positive constant.

    Features no class has ever seen are dropped rather than scored against
    each class's smoothing floor: they carry no discriminative information,
    and including them would let description *length* drag the posterior
    toward whichever class happens to have the smallest vocabulary.

    Scores are then divided by the number of features that did match, making
    each one the *mean* per-feature log-probability rather than the sum. Naive
    Bayes assumes features are independent and overlapping character n-grams
    emphatically are not, so summing forty of them double-counts the same
    evidence forty times and pins every posterior at 1.0 -- which would leave
    §5.5's calibration buckets sorting predictions that all report the same
    number. Averaging is the standard correction and costs nothing here.
    """
    scores = {
        account: prior
        + sum(
            model.log_likelihood[account].get(feature, model.log_unseen[account])
            for feature in features
        )
        for account, prior in model.log_prior.items()
    }
    scale = max(1, len(features))

    highest = max(scores.values())
    weights = {
        account: math.exp((score - highest) / scale) for account, score in scores.items()
    }
    total = sum(weights.values())
    return {account: weight / total for account, weight in weights.items()}


class StatisticalCategorizer:
    """Tier 3 of the §5.4 cascade: naive Bayes over previously confirmed labels.

    Fitting is lazy and cached against the corpus the context carries. A sync
    categorizes a few hundred transactions against one unchanging
    `LedgerContext`, so refitting per transaction would do the same work three
    hundred times and turn an instant tier into the slow one.
    """

    tier = Tier.STATISTICAL

    def __init__(self, *, normalizer: Callable[[str], str] | None = None) -> None:
        self._normalize = normalizer if normalizer is not None else _default_normalizer()
        self._cache: tuple[
            tuple[LabeledExample, ...], tuple[str, ...], _Model | None
        ] | None = None

    def _model_for(self, ctx: LedgerContext) -> _Model | None:
        """The fitted model for `ctx`, reusing the cached fit when it still applies.

        Keyed on the account set as well as the corpus: `accounts` decides
        which examples are eligible, so opening or closing an account changes
        the fit even when not one example moved. The cache stores a `None`
        fit too, so a corpus too small to fit is diagnosed once rather than
        once per transaction.
        """
        cached = self._cache
        if cached is not None:
            examples, accounts, model = cached
            if accounts == ctx.accounts and (
                examples is ctx.examples or examples == ctx.examples
            ):
                return model

        model = _fit_model(ctx.examples, ctx.accounts)
        self._cache = (ctx.examples, ctx.accounts, model)
        return model

    def predict(
        self, txn: CategorizationInput, ctx: LedgerContext
    ) -> Prediction | None:
        model = self._model_for(ctx)
        if model is None:
            return None

        features = [
            feature
            for feature in _features(self._normalize(txn.description))
            if feature in model.vocabulary
        ]
        if not features:
            # Nothing in this description has ever been seen, so the only
            # thing left to answer with is the class prior -- which is the
            # majority-class baseline wearing a confidence score. Abstain and
            # let the LLM tier, which is there for exactly this case, have it.
            return None

        posterior = _posterior(model, features)
        account, confidence = min(
            posterior.items(), key=lambda item: (-item[1], item[0])
        )
        if account not in ctx.accounts:
            return None

        return Prediction(
            account=account,
            confidence=confidence,
            tier=Tier.STATISTICAL,
            rationale=(
                f"naive Bayes over {model.example_count} confirmed examples "
                f"({len(features)} known features matched)"
            ),
        )
