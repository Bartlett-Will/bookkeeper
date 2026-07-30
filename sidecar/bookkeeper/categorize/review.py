"""The review queue, and the confirmation path that closes the learning loop.

Review-everything is the shipped default (decision 5), which makes this the
*primary* categorization surface, not a fallback for the hard cases. It has
two jobs.

**Show a human what still needs deciding.** Every transaction still posting
to `Expenses:Unknown`, with the cascade's best guess, that guess's tier and
confidence, and enough identifying detail (date, description, amount,
account) to decide without opening the ledger. `ReviewEntry` is primitives
only and `to_dict()` needs no custom encoder, because Phase 4 renders these
as generative-UI `ReviewCard`s and a beancount object leaking into the
sidecar's JSON would be discovered at the browser, not here.

**Make a confirmation stick, in both places it has to.** Accepting a
categorization writes the account to the ledger *and* feeds
`data/memory.json`, which is what makes Phase 4's exit criterion --
"corrections demonstrably change the next run's predictions" -- true rather
than aspirational. Tier 1 is an exact index over confirmed decisions, so a
confirmation that only touched the ledger would fix one transaction and
teach nothing; the same merchant would come back to the queue next month.

Batch confirmation is the primitive, not a loop over the single case: §5.3
rule 2 has the user approving 40 transactions with 40 direct API calls, and
that must produce one ledger pass and *one* commit, not forty of each.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from bookkeeper.categorize.apply import (
    DECISION_CONFIRMED,
    CategorizeResult,
    Decision,
    LedgerEdit,
    apply_edits,
    run_categorize,
)
from bookkeeper.categorize.gitcommit import CommitResult, commit_ledger
from bookkeeper.categorize.models import UNKNOWN_ACCOUNTS, LedgerContext

#: The tier recorded on a human decision. Deliberately not a `Tier` member:
#: `Tier` enumerates the cascade's automated tiers and is lead-owned, and a
#: person is not one of them. The distinction matters when reading the
#: ledger back -- "a human said so" is the only fully trustworthy label in
#: the file, and the eval harness must never count it as tier coverage.
HUMAN_TIER = "human"

#: A confirmation is by definition certain. This is not a model's
#: self-assessment (§5.5 says those are not trusted as probabilities); it
#: records that the decision did not come from a model at all.
HUMAN_CONFIDENCE = 1.0

#: The account a transaction sits in until someone decides otherwise.
UNCATEGORIZED_ACCOUNT = "Expenses:Unknown"


@dataclass(frozen=True)
class ReviewEntry:
    """One transaction awaiting a decision, as Phase 4 will render it.

    Primitives only -- ISO date strings, decimal strings -- so
    `to_dict()` is directly JSON-serializable.
    """

    simplefin_id: str
    asset_account: str
    posted_date: str
    description: str
    amount: str
    currency: str
    current_account: str = UNCATEGORIZED_ACCOUNT
    suggested_account: str | None = None
    confidence: float | None = None
    tier: str | None = None
    rationale: str = ""
    mcc: str | None = None
    payee: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.asset_account, self.simplefin_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "simplefin_id": self.simplefin_id,
            "asset_account": self.asset_account,
            "posted_date": self.posted_date,
            "description": self.description,
            "amount": self.amount,
            "currency": self.currency,
            "current_account": self.current_account,
            "suggested_account": self.suggested_account,
            "confidence": self.confidence,
            "tier": self.tier,
            "rationale": self.rationale,
            "mcc": self.mcc,
            "payee": self.payee,
        }


def entry_from_decision(decision: Decision) -> ReviewEntry:
    return ReviewEntry(
        simplefin_id=decision.simplefin_id,
        asset_account=decision.asset_account,
        posted_date=decision.posted_date,
        description=decision.description,
        amount=decision.amount,
        currency=decision.currency,
        suggested_account=decision.suggested_account,
        confidence=decision.confidence,
        tier=decision.tier,
        rationale=decision.rationale,
        mcc=decision.mcc,
        payee=decision.payee,
    )


@dataclass
class ReviewQueue:
    ok: bool
    entries: list[ReviewEntry] = field(default_factory=list)
    total: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def with_suggestion(self) -> int:
        return sum(1 for e in self.entries if e.suggested_account is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "total": self.total,
            "shown": len(self.entries),
            "entries": [e.to_dict() for e in self.entries],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["review failed:", *(f"  - {e}" for e in self.errors)])
        if not self.entries:
            return "review queue is empty — nothing is posting to Expenses:Unknown"

        lines = [
            f"{self.total} transaction(s) awaiting review; showing {len(self.entries)}.",
            f"{self.with_suggestion} of the shown entries have a suggestion.",
            "",
        ]
        for entry in self.entries:
            lines.append(f"{entry.posted_date}  {entry.amount:>12} {entry.currency}  {entry.description}")
            lines.append(f"    account: {entry.asset_account}   id: {entry.simplefin_id}")
            if entry.suggested_account is None:
                lines.append("    suggestion: none — no tier answered")
            else:
                confidence = "n/a" if entry.confidence is None else f"{entry.confidence:.2f}"
                lines.append(
                    f"    suggestion: {entry.suggested_account}  "
                    f"(tier {entry.tier}, confidence {confidence})"
                )
                if entry.rationale:
                    lines.append(f"    because: {entry.rationale}")
            lines.append("")
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines).rstrip("\n")


def review_queue(
    limit: int | None = None,
    *,
    use_llm: bool = False,
    categorize: Callable[..., CategorizeResult] | None = None,
) -> ReviewQueue:
    """Every transaction still on `Expenses:Unknown`, with its best guess.

    Read-only: it runs the cascade as a dry run and writes nothing, so
    listing the queue can never alter the ledger.

    `use_llm` defaults to **False** here even though `categorize` defaults
    it to True. This is an interactive listing over the whole backlog, and
    §5.3 rule 3 keeps batch model work out of interactive paths -- 338
    transactions at ~1-2s each is a minute of stall to render a list. Tiers
    1-3 answer the head of the distribution instantly; run `categorize` to
    put the model on the tail.
    """
    runner = categorize or run_categorize
    result = runner(apply=False, limit=limit, use_llm=use_llm, commit=False)
    if not result.ok:
        return ReviewQueue(ok=False, errors=list(result.errors))

    entries = [entry_from_decision(d) for d in result.decisions if d.needs_review]
    return ReviewQueue(
        ok=True, entries=entries, total=len(entries), warnings=list(result.warnings)
    )


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """A human's decision about one transaction."""

    asset_account: str
    simplefin_id: str
    account: str

    @property
    def key(self) -> tuple[str, str]:
        return (self.asset_account, self.simplefin_id)


@dataclass
class ConfirmResult:
    ok: bool
    confirmed: int = 0
    learned: int = 0
    files_written: tuple[str, ...] = ()
    commit: CommitResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "confirmed": self.confirmed,
            "learned": self.learned,
            "files_written": list(self.files_written),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["confirm failed:", *(f"  - {e}" for e in self.errors)])
        lines = [
            f"confirmed {self.confirmed} transaction(s)",
            f"recorded {self.learned} description(s) to memory for tier 1",
        ]
        if self.files_written:
            lines.extend(f"  wrote ledger/transactions/{name}" for name in self.files_written)
        if self.commit is not None:
            lines.append(self.commit.render())
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _default_memory_hooks() -> tuple[Callable[[str], str], Callable[[str, str], None]]:
    """The real tier-1 normalizer and recorder, resolved lazily.

    One `MemoryCategorizer` is constructed for the whole batch and its bound
    `record_confirmation` handed back, rather than a fresh instance per
    confirmation: the categorizer reads `memory.json` into memory at
    construction and rewrites it on every record, so per-confirmation
    instances would re-read the file forty times while approving forty
    review cards, and each would be working from a snapshot taken before its
    predecessors' writes.
    """
    from bookkeeper.categorize.memory import MemoryCategorizer
    from bookkeeper.categorize.normalize import normalize_description

    return normalize_description, MemoryCategorizer().record_confirmation


def _default_lookup() -> Callable[[], list[Any]]:
    from bookkeeper.categorize.context import uncategorized_transactions

    return uncategorized_transactions


def confirm_categorizations(
    confirmations: Sequence[Confirmation],
    *,
    commit: bool = True,
    context: LedgerContext | None = None,
    transactions: Sequence[Any] | None = None,
    transactions_dir: Path | None = None,
    normalizer: Callable[[str], str] | None = None,
    recorder: Callable[[str, str], None] | None = None,
) -> ConfirmResult:
    """Accept `confirmations`: write them to the ledger and teach tier 1.

    One ledger pass and one commit for the whole batch, so approving forty
    review cards produces one revertible commit rather than forty.

    The ledger write happens first and memory follows. If memory recording
    fails, the ledger is still correct and the failure is a warning -- the
    inverse order could teach a decision that never reached the ledger,
    which is the worse of the two inconsistencies (the queue would stop
    offering the transaction while it still posts to Unknown).
    """
    if not confirmations:
        return ConfirmResult(ok=True)

    rejected = [c for c in confirmations if c.account in UNKNOWN_ACCOUNTS or not c.account.strip()]
    if rejected:
        return ConfirmResult(
            ok=False,
            errors=[
                f"refusing to confirm {c.simplefin_id} as {c.account!r}: that is not a "
                "categorization, it is the absence of one"
                for c in rejected
            ],
        )

    if context is not None:
        unknown = [c for c in confirmations if c.account not in context.accounts]
        if unknown:
            return ConfirmResult(
                ok=False,
                errors=[
                    f"{c.account!r} is not an account open in the ledger" for c in unknown
                ],
            )

    warnings: list[str] = []
    edits = [
        LedgerEdit(
            asset_account=c.asset_account,
            simplefin_id=c.simplefin_id,
            account=c.account,
            tier=HUMAN_TIER,
            confidence=HUMAN_CONFIDENCE,
            decision=DECISION_CONFIRMED,
        )
        for c in confirmations
    ]
    report = apply_edits(edits, transactions_dir=transactions_dir)
    if report.unmatched:
        warnings.append(
            f"{len(report.unmatched)} confirmation(s) matched no Expenses:Unknown posting and "
            "were skipped (already categorized, or an unknown transaction id)"
        )
    landed = {c.key for c in confirmations} - set(report.unmatched)

    learned = 0
    try:
        normalize, record = normalizer, recorder
        if normalize is None or record is None:
            default_normalize, default_record = _default_memory_hooks()
            normalize = normalize or default_normalize
            record = record or default_record
        descriptions = _descriptions_for(landed, transactions)
        for key in sorted(landed):
            confirmation = next(c for c in confirmations if c.key == key)
            description = descriptions.get(key)
            if description is None:
                warnings.append(
                    f"confirmed {key[1]} in the ledger but could not recover its description "
                    "to record in memory; tier 1 will not learn from it"
                )
                continue
            record(normalize(description), confirmation.account)
            learned += 1
    except Exception as exc:  # noqa: BLE001 - the ledger write already succeeded
        warnings.append(f"ledger updated but memory was not ({exc}); tier 1 will not learn")

    result = ConfirmResult(
        ok=True,
        confirmed=report.edits_applied,
        learned=learned,
        files_written=report.files_written,
        warnings=warnings,
    )
    if commit and report.edits_applied:
        noun = "transaction" if report.edits_applied == 1 else "transactions"
        result.commit = commit_ledger(
            f"Categorize {report.edits_applied} {noun} (confirmed by hand)"
        )
    return result


def _descriptions_for(
    keys: set[tuple[str, str]], transactions: Sequence[Any] | None
) -> dict[tuple[str, str], str]:
    """Raw descriptions for the confirmed keys, for memory normalization.

    Read from the uncategorized set rather than passed in by the caller:
    the description that tier 1 indexes must be the one actually on the
    transaction, not whatever a client echoed back to the API.
    """
    if not keys:
        return {}
    source = transactions if transactions is not None else _default_lookup()()
    return {
        (txn.asset_account, txn.simplefin_id): txn.description
        for txn in source
        if (txn.asset_account, txn.simplefin_id) in keys
    }


def confirm_categorization(
    simplefin_id: str, asset_account: str, account: str, **kwargs: Any
) -> ConfirmResult:
    """Single-transaction convenience over `confirm_categorizations`."""
    return confirm_categorizations(
        [Confirmation(asset_account=asset_account, simplefin_id=simplefin_id, account=account)],
        **kwargs,
    )
