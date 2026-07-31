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

Every response is a declared pydantic model, not a bare `dict[str, Any]`.
That is not tidiness: `worker-2` generates the browser's TypeScript from
`/openapi.json`, so a field that is untyped here is `unknown` there, and the
"typed contract, checked end to end" of §5.1 stops being a property and
becomes a claim. `Decimal` is left as `Decimal` throughout -- pydantic
serializes it to a JSON *string*, which is the whole point: money must not
round-trip through a float.

Long work does not run inside a request. `POST /sync/start` registers a job
with `bookkeeper.jobs` and returns immediately (§5.3 rule 3); the UI polls
`GET /sync/status/{job_id}`.
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
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

from bookkeeper import paths
from bookkeeper.envelope.compute import EnvelopeReport, coerce_asof, compute_envelope_state
from bookkeeper.envelope.verify import verify_entries
from bookkeeper.ingest.sync import SyncResult, run_sync

#: The `jobs` registry key for a SimpleFIN sync. One kind means one running
#: sync at a time, which is what keeps a double-clicked *Sync* from spending
#: two of SimpleFIN's ~24 daily requests (§3.1).
SYNC_JOB_KIND = "sync"


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


class HealthResponse(BaseModel):
    status: str
    beancount_version: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", beancount_version=beancount.__version__)


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


class AccountPositionModel(BaseModel):
    """One currency's worth of an account's balance.

    `number` is a `Decimal` and therefore serializes as a string. A balance
    is money; JSON numbers are doubles.
    """

    number: Decimal
    currency: str


class LedgerAccountModel(BaseModel):
    account: str
    balance: list[AccountPositionModel]


class AccountsResponse(BaseModel):
    accounts: list[LedgerAccountModel]
    #: Present (as `null`) even when there is nothing to say, so the
    #: generated TypeScript has one shape rather than two.
    note: str | None = None


class CategorizableAccountsResponse(BaseModel):
    accounts: list[str]


@app.get("/accounts", response_model=AccountsResponse)
def accounts() -> AccountsResponse:
    """Current SimpleFIN-derived asset accounts and balances, from the ledger.

    This reads the *ledger*, not SimpleFIN live -- fetching live data is
    reserved for `/sync` given the ~24 req/day rate limit (PLAN.md §3.1).
    """
    entries, errors, _options = _ledger_cache.get()
    if not paths.main_ledger().exists():
        return AccountsResponse(
            accounts=[],
            note=(
                "ledger/main.beancount not found -- accounts.beancount include "
                "wiring is owned by worker-2 and may not be in place yet"
            ),
        )
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
            AccountPositionModel(number=pos.units.number, currency=pos.units.currency)
            for pos in real_account.balance
        ]
        result.append(LedgerAccountModel(account=real_account.account, balance=positions))
    return AccountsResponse(accounts=result)


def _import_optional(name: str) -> ModuleType:
    """Import a bookkeeper module at request time, not at import time.

    The categorization, jobs and reports layers are younger than the sidecar
    and may be absent or half-built. A module-level import would mean one
    missing file takes the whole app down -- including `/health`, which is
    precisely the endpoint you reach for when something is down. Importing
    here turns that into a single 503 on a single endpoint.
    """
    try:
        return importlib.import_module(name)
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"{name} is unavailable: {exc}",
        ) from exc


def _module_callable(name: str, func: str) -> Any:
    """The named function from a lazily-imported module, or a clean 503.

    A module that exists but doesn't define the function yet is the same
    class of problem as a module that doesn't exist, and deserves the same
    answer rather than an `AttributeError` surfacing as an opaque 500.
    """
    loaded = _import_optional(name)
    fn = getattr(loaded, func, None)
    if fn is None:
        raise HTTPException(status_code=503, detail=f"{name}.{func} is not defined")
    return fn


def _categorize_callable(module: str, func: str) -> Any:
    return _module_callable(f"bookkeeper.categorize.{module}", func)


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


class CommitModel(BaseModel):
    """One auto-commit attempt, mirroring `categorize.gitcommit.CommitResult`.

    Surfaced rather than swallowed because git *is* the undo system (§9): a
    UI that has just written forty transactions needs the sha to tell the
    user what to revert.
    """

    ok: bool
    committed: bool
    sha: str
    message: str
    files: list[str]
    warnings: list[str]


class ReviewEntryModel(BaseModel):
    """One transaction awaiting a decision, mirroring `review.ReviewEntry`.

    `simplefin_id` is the key `POST /review/confirm` batches on, so it is
    typed rather than left to a `dict[str, Any]` that TypeScript would widen
    to `unknown`: a typo in that field silently confirms nothing, and the
    user reads that as "I approved 40 transactions and nothing happened"
    against their own financial records.

    `amount` is a *string* here, not a `Decimal`, because `ReviewEntry`
    already carries it as one -- primitives only, so the queue crosses to
    the browser with no encoder in between.
    """

    simplefin_id: str
    asset_account: str
    posted_date: str
    description: str
    amount: str
    currency: str
    current_account: str
    suggested_account: str | None = None
    confidence: float | None = None
    tier: str | None = None
    rationale: str = ""
    mcc: str | None = None
    payee: str | None = None


class ReviewQueueModel(BaseModel):
    ok: bool
    entries: list[ReviewEntryModel]
    total: int
    errors: list[str]
    warnings: list[str]


class ReviewQueueResponse(BaseModel):
    ok: bool
    summary: str
    queue: ReviewQueueModel


class AutoApplyPolicyModel(BaseModel):
    """Whether unattended writes are permitted at all, and on whose say-so.

    Reported on every categorize response because "auto-apply is OFF" is the
    headline fact about a dry run (Phase 3, measured), not a footnote.
    """

    threshold: float | None = None
    source: str = "default"


class DecisionModel(BaseModel):
    """One prediction and what happened to it, mirroring `apply.Decision`."""

    simplefin_id: str
    asset_account: str
    posted_date: str
    description: str
    amount: str
    currency: str
    disposition: str
    suggested_account: str | None = None
    confidence: float | None = None
    tier: str | None = None
    rationale: str = ""
    mcc: str | None = None
    payee: str | None = None


class CategorizeResultModel(BaseModel):
    ok: bool
    policy: AutoApplyPolicyModel
    applied: bool
    decisions: list[DecisionModel]
    files_written: list[str]
    commit: CommitModel | None = None
    errors: list[str]
    warnings: list[str]


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
    result: CategorizeResultModel


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


@app.get("/accounts/categorizable", response_model=CategorizableAccountsResponse)
def categorizable_accounts() -> CategorizableAccountsResponse:
    """The open expense/income accounts a categorization may name.

    Deliberately *not* `/accounts`, which answers a different question (which
    bank accounts exist and what is in them) and filters on `Assets:`.

    The set comes from `categorize.context.build_ledger_context`, not from a
    filter written here, because it is the same set the cascade is
    constrained to predict into. Two definitions of "a valid account" would
    drift, and the drift would surface as a review-card dropdown offering an
    account the categorizer could never suggest -- and then as
    `POST /review/confirm` rejecting a whole batch over an account the UI
    itself proposed. `LedgerContext.examples` is built and thrown away here;
    one definition is worth a scan over already-cached entries.
    """
    build_ledger_context = _module_callable(
        "bookkeeper.categorize.context", "build_ledger_context"
    )
    entries, errors, _options = _ledger_cache.get()
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    return CategorizableAccountsResponse(accounts=list(build_ledger_context(entries).accounts))


# --------------------------------------------------------------------------
# Confirmation (§5.3 rule 2)
# --------------------------------------------------------------------------


class ConfirmationModel(BaseModel):
    """One human decision. Keyed on `(asset_account, simplefin_id)`.

    Both halves are required because SimpleFIN ids are unique per account,
    not globally (`ingest/dedup.py`) -- the id alone would address two
    different transactions at two different banks.
    """

    asset_account: str
    simplefin_id: str
    account: str


class ConfirmRequest(BaseModel):
    #: A batch, always. §5.3 rule 2 has a human approving forty review cards,
    #: and that must be one ledger pass and one commit, not forty of each.
    #: A single confirmation is a batch of one.
    confirmations: list[ConfirmationModel]


class ConfirmResponse(BaseModel):
    ok: bool
    summary: str
    confirmed: int
    learned: int
    files_written: list[str]
    commit: CommitModel | None = None
    errors: list[str]
    warnings: list[str]


def _commit_model(commit: Any) -> CommitModel | None:
    return None if commit is None else CommitModel(**asdict(commit))


@app.post("/review/confirm", response_model=ConfirmResponse)
def review_confirm(req: ConfirmRequest) -> ConfirmResponse:
    """Accept a batch of human categorizations: write them, and teach tier 1.

    This is what an *Accept* button hits. It is deliberately not one of the
    six chat tools (§5.3 rule 2): approving forty transactions must be forty
    deterministic HTTP calls and zero LLM calls, so the button reaches this
    endpoint directly and the model is never in the loop.

    A rejected batch is a 200 with `ok: false` and one error per bad
    confirmation, not a 500. The findings *are* the payload -- the UI has to
    show the user which account it refused and why -- and flattening them
    into a 500 would leave "your correction is invalid" indistinguishable
    from "the sidecar fell over" (the same reasoning as `/verify`).

    `context` is passed so `confirm_categorizations` actually runs its
    open-account check; without it the guard is inert and a typo'd account
    lands in the ledger. That check rejects the *whole* batch, which is why
    `GET /accounts/categorizable` exists for the UI to pre-validate against.
    """
    confirm = _categorize_callable("review", "confirm_categorizations")
    confirmation_type = _categorize_callable("review", "Confirmation")
    build_ledger_context = _module_callable(
        "bookkeeper.categorize.context", "build_ledger_context"
    )

    entries, errors, _options = _ledger_cache.get()
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )

    batch = [
        confirmation_type(
            asset_account=c.asset_account,
            simplefin_id=c.simplefin_id,
            account=c.account,
        )
        for c in req.confirmations
    ]
    try:
        result = confirm(batch, context=build_ledger_context(entries))
    finally:
        # In `finally`: a batch that wrote some transactions and then raised
        # has still changed the ledger, and a cache holding pre-write entries
        # is worse than a wasted reload.
        _ledger_cache.invalidate()

    return ConfirmResponse(
        ok=result.ok,
        summary=result.render(),
        confirmed=result.confirmed,
        learned=result.learned,
        files_written=list(result.files_written),
        commit=_commit_model(result.commit),
        errors=list(result.errors),
        warnings=list(result.warnings),
    )


# --------------------------------------------------------------------------
# Background sync (§5.3 rule 3)
# --------------------------------------------------------------------------


class SyncStartRequest(BaseModel):
    since: str | None = None
    demo: bool = False


class SyncStartResponse(BaseModel):
    job_id: str
    kind: str
    state: str
    #: False when a sync was already running and this request was handed that
    #: job instead of launching a second one. The caller polls the same way
    #: either way; this only explains why.
    started: bool


class SyncJobResult(BaseModel):
    """What a finished sync job produced.

    Typed concretely rather than left as a free dict because this is the
    payload `GET /sync/status/{job_id}` hands the browser, and an untyped
    `result` is exactly the `unknown` that costs the web layer its safety.
    The endpoint 404s a job of any other kind, so this stays honest.
    """

    ok: bool
    summary: str
    accounts_synced: int
    transactions_seen: int
    transactions_added: int
    pending_skipped: int
    balances_written: int
    opening_balances_written: int


class SyncStatusResponse(BaseModel):
    job_id: str
    kind: str
    state: str
    progress: int
    total: int
    step: str
    result: SyncJobResult | None = None
    error: str | None = None
    started_at: float
    finished_at: float | None = None
    done: bool
    summary: str


def _sync_job(since: str | None, demo: bool) -> Any:
    """The unit of work behind `POST /sync/start`.

    Returns a plain dict so the registry can hand it back verbatim; raises on
    failure so the registry records `state=failed` with the reason attached.
    Returning a failed `SyncResult` as a *success* would leave the UI showing
    a green sync that fetched nothing.
    """

    def work(progress: Any) -> dict[str, Any]:
        progress.report(step="fetching from SimpleFIN", progress=0, total=2)
        result: SyncResult = run_sync(since=since, demo=demo)
        # The sync is the sole ledger writer here and has just written files
        # the cache watches; force a reload rather than race mtime
        # granularity. Safe from the worker thread: `invalidate` only clears
        # the signature, so the worst case is one extra load.
        _ledger_cache.invalidate()
        progress.report(step="ledger updated", progress=2)
        if not result.ok:
            raise RuntimeError(result.render())
        return {
            "ok": result.ok,
            "summary": result.render(),
            "accounts_synced": result.accounts_synced,
            "transactions_seen": result.transactions_seen,
            "transactions_added": result.transactions_added,
            "pending_skipped": result.pending_skipped,
            "balances_written": result.balances_written,
            "opening_balances_written": result.opening_balances_written,
        }

    return work


@app.post("/sync/start", response_model=SyncStartResponse)
def sync_start(req: SyncStartRequest) -> SyncStartResponse:
    """Kick off a sync and return immediately with its job id.

    §5.3 rule 3: a sync fetches over the network and can be followed by
    categorizing dozens of transactions at ~1-2s each. Doing that inside the
    request would stall a chat turn for a minute, so the work runs on a
    background thread and the UI polls `GET /sync/status/{job_id}`.

    Starting a sync while one is already running returns the running job
    rather than launching a second: SimpleFIN allows on the order of 24
    requests a day (§3.1), so a double-clicked *Sync* is not a wasted thread,
    it is a meaningful slice of the daily budget.
    """
    jobs = _import_optional("bookkeeper.jobs")
    snapshot, started = jobs.registry.start(
        SYNC_JOB_KIND, _sync_job(req.since, req.demo), total=2
    )
    return SyncStartResponse(
        job_id=snapshot.job_id,
        kind=snapshot.kind,
        state=snapshot.state.value,
        started=started,
    )


@app.get("/sync/status/{job_id}", response_model=SyncStatusResponse)
def sync_status(job_id: str) -> SyncStatusResponse:
    """Where a sync job has got to. Cheap enough to poll."""
    jobs = _import_optional("bookkeeper.jobs")
    snapshot = jobs.registry.get(job_id)
    if snapshot is None or snapshot.kind != SYNC_JOB_KIND:
        # A forgotten job and a job that never existed are the same answer to
        # a poller: there is nothing here to wait for. The registry keeps a
        # bounded history, so "forgotten" is a real case, not a bug.
        raise HTTPException(status_code=404, detail=f"no sync job with id {job_id!r}")
    return SyncStatusResponse(
        job_id=snapshot.job_id,
        kind=snapshot.kind,
        state=snapshot.state.value,
        progress=snapshot.progress,
        total=snapshot.total,
        step=snapshot.step,
        result=None if snapshot.result is None else SyncJobResult(**snapshot.result),
        error=snapshot.error,
        started_at=snapshot.started_at,
        finished_at=snapshot.finished_at,
        done=snapshot.done,
        summary=snapshot.render(),
    )


# --------------------------------------------------------------------------
# Allocation -- the one write tool the chat layer exposes
# --------------------------------------------------------------------------


class AllocateRequest(BaseModel):
    envelope: str
    #: `Decimal`, never `float`. An 8B model picks this argument (§3.3) and
    #: it becomes a line in a budget file; `0.1` as a double is not `0.1`.
    amount: Decimal
    currency: str = "USD"
    allocated_on: str | None = None


class AllocateResponse(BaseModel):
    ok: bool
    summary: str
    envelope: str
    amount: Decimal
    currency: str
    allocated_on: date | None = None
    directive: str
    path: str
    available: Decimal | None = None
    over_allocated: bool
    #: Every envelope the ledger knows about. Returned on failure too, and
    #: especially then: a model that invented "Groccerys" needs the real list
    #: to correct itself without another round trip.
    known_envelopes: list[str]
    commit: CommitModel | None = None
    errors: list[str]
    warnings: list[str]


@app.post("/envelopes/allocate", response_model=AllocateResponse)
def envelopes_allocate(req: AllocateRequest) -> AllocateResponse:
    """Move money into an envelope by appending an `allocate` directive.

    A refused allocation -- unknown envelope, negative amount, a directive
    that would not parse -- is a 200 with `ok: false`, its reasons, and the
    known envelope names. The request succeeded and the refusal is the
    payload, exactly as with `/verify`; a 4xx here would also throw away
    `known_envelopes`, which is the field that lets a caller fix its own
    mistake.

    Over-allocation is reported, never prevented: `over_allocated` and the
    recomputed `available` come back on a successful write, because §5.2's
    `verify` is what judges a budget, and the numbers move under the user's
    feet whenever a sync lands.
    """
    allocate = _module_callable("bookkeeper.envelope.allocate", "allocate_to_envelope")
    entries, errors, _options = _ledger_cache.get()
    try:
        result = allocate(
            req.envelope,
            req.amount,
            currency=req.currency,
            allocated_on=req.allocated_on,
            entries=entries,
            errors=errors,
        )
    finally:
        # Unconditional: `allocate_to_envelope` appends before it validates
        # and rolls back, so even a refused allocation may have touched
        # budget.beancount between those two points.
        _ledger_cache.invalidate()

    return AllocateResponse(
        ok=result.ok,
        summary=result.render(),
        envelope=result.envelope,
        amount=result.amount,
        currency=result.currency,
        allocated_on=result.allocated_on,
        directive=result.directive,
        path=result.path,
        available=result.available,
        over_allocated=result.over_allocated,
        known_envelopes=list(result.known_envelopes),
        commit=_commit_model(result.commit),
        errors=list(result.errors),
        warnings=list(result.warnings),
    )


# --------------------------------------------------------------------------
# Reads: search and spending
# --------------------------------------------------------------------------


class TransactionMatchModel(BaseModel):
    posted_date: date
    description: str
    amount: Decimal
    currency: str
    account: str
    categorized_account: str | None = None
    envelope: str | None = None
    simplefin_id: str | None = None
    payee: str | None = None
    memo: str | None = None


class TransactionSearchResponse(BaseModel):
    ok: bool
    summary: str
    query: str
    matches: list[TransactionMatchModel]
    total: int
    limit: int
    truncated: bool
    errors: list[str]
    warnings: list[str]


@app.get("/transactions/search", response_model=TransactionSearchResponse)
def transactions_search(
    q: str, limit: int | None = None
) -> TransactionSearchResponse:
    """Free-text search over ledger transactions. Read-only.

    `q` is untrusted -- it is typed by a user or emitted by an 8B model --
    and `reports.search` is where that is dealt with (bound query parameter,
    escaped as a literal pattern). The cached ledger is fed in rather than
    reloaded per keystroke (§5.1).

    An empty `q` comes back as `ok: false` with a reason rather than a 422:
    the caller is often a model, and a structured "nothing to search for"
    is easier for it to recover from than an HTTP error.
    """
    search = _module_callable("bookkeeper.reports.search", "search_transactions")
    entries, errors, options = _ledger_cache.get()
    if errors:
        # A partially-parsed ledger yields silently missing rows, which for a
        # search over financial records is the worst possible failure: it
        # looks exactly like "you never spent that".
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    result = search(q, limit=limit, entries=entries, errors=errors, options=options)
    return TransactionSearchResponse(
        ok=result.ok,
        summary=result.render(),
        query=result.query,
        matches=[TransactionMatchModel(**asdict(m)) for m in result.matches],
        total=result.total,
        limit=result.limit,
        truncated=result.truncated,
        errors=list(result.errors),
        warnings=list(result.warnings),
    )


class SpendPointModel(BaseModel):
    period: str
    amount: Decimal


class EnvelopeSeriesModel(BaseModel):
    name: str
    total: Decimal
    points: list[SpendPointModel]


class SpendingReportResponse(BaseModel):
    ok: bool
    summary: str
    from_date: date
    to_date: date
    period: str
    currency: str
    periods: list[str]
    envelopes: list[EnvelopeSeriesModel]
    total: Decimal
    #: Spending that belongs to no envelope, reported rather than dropped.
    #: With auto-apply off, nearly everything still posts to
    #: `Expenses:Unknown`, and a report that quietly summed only mapped
    #: accounts would render as "you spent nothing".
    unmapped_total: Decimal
    unmapped_accounts: list[str]
    errors: list[str]
    warnings: list[str]


@app.get("/reports/spending", response_model=SpendingReportResponse)
def reports_spending(
    from_date: str | None = Query(default=None, alias="from"),
    to: str | None = None,
    period: str = "month",
) -> SpendingReportResponse:
    """Spend by envelope over time. Both bounds inclusive, both optional.

    Defaults to the ledger's own first and last transaction dates rather than
    a wall-clock window, so a report of a fixed ledger does not change meaning
    because a day passed.

    `period` is validated by `reports.spending` against its own `PERIODS`
    rather than re-listed here; an unknown period or an unparseable date is a
    422, because unlike a refused allocation there is no useful payload to
    return -- the request cannot be answered at all.
    """
    spending_report = _module_callable("bookkeeper.reports.spending", "spending_report")
    entries, errors, options = _ledger_cache.get()
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    try:
        report = spending_report(
            from_date, to, period, entries=entries, errors=errors, options=options
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return SpendingReportResponse(
        ok=report.ok,
        summary=report.render(),
        from_date=report.from_date,
        to_date=report.to_date,
        period=report.period,
        currency=report.currency,
        periods=list(report.periods),
        envelopes=[
            EnvelopeSeriesModel(
                name=series.name,
                total=series.total,
                points=[SpendPointModel(period=p.period, amount=p.amount) for p in series.points],
            )
            for series in report.envelopes
        ],
        total=report.total,
        unmapped_total=report.unmapped_total,
        unmapped_accounts=list(report.unmapped_accounts),
        errors=list(report.errors),
        warnings=list(report.warnings),
    )


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
