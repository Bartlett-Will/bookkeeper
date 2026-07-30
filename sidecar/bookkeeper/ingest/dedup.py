"""Dedup on SimpleFIN `id`, scoped per account.

Per PLAN.md §3.1 (quoting the protocol doc), the id is unique *per
account* and never reused -- a free, exact idempotency key. We do not need
(and deliberately do not build) the fuzzy candidate-matching machinery a
CSV/OFX importer like beancount-import requires.

"Per account" is load-bearing, not a hedge: live-tested against the real
demo server (2026-07-30), its Checking and Savings accounts return
transactions with *identical* ids for different real transactions on the
same day (ids there look like `posted` timestamps, reused across
accounts). A dedup key on the bare id alone silently drops every one of
those as a false-positive "already recorded" duplicate the first time two
accounts share an id -- confirmed live: 338 fetched, only 169 survived a
bare-id dedup. The key here is therefore `(asset_account, simplefin_id)`,
matching what the protocol actually guarantees rather than what a
looser reading of it would imply.

Scans the already-written `.beancount` transaction files for
`simplefin-id: "..."` / `simplefin-account: "..."` metadata pairs rather
than re-loading the ledger through beancount's parser: it's cheap, it has
no opinion about whether the surrounding entries are otherwise valid, and
a lone transactions/YYYY.beancount fragment referencing accounts opened
elsewhere would not load standalone anyway.
"""

from __future__ import annotations

import re
from pathlib import Path

# `render.py` always emits simplefin-account immediately after
# simplefin-id -- this regex depends on that adjacency.
_KEY_RE = re.compile(
    r'simplefin-id:\s*"([^"]*)"\s*\n\s*simplefin-account:\s*"([^"]*)"'
)

# `render_opening_balance` emits these two lines adjacently, in this order
# -- distinct from (and never matched by) `_KEY_RE` above, since an opening
# entry has no `simplefin-id` at all (see `ingest/render.py` for why the
# two markers are kept separate).
_OPENING_RE = re.compile(
    r'simplefin-account:\s*"([^"]*)"\s*\n\s*simplefin-opening:\s*"true"'
)


def existing_simplefin_keys(transactions_dir: Path) -> set[tuple[str, str]]:
    """All `(asset_account, simplefin_id)` pairs already recorded.

    The tuple order is `(account, id)`, not `(id, account)`, to read
    naturally as "this account already has this id" at call sites.
    """
    keys: set[tuple[str, str]] = set()
    if not transactions_dir.exists():
        return keys
    for path in sorted(transactions_dir.glob("*.beancount")):
        text = path.read_text(encoding="utf-8")
        keys.update((account, txn_id) for txn_id, account in _KEY_RE.findall(text))
    return keys


def existing_opening_balance_accounts(transactions_dir: Path) -> set[str]:
    """Asset accounts that already have an `Equity:Opening-Balances` plug.

    Used to decide whether an account is being synced for the first time
    (needs a plug so its `balance` assertion can reconcile against
    SimpleFIN's window-capped API -- see `NormalizedBalance`/
    `OpeningBalance` in `ingest/normalize.py`) or has one already.
    """
    accounts: set[str] = set()
    if not transactions_dir.exists():
        return accounts
    for path in sorted(transactions_dir.glob("*.beancount")):
        text = path.read_text(encoding="utf-8")
        accounts.update(_OPENING_RE.findall(text))
    return accounts
