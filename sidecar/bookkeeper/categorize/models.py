"""Shared contract for the tiered categorizer (PLAN.md §5.4).

CONTRACT — this file is owned by the team lead. Workers must NOT restructure it.
Add your tier as a new module implementing `Categorizer`; do not change these
dataclasses, the `Tier` members, or `CASCADE_ORDER` without saying so, because
every other tier module and the eval harness are written against them.

The cascade is first-hit-wins over independent, individually-testable tiers:

    memory -> rule -> mcc -> statistical -> llm

Each tier answers or abstains. Abstaining is `None`, never a guess with low
confidence — "I don't know" and "I think it's groceries at 0.1" are different
claims and the eval harness in §5.5 measures *coverage* (share answered) and
precision separately per tier. A tier that quietly guesses destroys both
numbers.

Nothing here touches the filesystem, the network, or Ollama, so every tier can
be unit-tested against a hand-built `LedgerContext` with no ledger on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable


class Tier(str, Enum):
    """Cascade tiers, in the order PLAN.md §5.4 defines them.

    `MCC` is not in the plan's original four. It comes from the §3.1
    amendment: the live bridge emits an undocumented `mcc` field, and where
    it is present it is a high-precision deterministic signal that should
    short-circuit ahead of the LLM. It is optional everywhere -- a feed that
    sends no MCC degrades to exactly the four-tier cascade §5.4 specifies.
    """

    MEMORY = "memory"
    RULE = "rule"
    MCC = "mcc"
    STATISTICAL = "statistical"
    LLM = "llm"


#: Default first-hit-wins evaluation order. Deterministic tiers first, model
#: last. MCC sits ahead of the statistical tier because it is a fixed lookup
#: rather than a fitted estimate, but that placement is a *hypothesis*: the
#: eval harness reports per-tier precision, and if MCC measures worse than
#: the statistical tier on real data this order is what changes.
CASCADE_ORDER: tuple[Tier, ...] = (
    Tier.MEMORY,
    Tier.RULE,
    Tier.MCC,
    Tier.STATISTICAL,
    Tier.LLM,
)


@dataclass(frozen=True)
class CategorizationInput:
    """One transaction as the categorizer sees it.

    Deliberately a separate type from `ingest.normalize.NormalizedTransaction`
    rather than reusing it: ingest's model is about *recording* a transaction
    (it carries `extra`, currency, ledger-render concerns), while this is
    about *classifying* one. Keeping them apart means a change to the ingest
    wire format doesn't silently alter classifier behaviour, and lets tests
    build a two-line input without constructing a full ingest record.

    `mcc` / `payee` / `memo` are optional exactly as in ingest -- undocumented
    fields that a real bank feed may never send (§3.1). No tier may *require*
    them; a tier that depends on one abstains when it is absent.
    """

    description: str
    amount: Decimal
    posted_date: date
    asset_account: str
    simplefin_id: str
    currency: str = "USD"
    mcc: str | None = None
    payee: str | None = None
    memo: str | None = None


@dataclass(frozen=True)
class LabeledExample:
    """A previously-confirmed categorization: the training/retrieval corpus.

    "Confirmed" means a human accepted it, or it was read back out of the
    ledger as a non-`Expenses:Unknown` posting. Tiers 3 and 4 learn from
    these; tier 1 is a direct index over them.
    """

    normalized_description: str
    account: str
    description: str = ""
    amount: Decimal = Decimal(0)
    posted_date: date | None = None
    mcc: str | None = None
    payee: str | None = None


@dataclass(frozen=True)
class LedgerContext:
    """Everything a tier is allowed to know about the ledger.

    `accounts` is the **closed label set** -- the accounts actually open in
    the ledger, excluding the `Unknown` catch-alls. This is what the LLM
    tier passes to Ollama as a JSON Schema `enum` (§5.4), which makes a
    hallucinated account literally unrepresentable rather than merely
    unlikely. Every tier must return an account from this tuple or abstain;
    `predict_is_valid` enforces it in tests.

    Passing this in (rather than having tiers read the ledger themselves)
    is what keeps the tiers pure and the eval harness able to hold the
    label set fixed while swapping the corpus.
    """

    accounts: tuple[str, ...]
    examples: tuple[LabeledExample, ...] = ()


@dataclass(frozen=True)
class Prediction:
    """A tier's answer. Abstention is `None`, not a low-confidence Prediction.

    `confidence` is a float in [0, 1] and is **not** trusted as a probability
    (§5.5): LLM self-reported confidence especially is a weak signal. It is
    an ordering signal that the eval harness buckets and calibrates against
    measured precision. The auto-apply threshold comes out of that
    measurement, never out of a tier's own self-assessment.

    `rationale` is a short human-readable reason ("matched rule 'PG&E'",
    "3 prior confirmations") shown on review cards in Phase 4. It is for
    humans, not for parsing.
    """

    account: str
    confidence: float
    tier: Tier
    rationale: str = ""


@runtime_checkable
class Categorizer(Protocol):
    """One tier. Implement this, register it in the cascade, done.

    This is the "swappable interface" PLAN.md §6 Phase 3 puts tier 4 behind:
    the LLM tier is just another `Categorizer`, so the cascade can run with
    it absent (no Ollama installed, or deliberately disabled) and every
    other tier is unaffected.
    """

    tier: Tier

    def predict(
        self, txn: CategorizationInput, ctx: LedgerContext
    ) -> Prediction | None:
        """Return a prediction, or `None` to abstain and fall through."""
        ...


def predict_is_valid(prediction: Prediction | None, ctx: LedgerContext) -> bool:
    """True if `prediction` is an abstention or names an account in the closed set.

    The invariant every tier must hold. Enforced in tests rather than at
    runtime in the cascade, because a tier returning an out-of-set account
    is a bug in that tier, not a condition to be handled in production.
    """
    if prediction is None:
        return True
    return (
        prediction.account in ctx.accounts
        and 0.0 <= prediction.confidence <= 1.0
    )


#: Accounts that are never a valid *prediction*. Failing to classify is an
#: abstention (`None`), which lands the transaction in the review queue --
#: it is never "successfully predicted as Unknown", which would look like
#: coverage in the eval numbers while meaning the opposite.
UNKNOWN_ACCOUNTS: frozenset[str] = frozenset(
    {"Expenses:Unknown", "Income:Unknown"}
)
