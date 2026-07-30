"""Shared ledger reading for the categorizer (PLAN.md §5.4).

Every tier needs two things out of the ledger: the closed label set it is
allowed to predict, and the transactions actually waiting to be
categorized. Both are read here, once, so the tiers themselves (and the
eval harness) never touch beancount's loader directly -- they take a
`LedgerContext` / `CategorizationInput` list and stay pure and unit-testable
against a hand-built ledger, per `categorize.models`.
"""

from __future__ import annotations

from decimal import Decimal

from beancount.core.data import Directive, Open, Transaction

from bookkeeper.categorize.models import (
    UNKNOWN_ACCOUNTS,
    CategorizationInput,
    LabeledExample,
    LedgerContext,
)
from bookkeeper.categorize.normalize import normalize_description
from bookkeeper.envelope.compute import load_ledger

# The two namespaces a categorizer is ever allowed to predict into. Assets
# and Equity postings (the other side of every transaction) are never
# candidate labels.
_CATEGORIZABLE_PREFIXES = ("Expenses:", "Income:")


def _is_categorizable(account: str) -> bool:
    return account.startswith(_CATEGORIZABLE_PREFIXES) and account not in UNKNOWN_ACCOUNTS


def build_ledger_context(entries: list[Directive] | None = None) -> LedgerContext:
    """The closed label set plus every already-confirmed categorization.

    `accounts` excludes `models.UNKNOWN_ACCOUNTS` by construction -- those
    are catch-alls, never valid predictions (see `models.py`). `examples`
    comes from postings already recorded against a real (non-Unknown)
    expense/income account: whatever a human confirmed, or ingest managed
    to categorize outright, is fair training/retrieval data for tiers 3-4.
    """
    if entries is None:
        entries, _errors, _options_map = load_ledger()

    accounts = sorted(
        entry.account
        for entry in entries
        if isinstance(entry, Open) and _is_categorizable(entry.account)
    )

    examples: list[LabeledExample] = []
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        meta = entry.meta or {}
        for posting in entry.postings:
            if not _is_categorizable(posting.account):
                continue
            units = posting.units
            # `LabeledExample.amount` is the posting's own booked amount --
            # standard beancount sign convention (an Expenses posting is
            # positive, an Income posting is negative) -- unlike
            # `CategorizationInput.amount`, which is deliberately the
            # signed *asset-side* amount to match ingest's convention.
            # They're different fields on different types answering
            # different questions ("what did this posting book as" vs.
            # "what did the bank account move by"), so this is not an
            # inconsistency to reconcile.
            examples.append(
                LabeledExample(
                    normalized_description=normalize_description(entry.narration),
                    account=posting.account,
                    description=entry.narration,
                    amount=units.number if units is not None else Decimal(0),
                    posted_date=entry.date,
                    mcc=meta.get("simplefin-mcc"),
                    payee=meta.get("simplefin-payee"),
                )
            )

    return LedgerContext(accounts=tuple(accounts), examples=tuple(examples))


def uncategorized_transactions(entries: list[Directive] | None = None) -> list[CategorizationInput]:
    """Every transaction still posted to `Expenses:Unknown`/`Income:Unknown`.

    The "other side" of such a transaction -- the bank/asset posting -- is
    exactly one posting by construction (`ingest.render.render_transaction`
    always emits asset-account + one Unknown catch-all); a transaction that
    doesn't fit that shape is skipped rather than guessed at, since there is
    no single account to report as `asset_account`.

    `amount` is that asset posting's signed amount, matching
    `ingest.normalize.NormalizedTransaction.amount` (negative for spending)
    -- not a magnitude, and not the Unknown posting's (implicit, opposite
    sign) amount.
    """
    if entries is None:
        entries, _errors, _options_map = load_ledger()

    out: list[CategorizationInput] = []
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        unknown_postings = [p for p in entry.postings if p.account in UNKNOWN_ACCOUNTS]
        if not unknown_postings:
            continue
        other_postings = [p for p in entry.postings if p.account not in UNKNOWN_ACCOUNTS]
        if len(other_postings) != 1:
            continue
        asset_posting = other_postings[0]
        units = asset_posting.units
        if units is None:
            continue

        meta = entry.meta or {}
        out.append(
            CategorizationInput(
                description=entry.narration,
                amount=units.number,
                posted_date=entry.date,
                asset_account=asset_posting.account,
                simplefin_id=meta.get("simplefin-id", ""),
                currency=units.currency,
                mcc=meta.get("simplefin-mcc"),
                payee=meta.get("simplefin-payee"),
                memo=meta.get("simplefin-memo"),
            )
        )
    return out
