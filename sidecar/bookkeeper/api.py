"""FastAPI sidecar (PLAN.md §5.1).

The sidecar is the sole ledger writer and the sole holder of the SimpleFIN
Access URL -- neither the credential nor a raw `.beancount` file write ever
crosses this boundary to the browser/Next.js side.

Ledger reads are cached: `loader.load_file` is the dominant latency in any
beancount operation (§5.1), so it is loaded once and reloaded only when the
watched files' mtimes actually change, not on every request.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import beancount
from beancount import loader
from beancount.core import realization
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from bookkeeper import paths
from bookkeeper.ingest.sync import SyncResult, run_sync


class _LedgerCache:
    """Loads `main.beancount`, reloading only when a watched file's mtime changes.

    Watches `main.beancount` plus the files it's expected to `include`
    (accounts, budget, balances, every transactions/*.beancount) directly,
    rather than relying on `main.beancount`'s own mtime, since editing an
    included file doesn't touch the includer's mtime.
    """

    def __init__(self) -> None:
        self._signature: tuple[tuple[str, float], ...] | None = None
        self._entries: list[Any] = []
        self._errors: list[Any] = []
        self._options: dict[str, Any] = {}

    def _watched_paths(self) -> list[Path]:
        watched = [
            paths.main_ledger(),
            paths.accounts_ledger(),
            paths.budget_ledger(),
            paths.balances_ledger(),
        ]
        if paths.transactions_dir().exists():
            watched.extend(sorted(paths.transactions_dir().glob("*.beancount")))
        return watched

    def _signature_now(self) -> tuple[tuple[str, float], ...]:
        sig = []
        for p in self._watched_paths():
            try:
                sig.append((str(p), p.stat().st_mtime))
            except FileNotFoundError:
                continue
        return tuple(sig)

    def get(self) -> tuple[list[Any], list[Any], dict[str, Any]]:
        signature = self._signature_now()
        if signature != self._signature:
            main = paths.main_ledger()
            if main.exists():
                self._entries, self._errors, self._options = loader.load_file(str(main))
            else:
                self._entries, self._errors, self._options = [], [], {}
            self._signature = signature
        return self._entries, self._errors, self._options

    def invalidate(self) -> None:
        self._signature = None


_ledger_cache = _LedgerCache()

app = FastAPI(title="bookkeeper sidecar")


class SyncRequest(BaseModel):
    since: str | None = None
    demo: bool = False


class SyncResponse(BaseModel):
    ok: bool
    summary: str
    accounts_synced: int
    transactions_added: int
    balances_written: int


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "beancount_version": beancount.__version__}


@app.post("/sync", response_model=SyncResponse)
def sync(req: SyncRequest) -> SyncResponse:
    result: SyncResult = run_sync(since=req.since, demo=req.demo)
    # The sidecar is the sole ledger writer; any successful sync just wrote
    # files the cache is watching, so force a reload on the next read
    # rather than waiting on a filesystem mtime granularity race.
    _ledger_cache.invalidate()
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.render())
    return SyncResponse(
        ok=result.ok,
        summary=result.render(),
        accounts_synced=result.accounts_synced,
        transactions_added=result.transactions_added,
        balances_written=result.balances_written,
    )


@app.get("/accounts")
def accounts() -> dict[str, Any]:
    """Current SimpleFIN-derived asset accounts and balances, from the ledger.

    This reads the *ledger*, not SimpleFIN live -- fetching live data is
    reserved for `/sync` given the ~24 req/day rate limit (PLAN.md §3.1).
    """
    entries, errors, _options = _ledger_cache.get()
    if not paths.main_ledger().exists():
        return {
            "accounts": [],
            "note": (
                "ledger/main.beancount not found -- accounts.beancount include "
                "wiring is owned by worker-2 and may not be in place yet"
            ),
        }
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see sidecar logs",
        )

    real_root = realization.realize(entries)
    result = []
    for real_account in realization.iter_children(real_root):
        # Every SimpleFIN-derived account lives directly under Assets: in
        # the current ledger/accounts.beancount convention (worker-2's
        # file) -- there's no separate namespace to filter on. This will
        # over-include if a manually-added, non-SimpleFIN Assets account
        # is ever opened; acceptable for Phase 1's demo-only scope.
        if not real_account.account.startswith("Assets:"):
            continue
        positions = [
            {"number": str(pos.units.number), "currency": pos.units.currency}
            for pos in real_account.balance
        ]
        result.append({"account": real_account.account, "balance": positions})
    return {"accounts": result}


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
