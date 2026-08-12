"""SimpleFIN -> beancount normalization.

Pure transforms: SimpleFIN's `Account`/`Transaction` models in, plain
dataclasses out. No filesystem or network access here -- that keeps this
module trivially testable and keeps the "one API call per sync" rule (§3.1)
from ever being tempted by a retry-in-a-loop written in here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from bookkeeper.simplefin.models import Account, AccountSet


@dataclass(frozen=True)
class NormalizedTransaction:
    simplefin_id: str
    posted_date: date
    amount: Decimal
    currency: str
    description: str
    asset_account: str
    # Not part of the documented protocol, but observed on the live demo
    # server (observed live, 2026-07-30). Optional everywhere downstream --
    # never required by ingest, since a real bank feed may send none of
    # them. Persisted as metadata when present (free signal for Phase 3).
    mcc: str | None = None
    payee: str | None = None
    memo: str | None = None
    extra: dict[str, Any] | None = None


@dataclass(frozen=True)
class NormalizedBalance:
    account: str
    amount: Decimal
    currency: str
    # balance-date + 1 day: a beancount `balance` directive asserts the
    # balance as of the *start* of its date, i.e. after everything posted
    # strictly before it. To assert "the balance was X at the end of
    # balance-date", the directive must be dated the following day.
    assertion_date: date


@dataclass(frozen=True)
class OpeningBalance:
    """A one-time plug against `Equity:Opening-Balances` for an account's
    first-ever sync.

    Why this exists: PLAN.md decision #2 is "greenfield ledger, bootstrapped
    by backfilling history from SimpleFIN" -- but SimpleFIN's date window is
    capped (live-observed 2026-07-30: ~45 days recommended, higher hard
    cap), so a single sync can never fetch an account's *entire* history.
    Without a plug, `balance Assets:X <current balance>` can never pass
    bean-check for any account with activity older than the window, since
    the ledger would only contain a partial slice of postings. Live-tested:
    this reproduces on the real demo server's Checking/Savings accounts,
    which have months of history behind any single fetch.

    `Equity:Opening-Balances` is already opened in
    ledger/accounts.beancount anticipating exactly this.
    """

    asset_account: str
    amount: Decimal
    currency: str
    entry_date: date


_DEMO_BRAND_PREFIX_RE = re.compile(r"^\s*SimpleFIN\s+", re.IGNORECASE)


def _sanitize_leaf(name: str) -> str:
    # Live demo account names come back branded, e.g. "SimpleFIN Savings",
    # "SimpleFIN Checking", "SimpleFIN Empty Account" (confirmed live,
    # 2026-07-30) -- strip that prefix so the leaf is "Savings"/"Checking"/
    # "EmptyAccount", matching accounts.beancount's convention.
    # A no-op for real (Phase 6) bank accounts, which won't be branded this
    # way.
    name = _DEMO_BRAND_PREFIX_RE.sub("", name)
    parts = re.split(r"[^A-Za-z0-9]+", name)
    leaf = "".join(part.capitalize() for part in parts if part)
    return leaf or "Account"


class AssetAccountCollisionError(RuntimeError):
    """Two different SimpleFIN accounts sanitize to the same asset account."""


# Fixed, deterministic `open` date for every generated Assets:SimpleFIN:*
# account -- not derived from any account's actual data (balance-date,
# earliest transaction, "today"), so accounts-simplefin.beancount renders
# byte-identically across reruns regardless of which window a sync
# happened to request. Safely earlier than any realistic SimpleFIN
# transaction or backfill date; revisit only if that stops being true.
ASSET_ACCOUNT_OPEN_DATE = date(2000, 1, 1)


def simplefin_asset_account(account: Account) -> str:
    """Deterministic `Assets:SimpleFIN:<Name>` account name.

    Namespaced under `Assets:SimpleFIN:` (decided 2026-07-30,
    superseding an earlier flat `Assets:<Name>` scheme this function used
    briefly to match the hand-curated accounts.beancount): flat
    names require a human to pre-open one ledger account per bank account
    and collapse the moment two different banks both have a "Checking".
    `bookkeeper.ingest` now owns opening these accounts itself, into
    accounts-simplefin.beancount (see `render_accounts_simplefin_file`) --
    accounts.beancount stays hand-curated for expense/envelope accounts.

    No id-derived disambiguation suffix on the common case; two SimpleFIN
    accounts that genuinely sanitize to the same name are a hard error
    (`check_no_asset_account_collisions`), not a silent merge.
    """
    return f"Assets:SimpleFIN:{_sanitize_leaf(account.name)}"


def check_no_asset_account_collisions(accounts: list[Account]) -> None:
    """Raise `AssetAccountCollisionError` if two different accounts share a name.

    Called once per sync, on the full account list, before anything is
    normalized or written -- a collision aborts the whole sync rather than
    partially writing one account's data under a name that secretly
    conflates two real bank accounts.
    """
    claimed_by: dict[str, str] = {}  # asset_account -> account.id that claimed it
    for account in accounts:
        name = simplefin_asset_account(account)
        prior_id = claimed_by.get(name)
        if prior_id is not None and prior_id != account.id:
            raise AssetAccountCollisionError(
                f"SimpleFIN accounts {prior_id!r} and {account.id!r} both "
                f"sanitize to {name!r} -- rename one of them (in your bank's "
                f"account nickname or SimpleFIN connection settings) so they "
                f"can be told apart in the ledger."
            )
        claimed_by[name] = account.id


@dataclass(frozen=True)
class AssetAccountOpen:
    asset_account: str
    currency: str


def discover_asset_accounts(account_set: AccountSet) -> list[AssetAccountOpen]:
    """Every SimpleFIN account's `Assets:SimpleFIN:<Name>` + currency, sorted.

    Sorted by name (not SimpleFIN's response order, which isn't guaranteed
    stable) so `accounts-simplefin.beancount` renders in the same order on
    every sync -- required for it to be byte-identical across reruns.
    """
    seen: dict[str, str] = {}
    for account in account_set.accounts:
        seen[simplefin_asset_account(account)] = account.currency
    return [
        AssetAccountOpen(asset_account=name, currency=currency)
        for name, currency in sorted(seen.items())
    ]


def normalize_account_set(
    account_set: AccountSet,
) -> tuple[list[NormalizedTransaction], int]:
    """Returns `(non-pending transactions, count of pending skipped)`.

    Pending transactions are excluded here rather than filtered later,
    because their `id` is documented as unstable once the transaction
    posts (PLAN.md risk table): recording one under its pending id would
    create a permanent duplicate when the posted version later arrives.
    """
    out: list[NormalizedTransaction] = []
    skipped_pending = 0
    for account in account_set.accounts:
        asset_account = simplefin_asset_account(account)
        for txn in account.transactions:
            if txn.pending:
                skipped_pending += 1
                continue
            posted = datetime.fromtimestamp(txn.posted, tz=UTC).date()
            out.append(
                NormalizedTransaction(
                    simplefin_id=txn.id,
                    posted_date=posted,
                    amount=txn.amount,
                    currency=account.currency,
                    description=txn.description,
                    asset_account=asset_account,
                    mcc=txn.mcc,
                    payee=txn.payee,
                    memo=txn.memo,
                    extra=txn.extra,
                )
            )
    return out, skipped_pending


def normalize_balances(account_set: AccountSet) -> list[NormalizedBalance]:
    """One balance assertion per account, from SimpleFIN's `balance` field.

    This is the cash-truth guard (PLAN.md §5.2 item 3): however the
    envelope layer drifts, `bean-check` will fail here if a transaction was
    ever dropped or duplicated.
    """
    out: list[NormalizedBalance] = []
    for account in account_set.accounts:
        asset_account = simplefin_asset_account(account)
        balance_date = datetime.fromtimestamp(account.balance_date, tz=UTC).date()
        out.append(
            NormalizedBalance(
                account=asset_account,
                amount=account.balance,
                currency=account.currency,
                assertion_date=balance_date + timedelta(days=1),
            )
        )
    return out
