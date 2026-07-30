"""FastAPI sidecar (PLAN.md §5.1).

The sidecar is the sole ledger writer and the sole holder of the SimpleFIN
Access URL -- neither the credential nor a raw `.beancount` file write ever
crosses this boundary to the browser/Next.js side.

Ledger reads are cached: `loader.load_file` is the dominant latency in any
beancount operation (§5.1), so it is loaded once and reloaded only when the
watched files' mtimes actually change, not on every request. Every endpoint
that writes invalidates that cache before returning.

Writes are opt-in, never default. `POST /categorize` dry-runs unless it is
asked to apply, mirroring the CLI's `--apply` flag, because an endpoint that
rewrites the ledger by default is PLAN.md §7's top risk.
"""

from __future__ import annotations

import importlib
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import beancount
from beancount import loader
from beancount.core import realization
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from bookkeeper import paths
from bookkeeper.envelope.compute import EnvelopeReport, coerce_asof, compute_envelope_state
from bookkeeper.envelope.verify import verify_entries
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


def _import_categorize(module: str) -> ModuleType:
    """Import a `bookkeeper.categorize.*` module at request time, not import time.

    The categorization layer is younger than the sidecar and may be absent or
    half-built. A module-level import would mean one missing file takes the
    whole app down -- including `/health`, which is precisely the endpoint you
    reach for when something is down. Importing here turns that into a single
    503 on a single endpoint.
    """
    name = f"bookkeeper.categorize.{module}"
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{name} is unavailable: {exc}",
        ) from exc


def _categorize_callable(module: str, func: str) -> Any:
    """The named function from a categorize module, or a clean 503.

    A module that exists but doesn't define the function yet is the same
    class of problem as a module that doesn't exist, and deserves the same
    answer rather than an `AttributeError` surfacing as an opaque 500.
    """
    loaded = _import_categorize(module)
    fn = getattr(loaded, func, None)
    if fn is None:
        raise HTTPException(
            status_code=503,
            detail=f"bookkeeper.categorize.{module}.{func} is not defined",
        )
    return fn


def _structured(result: Any) -> dict[str, Any]:
    """The result object's own fields, JSON-safe, without restating its schema.

    The review-queue and categorize result shapes are owned by the
    categorization layer, so this endpoint passes them through structurally
    rather than mirroring every field into a pydantic model that would then
    have to be kept in lockstep. `Decimal`, `date`, and `Enum` values survive
    because FastAPI's response encoder handles them.

    A non-dataclass result yields `{}` rather than an error: the CLI contract
    only promises `.ok` and `.render()`, both of which are reported separately,
    so the endpoint still answers usefully.
    """
    if is_dataclass(result) and not isinstance(result, type):
        return asdict(result)
    return {}


class EnvelopeBalanceModel(BaseModel):
    name: str
    allocated: Decimal
    spent: Decimal
    balance: Decimal
    overspent: bool
    overspend: Decimal


class EnvelopeReportResponse(BaseModel):
    asof: date
    envelopes: list[EnvelopeBalanceModel]
    budgeted_cash: Decimal
    total_envelope_balance: Decimal
    total_overspend: Decimal
    available: Decimal
    summary: str


class VerifyResponse(BaseModel):
    ok: bool
    summary: str
    errors: list[str]
    notes: list[str]


class ReviewQueueResponse(BaseModel):
    ok: bool
    summary: str
    queue: dict[str, Any]


class CategorizeRequest(BaseModel):
    #: Default False, and deliberately so: a POST that rewrites the ledger
    #: unless you opt out is PLAN.md §7's top risk ("auto-apply silently
    #: corrupts the ledger"). Writing is opt-in here exactly as `--apply` is
    #: opt-in on the CLI.
    apply: bool = False
    limit: int | None = None
    use_llm: bool = True


class CategorizeResponse(BaseModel):
    ok: bool
    applied: bool
    summary: str
    result: dict[str, Any]


def _to_envelope_response(report: EnvelopeReport) -> EnvelopeReportResponse:
    return EnvelopeReportResponse(
        asof=report.asof,
        envelopes=[
            EnvelopeBalanceModel(
                name=e.name,
                allocated=e.allocated,
                spent=e.spent,
                balance=e.balance,
                overspent=e.overspent,
                overspend=e.overspend,
            )
            for e in report.envelopes
        ],
        budgeted_cash=report.budgeted_cash,
        total_envelope_balance=report.total_envelope_balance,
        total_overspend=report.total_overspend,
        available=report.available,
        summary=report.render(),
    )


@app.get("/envelopes", response_model=EnvelopeReportResponse)
def envelopes(asof: str | None = None) -> EnvelopeReportResponse:
    """Envelope balances as of `asof` (ISO date; defaults to the UTC day).

    Feeds the cached ledger into the pure `compute_envelope_state` rather than
    calling `envelope_report`, which would reload the ledger from disk on every
    request -- this is Phase 4's `get_envelope_status`, on the chat hot path.
    """
    try:
        report_date = coerce_asof(asof)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"asof must be an ISO date (YYYY-MM-DD), got {asof!r}"
        ) from exc

    entries, errors, _options = _ledger_cache.get()
    if errors:
        # Budget figures derived from a ledger that did not parse would be
        # confidently wrong. `/verify` is the endpoint that reports *why*.
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    return _to_envelope_response(compute_envelope_state(entries, report_date))


@app.get("/verify", response_model=VerifyResponse)
def verify() -> VerifyResponse:
    """Run the ledger + envelope integrity checks (PLAN.md §5.2).

    A failing ledger is a 200 with `ok: false`, not an HTTP error: the request
    succeeded and the findings *are* the payload. Reserving 5xx for genuine
    endpoint failure keeps "the books are wrong" distinguishable from "the
    sidecar is broken".
    """
    entries, bean_errors, _options = _ledger_cache.get()
    result = verify_entries(entries, bean_errors)
    return VerifyResponse(
        ok=result.ok,
        summary=result.render(),
        errors=result.errors,
        notes=result.notes,
    )


@app.get("/review-queue", response_model=ReviewQueueResponse)
def review_queue(limit: int | None = None) -> ReviewQueueResponse:
    """Transactions awaiting human categorization. Read-only."""
    queue = _categorize_callable("review", "review_queue")(limit=limit)
    return ReviewQueueResponse(
        ok=queue.ok,
        summary=queue.render(),
        queue=_structured(queue),
    )


@app.post("/categorize", response_model=CategorizeResponse)
def categorize(req: CategorizeRequest) -> CategorizeResponse:
    """Predict accounts for uncategorized transactions. Dry run unless `apply`."""
    run_categorize = _categorize_callable("apply", "run_categorize")
    try:
        result = run_categorize(apply=req.apply, limit=req.limit, use_llm=req.use_llm)
    finally:
        # In `finally` rather than after the call: a run that wrote some
        # transactions and then raised has still changed the ledger, and a
        # cache left holding pre-write entries is worse than a wasted reload.
        if req.apply:
            _ledger_cache.invalidate()
    return CategorizeResponse(
        ok=result.ok,
        applied=req.apply,
        summary=result.render(),
        result=_structured(result),
    )


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
