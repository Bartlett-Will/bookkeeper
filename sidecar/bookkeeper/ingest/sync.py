"""Orchestrates one sync: fetch -> normalize -> dedup -> write.

`run_sync` is the single entry point both the CLI (`bookkeeper sync`) and
the FastAPI sidecar (`POST /sync`) call, per the CLI contract in
`bookkeeper/cli.py`.

Idempotency is structural, not best-effort: a file is only opened for
writing when there is at least one genuinely new line to add to it. On a
rerun where SimpleFIN returns nothing not already recorded, no file is
touched at all, so the ledger is trivially byte-identical -- this is
proven in `tests/test_ingest_sync.py` by hashing the generated files
across two runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from bookkeeper import paths
from bookkeeper.categorize.gitcommit import CommitResult, commit_ledger
from bookkeeper.ingest.dedup import existing_opening_balance_accounts, existing_simplefin_keys
from bookkeeper.ingest.normalize import (
    ASSET_ACCOUNT_OPEN_DATE,
    OpeningBalance,
    check_no_asset_account_collisions,
    discover_asset_accounts,
    normalize_account_set,
    normalize_balances,
    simplefin_asset_account,
)
from bookkeeper.ingest.render import (
    render_accounts_simplefin_file,
    render_balances_file,
    render_opening_balance,
    render_transaction,
)
from bookkeeper.simplefin.fetch import fetch_accounts

# First-class way to supply an Access URL without going through `claim` at
# all -- e.g. the live SimpleFIN demo bridge's claim endpoint is currently
# broken server-side (bridge.simplefin.org's redirect drops the path; the
# real handler 403s "already claimed" -- see claim.py), but its Access URL
# is independently published and works. Same credential-hygiene rule as
# everywhere else in this module: never logged, never echoed back.
ACCESS_URL_ENV_VAR = "BOOKKEEPER_SIMPLEFIN_ACCESS_URL"


@dataclass
class SyncResult:
    ok: bool
    accounts_synced: int = 0
    transactions_seen: int = 0
    transactions_added: int = 0
    pending_skipped: int = 0
    # Total balance assertions in balances.beancount *after* this sync, not
    # an incremental "newly added" count -- the file is rewritten wholesale
    # every sync (see `render_balances_file`), so "written" always means
    # "currently present," matching what's actually on disk.
    balances_written: int = 0
    opening_balances_written: int = 0
    errors: list[str] = field(default_factory=list)
    commit: CommitResult | None = None

    def render(self) -> str:
        if not self.ok:
            lines = ["sync failed:"]
            lines.extend(f"  - {e}" for e in self.errors)
            return "\n".join(lines)
        already_present = self.transactions_seen - self.transactions_added - self.pending_skipped
        lines = [
            f"accounts synced: {self.accounts_synced}",
            f"transactions seen: {self.transactions_seen}",
            f"transactions added: {self.transactions_added}",
            f"transactions already present (skipped): {already_present}",
            f"pending transactions skipped: {self.pending_skipped}",
            f"balance assertions now in balances.beancount: {self.balances_written}",
            f"opening balance plugs written: {self.opening_balances_written}",
        ]
        if self.commit is not None:
            lines.append(self.commit.render())
        if self.errors:
            lines.append("warnings:")
            lines.extend(f"  - {e}" for e in self.errors)
        return "\n".join(lines)


def _ensure_access_url(demo: bool) -> str:
    """Resolve the Access URL to sync with, without ever calling `claim`.

    `--demo` used to auto-claim the well-known demo token here when no
    Access URL was on file. That route is currently broken server-side
    (see `bookkeeper.simplefin.claim`'s module docstring): claiming
    reliably fails, which made `sync --demo` fail outright on a fresh
    checkout instead of degrading to a clear, actionable error. `sync`
    never claims on your behalf now, demo or not -- an Access URL, however
    obtained (explicit `claim`, or one supplied directly via
    `ACCESS_URL_ENV_VAR` or `paths.access_url_file()`), is a precondition
    `run_sync` checks for, not something it will silently go fetch.
    """
    env_value = os.environ.get(ACCESS_URL_ENV_VAR)
    if env_value:
        return env_value.strip()

    access_file = paths.access_url_file()
    if not access_file.exists():
        if demo:
            raise RuntimeError(
                "no Access URL on file or in "
                f"${ACCESS_URL_ENV_VAR}. `sync --demo` no longer auto-claims "
                "-- the demo claim endpoint is currently broken server-side. "
                "Supply a working demo Access URL yourself: either run "
                f"`bookkeeper claim <setup-token>`, set ${ACCESS_URL_ENV_VAR}, "
                f"or write it directly to {access_file} (mode 0600)."
            )
        raise RuntimeError(
            "no Access URL on file; run `bookkeeper claim <setup-token>` first"
        )
    return access_file.read_text(encoding="utf-8").strip()


def _since_to_epoch(since: str | None) -> int | None:
    if since is None:
        return None
    d = date.fromisoformat(since)
    return int(datetime(d.year, d.month, d.day, tzinfo=UTC).timestamp())


def run_sync(since: str | None = None, demo: bool = False) -> SyncResult:
    try:
        access_url = _ensure_access_url(demo)
    except Exception as exc:  # noqa: BLE001 - surfaced via SyncResult, not raised
        return SyncResult(ok=False, errors=[str(exc)])

    try:
        account_set = fetch_accounts(access_url, start_date=_since_to_epoch(since))
    except Exception as exc:  # noqa: BLE001
        return SyncResult(ok=False, errors=[str(exc)])

    try:
        # Aborts the whole sync -- no partial write -- if two different
        # SimpleFIN accounts would sanitize to the same Assets:SimpleFIN:*
        # name (team-lead decision, 2026-07-30: hard-fail, don't silently
        # merge or uglify the common case with an id suffix).
        check_no_asset_account_collisions(account_set.accounts)
    except Exception as exc:  # noqa: BLE001
        return SyncResult(ok=False, errors=[str(exc)])

    # Ingest owns opening these accounts: rewrite accounts-simplefin.beancount
    # wholesale from whatever SimpleFIN currently reports, sorted and dated
    # with a fixed sentinel so the file is byte-identical across reruns with
    # no new accounts (worker-2's accounts.beancount never hand-opens one of
    # these again).
    asset_accounts_path = paths.accounts_simplefin_ledger()
    asset_accounts_path.parent.mkdir(parents=True, exist_ok=True)
    asset_accounts_path.write_text(
        render_accounts_simplefin_file(
            discover_asset_accounts(account_set), ASSET_ACCOUNT_OPEN_DATE
        ),
        encoding="utf-8",
    )

    normalized_txns, pending_skipped = normalize_account_set(account_set)
    balances = normalize_balances(account_set)

    transactions_dir = paths.transactions_dir()
    transactions_dir.mkdir(parents=True, exist_ok=True)
    # Keyed on (asset_account, simplefin_id), not id alone: the id is only
    # guaranteed unique *within* an account (confirmed live -- the real
    # demo server reuses the same id across different accounts).
    known_keys = existing_simplefin_keys(transactions_dir)
    accounts_with_opening = existing_opening_balance_accounts(transactions_dir)

    by_year: dict[int, list] = {}
    added = 0
    for txn in sorted(normalized_txns, key=lambda t: (t.posted_date, t.asset_account, t.simplefin_id)):
        key = (txn.asset_account, txn.simplefin_id)
        if key in known_keys:
            continue
        by_year.setdefault(txn.posted_date.year, []).append(txn)
        known_keys.add(key)  # guards duplicate keys within one batch too
        added += 1

    # One-time Equity:Opening-Balances plug per account, the first time we
    # ever sync it (see `OpeningBalance` for why: SimpleFIN's date window
    # is capped, so a single sync can never carry an account's full
    # history, and without a plug the balance assertion below can never
    # reconcile for any account with activity older than the window).
    opening_by_year: dict[int, list[OpeningBalance]] = {}
    opening_written = 0
    for account in account_set.accounts:
        asset_account = simplefin_asset_account(account)
        if asset_account in accounts_with_opening:
            continue
        added_for_account = [
            t for txns in by_year.values() for t in txns if t.asset_account == asset_account
        ]
        included_sum = sum((t.amount for t in added_for_account), Decimal(0))
        if added_for_account:
            entry_date = min(t.posted_date for t in added_for_account) - timedelta(days=1)
        else:
            entry_date = datetime.fromtimestamp(account.balance_date, tz=UTC).date()
        opening_by_year.setdefault(entry_date.year, []).append(
            OpeningBalance(
                asset_account=asset_account,
                amount=account.balance - included_sum,
                currency=account.currency,
                entry_date=entry_date,
            )
        )
        accounts_with_opening.add(asset_account)  # guard within-batch duplicates too
        opening_written += 1

    for year in sorted(set(by_year) | set(opening_by_year)):
        year_path = transactions_dir / f"{year}.beancount"
        # Opening plugs are dated before the transactions they reconcile,
        # so they lead the file for anyone reading it chronologically.
        block = "".join(render_opening_balance(ob) for ob in opening_by_year.get(year, []))
        block += "".join(render_transaction(t) for t in by_year.get(year, []))
        with year_path.open("a", encoding="utf-8") as f:
            f.write(block)

    # Wholesale, not merged (team-lead finding, 2026-07-30): a merge-append
    # here let a closed account's assertion survive forever after the
    # account stopped being reported, permanently failing bean-check
    # against data that no longer exists. balances.beancount is the
    # cash-truth guard (PLAN.md 5.2 item 3), not a reconciliation history
    # log -- it always reflects exactly the accounts SimpleFIN reports
    # *this* sync, full stop. See `render_balances_file`.
    balances_path = paths.balances_ledger()
    balances_path.parent.mkdir(parents=True, exist_ok=True)
    balances_path.write_text(render_balances_file(balances), encoding="utf-8")

    # `errlist` (documented) and `errors` (live-observed, not in the
    # protocol doc -- e.g. a >90-day start-date/end-date window being
    # capped) are both soft errors: HTTP 200, data still attached. Neither
    # is dropped just because it isn't the one the spec mentions.
    errors = [f"{e.code}: {e.msg}" for e in account_set.errlist if e.msg]
    errors.extend(account_set.errors)

    # PLAN.md §9 makes git the undo system, "after each sync and each accepted
    # batch". Only the batch half was ever wired: `review`, `apply` and
    # `allocate` all commit, and `sync` did not -- so a sync that no one
    # confirmed afterwards left the ledger written but uncommitted, and there
    # was nothing to revert to. The files a sync writes are exactly the ones
    # that are hardest to reconstruct by hand, since they came from a rate-
    # limited feed that will not serve the same window twice.
    #
    # Committed only when something actually changed: a rerun that adds
    # nothing touches no file (see the module docstring on idempotency), and
    # an empty commit for a no-op sync would be noise in the undo history.
    commit_result = None
    if added or opening_written:
        commit_result = commit_ledger(
            f"Sync {added} transaction(s) from {len(account_set.accounts)} account(s)"
        )

    return SyncResult(
        ok=True,
        accounts_synced=len(account_set.accounts),
        transactions_seen=len(normalized_txns) + pending_skipped,
        transactions_added=added,
        pending_skipped=pending_skipped,
        balances_written=len(balances),
        opening_balances_written=opening_written,
        errors=errors,
        commit=commit_result,
    )
