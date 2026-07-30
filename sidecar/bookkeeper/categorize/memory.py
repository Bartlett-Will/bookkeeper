"""Tier 1: exact memory of previously confirmed categorizations (PLAN.md §5.4).

Keyed on `normalize_description`, backed by `data/memory.json` -- a small,
human-readable, git-tracked file (a git diff on it is itself an audit trail
of what the user has confirmed over time). Deliberately decoupled from
`LedgerContext.examples`: memory only grows through explicit confirmation
(`record_confirmation`, called when a human accepts or corrects a review
card), not by silently re-deriving itself from whatever happens to already
be posted in the ledger.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from bookkeeper import paths
from bookkeeper.categorize.models import CategorizationInput, LedgerContext, Prediction, Tier
from bookkeeper.categorize.normalize import normalize_description


def memory_path() -> Path:
    return paths.data_dir() / "memory.json"


class MemoryCategorizer:
    """Tier 1. `{normalized_description: {account: confirmation_count}}` on disk.

    A normalized description can carry confirmations to more than one
    account over time (the user changes their mind, or two different real
    merchants happen to collapse to the same normalized key). Majority
    wins; confidence is the winning share of confirmations, not a flat 1.0
    -- a description confirmed 3-to-1 for groceries is a much stronger
    signal than one confirmed 2-to-1, and the §5.5 calibration needs that
    distinction to be honest. A tie between the top accounts is treated as
    "memory doesn't actually know" and abstains, rather than picking
    arbitrarily.
    """

    tier = Tier.MEMORY

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else memory_path()
        self._table: dict[str, dict[str, int]] = self._load()

    def _load(self) -> dict[str, dict[str, int]]:
        if not self._path.exists():
            return {}
        with self._path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {description: dict(counts) for description, counts in raw.items()}

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        key = normalize_description(txn.description)
        counts = self._table.get(key)
        if not counts:
            return None

        total = sum(counts.values())
        best_count = max(counts.values())
        leaders = [account for account, count in counts.items() if count == best_count]
        if len(leaders) != 1:
            return None  # genuine tie between top accounts -- abstain, don't guess

        account = leaders[0]
        if account not in ctx.accounts:
            # Stale entry: confirmed against an account since renamed or
            # closed. Never predict outside the closed label set.
            return None

        confidence = best_count / total
        rationale = f'{best_count}/{total} prior confirmation(s) for "{key}"'
        return Prediction(account=account, confidence=confidence, tier=Tier.MEMORY, rationale=rationale)

    def record_confirmation(self, normalized: str, account: str) -> None:
        """Record one confirmed categorization and persist it immediately."""
        counts = self._table.setdefault(normalized, {})
        counts[account] = counts.get(account, 0) + 1
        self._save()

    def _save(self) -> None:
        # Write-temp-then-rename: this is user data (learned history), and
        # a process killed mid-write must never leave a truncated or
        # half-written memory.json behind.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self._path.parent), prefix=".memory-", suffix=".json.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._table, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_path, self._path)
        except BaseException:
            os.unlink(tmp_path)
            raise
