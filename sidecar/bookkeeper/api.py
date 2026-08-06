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
from pydantic import BaseModel, ConfigDict

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


class StrictRequest(BaseModel):
    """Base for every request body: an unknown field is a 422, not a shrug.

    Pydantic's default is `extra="ignore"`, which silently drops a field it
    does not recognise. For a financial write that is the worst available
    behaviour: sending `date` instead of `allocated_on` to
    `POST /envelopes/allocate` returned 200 and recorded *today*, so a
    caller asking to backdate an allocation got a wrong date in the ledger
    with nothing anywhere to say so.

    An 8B model picks these arguments (§3.3) and a near-miss on a field name
    is precisely what one emits. Every *other* bad argument to that endpoint
    -- a bad currency, a negative amount, an invented envelope -- already
    comes back refused with a reason; accepting an unknown field while
    refusing those was an inconsistency that only read as deliberate if you
    knew the pydantic default.

    Requests only. Responses stay permissive: we construct those ourselves,
    and forbidding extras there would turn a new field in an upstream result
    dict into a 500 on an endpoint that could have answered.
    """

    model_config = ConfigDict(extra="forbid")


class SyncRequest(StrictRequest):
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
    #: Which ledger this process is actually serving.
    #:
    #: Present because *no other response can answer it*. Several sidecars run
    #: side by side during development -- the real ledger, fixture trees, and
    #: throwaway copies -- and a copy returns byte-identical balances, account
    #: lists and queue totals to the original. No value-based probe can tell
    #: them apart, so identifying a server previously meant reading
    #: `BOOKKEEPER_ROOT` out of its process environment from outside. That is
    #: not available to the browser, and it is the check you most want
    #: immediately before a write.
    #:
    #: Not a credential. PLAN.md §9 protects the SimpleFIN Access URL and
    #: account credentials; a local directory path is neither, and
    #: `/envelopes/allocate` already returns `path` on every response.
    root: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """Liveness, the beancount version, and which ledger is being served.

    `root` is what `paths.root()` actually computed, symlink-resolved --
    deliberately not the `BOOKKEEPER_ROOT` env var. An unset var must report
    the real repo path rather than an empty string, because the whole value
    of the field is that it answers without the caller knowing how the
    process was configured.
    """
    return HealthResponse(
        status="ok",
        beancount_version=beancount.__version__,
        root=str(paths.root().resolve()),
    )


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


class CategorizeRequest(StrictRequest):
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


class ConfirmationModel(StrictRequest):
    """One human decision. Keyed on `(asset_account, simplefin_id)`.

    Both halves are required because SimpleFIN ids are unique per account,
    not globally (`ingest/dedup.py`) -- the id alone would address two
    different transactions at two different banks.
    """

    asset_account: str
    simplefin_id: str
    account: str


class ConfirmRequest(StrictRequest):
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


class SyncStartRequest(StrictRequest):
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


class AllocateRequest(StrictRequest):
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


class CurrencyTotalModel(BaseModel):
    """What the matches add up to, in one currency, in each direction.

    Never one scalar. A match set spans accounts, currencies and both
    directions, and a lone figure labelled "total" over that mixture is a
    claim someone would act on and that would be false. `spent` and
    `received` are kept apart so a refund cannot quietly answer "how much did
    I spend" with a smaller number than was spent; `transferred` is money
    moved between the user's own accounts and is in neither, because the
    search returns both legs of a transfer and counting them would report the
    same dollar twice.
    """

    currency: str
    spent: Decimal
    received: Decimal
    net: Decimal
    transferred: Decimal
    spend_count: int
    receipt_count: int
    transfer_count: int
    accounts: list[str]


class TransactionSearchResponse(BaseModel):
    ok: bool
    summary: str
    query: str
    matches: list[TransactionMatchModel]
    #: A *count* of matching transactions. The money is `amount_totals`,
    #: named apart from this deliberately: `total` and `totals` one keystroke
    #: apart in the generated TypeScript is a bug waiting to be written.
    total: int
    #: One entry per currency, over *every* match rather than the `limit`ed
    #: page in `matches` — a total over the visible rows would understate the
    #: answer exactly when the result was large enough to need one.
    amount_totals: list[CurrencyTotalModel]
    #: True when the matches span more than one currency, so no combined
    #: figure exists. The ledger carries no exchange rates; a client must
    #: render the currencies separately rather than pick one.
    mixed_currency: bool
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
        amount_totals=[CurrencyTotalModel(**asdict(t)) for t in result.amount_totals],
        mixed_currency=result.mixed_currency,
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


class MonthEndEnvelopeModel(BaseModel):
    """One envelope's month.

    `overspent` and `over_budget` are two different failures. Overspent means
    the running balance is negative — money already spent with nothing behind
    it. Over budget means this month's spend exceeded this month's allocation,
    which an envelope with a healthy carried balance can do without ever going
    negative. A client that collapsed them would raise a false alarm or hide a
    real one.
    """

    name: str
    opening_balance: Decimal
    allocated: Decimal
    spent: Decimal
    closing_balance: Decimal
    remaining: Decimal
    overspend: Decimal
    #: Fraction of the allocation consumed (0.8333 is 83.33%). A ratio is not
    #: money, so it is a `float` rather than a `Decimal` string. `None`, never
    #: `0.0`, when nothing was allocated: 0.0 reads as untouched and 1.0 as
    #: exhausted, and a zero budget supports neither claim.
    consumed_ratio: float | None = None
    #: `within` | `over` | `unbudgeted` | `unused`.
    status: str
    #: `up` | `down` | `flat` | `insufficient_data`, read over a trailing
    #: window rather than this month alone. `insufficient_data` is an
    #: abstention and is **not** `flat`: `flat` means the slope was measured
    #: and is small. `direction_reason` says which rule fired, and the two
    #: period counts below make the abstention checkable rather than merely
    #: stated.
    direction: str
    direction_reason: str
    periods_observed: int = 0
    periods_required: int = 0
    overspent: bool
    over_budget: bool


class MonthEndOutlierModel(BaseModel):
    """One transaction that is unusual for its envelope.

    Carries `median`, `scale` and `scale_method` with it so `score` can be
    recomputed from this one record. A flag whose basis is not visible is not
    a finding a user can check.
    """

    envelope: str
    posted_date: date
    description: str
    amount: Decimal
    score: Decimal
    median: Decimal
    scale: Decimal
    scale_method: str
    threshold: Decimal


class MonthEndReportResponse(BaseModel):
    ok: bool
    summary: str
    month: str
    label: str
    from_date: date
    to_date: date
    #: What the closing figures are computed at: the month's last day, or
    #: today when the month is not over. Never a future date.
    asof: date
    #: `future` | `in-progress` | `no-data` | `partial` | `complete`. "So far
    #: this month" and "for July" are different claims and a user acts on them
    #: differently, so a client must not render them with the same words.
    coverage: str
    data_through: date | None = None
    #: The last date within the month the ledger accounts for. Distinct from
    #: `data_through` (the month's last transaction): a January whose last
    #: transaction is the 28th is still covered to the 31st, because the
    #: ledger continuing into February is evidence nothing happened rather
    #: than that nobody looked. `null` only when nothing is covered — the
    #: month has not started, or begins after the ledger's last transaction.
    through: date | None = None
    #: True when the month is over *and* the ledger accounts for all of it,
    #: i.e. the figures are done moving. Deliberately not `coverage ==
    #: "complete"`: a finished month the ledger continues past but in which
    #: nothing happened is `no-data` and is nonetheless final.
    complete: bool
    #: With `days_in_month`, what lets a client say "4 of 31 days" instead of
    #: rendering four days of August exactly like a finished July.
    days_elapsed: int
    days_in_month: int
    transactions: int
    #: Counts, not money — required alongside `unmapped_total`, not instead
    #: of it. With auto-apply off, "$0.00 in envelopes" and "$4,102 across 87
    #: transactions nobody has filed" are the same underlying state, and only
    #: the second reads as something to act on.
    #:
    #: These need not sum to `transactions`: a split with one mapped and one
    #: unmapped leg is honestly in both, and a transfer or paycheck is in
    #: neither.
    categorized_count: int
    uncategorized_count: int
    currency: str
    envelopes: list[MonthEndEnvelopeModel]
    opening_total: Decimal
    allocated_total: Decimal
    spent_total: Decimal
    closing_total: Decimal
    #: Spending this month that reached no envelope. With auto-apply off,
    #: nearly everything still posts to `Expenses:Unknown`, and a client that
    #: rendered only the per-envelope table would show a month of real
    #: spending as a month of zeros.
    unmapped_total: Decimal
    unmapped_accounts: list[str]
    #: `spent_total + unmapped_total` — the month's actual spending, and the
    #: denominator the per-envelope table must be read against.
    total_spend: Decimal
    #: `none` | `partial` | `full` | `no-spend`.
    categorization: str
    categorized_share: Decimal
    budgeted_cash: Decimal
    available: Decimal
    total_overspend: Decimal
    #: Unusual transactions dated within the reported month, judged against
    #: the trailing window `trend_from`..`trend_to` — a month is too short to
    #: be its own baseline.
    outliers: list[MonthEndOutlierModel]
    trend_from: date | None = None
    trend_to: date | None = None
    #: Envelopes with too little history to judge for outliers. Present so a
    #: client can say "nothing unusual among the N we could check" instead of
    #: the unfalsifiable "nothing looks unusual".
    unjudged: list[str]
    errors: list[str]
    warnings: list[str]


@app.get("/reports/month-end", response_model=MonthEndReportResponse)
def reports_month_end(month: str | None = None) -> MonthEndReportResponse:
    """The composite month-end report: envelopes, budget vs actual, unmapped.

    `month` is `YYYY-MM` and defaults to the month of the ledger's last
    transaction rather than the wall-clock month, for the same reason
    `/reports/spending` defaults its window off the ledger's own bounds: a
    report of a fixed ledger must not become an empty one because a day
    passed.

    An unparseable `month` is a 422 — unlike a refused allocation there is no
    useful payload to return, because there is no month to report on.
    """
    month_end_report = _module_callable("bookkeeper.reports.monthend", "month_end_report")
    entries, errors, options = _ledger_cache.get()
    if errors:
        # Budget figures derived from a ledger that did not parse would be
        # confidently wrong, and this is the report a user reads as final.
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    try:
        report = month_end_report(month, entries=entries, errors=errors, options=options)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return MonthEndReportResponse(
        ok=report.ok,
        summary=report.render(),
        month=report.month,
        label=report.label,
        from_date=report.from_date,
        to_date=report.to_date,
        asof=report.asof,
        coverage=report.coverage,
        data_through=report.data_through,
        through=report.through,
        complete=report.complete,
        days_elapsed=report.days_elapsed,
        days_in_month=report.days_in_month,
        transactions=report.transactions,
        categorized_count=report.categorized_count,
        uncategorized_count=report.uncategorized_count,
        currency=report.currency,
        envelopes=[
            MonthEndEnvelopeModel(
                name=e.name,
                opening_balance=e.opening_balance,
                allocated=e.allocated,
                spent=e.spent,
                closing_balance=e.closing_balance,
                remaining=e.remaining,
                overspend=e.overspend,
                consumed_ratio=e.consumed_ratio,
                status=e.status,
                direction=e.direction,
                direction_reason=e.direction_reason,
                periods_observed=e.periods_observed,
                periods_required=e.periods_required,
                overspent=e.overspent,
                over_budget=e.over_budget,
            )
            for e in report.envelopes
        ],
        opening_total=report.opening_total,
        allocated_total=report.allocated_total,
        spent_total=report.spent_total,
        closing_total=report.closing_total,
        unmapped_total=report.unmapped_total,
        unmapped_accounts=list(report.unmapped_accounts),
        total_spend=report.total_spend,
        categorization=report.categorization,
        categorized_share=report.categorized_share,
        budgeted_cash=report.budgeted_cash,
        available=report.available,
        total_overspend=report.total_overspend,
        outliers=[MonthEndOutlierModel(**asdict(o)) for o in report.outliers],
        trend_from=report.trend_from,
        trend_to=report.trend_to,
        unjudged=list(report.unjudged),
        errors=list(report.errors),
        warnings=list(report.warnings),
    )


class BudgetLineModel(BaseModel):
    """One envelope's allocation against its actual spending over a window.

    `consumed_ratio` is nullable and that is load-bearing, not laziness: an
    allocation of zero has no ratio, and both plausible substitutes are wrong
    in opposite directions (0 reads as untouched, 1 as exhausted). `null` is
    the only value a client cannot misread, and TypeScript will make it
    handle the case.

    The type asymmetry on the next three fields is deliberate. A ratio is not
    money, so `consumed_ratio` is a genuine JSON number; `overspend` is money
    and therefore a `Decimal` rendered as a string. `overspent` is a
    server-computed boolean so the UI reads a flag rather than deriving one
    by parsing a string -- a browser cannot do `Decimal` arithmetic and must
    not try. This mirrors `EnvelopeBalanceModel`, which carries the same pair
    for the same reason after the Phase 3 `available` fix.
    """

    name: str
    allocated: Decimal
    spent: Decimal
    remaining: Decimal
    consumed_ratio: float | None = None
    status: str
    overspent: bool
    overspend: Decimal
    carried_in: Decimal
    balance: Decimal


class BudgetReportResponse(BaseModel):
    ok: bool
    summary: str
    from_date: date
    to_date: date
    currency: str
    envelopes: list[BudgetLineModel]
    total_allocated: Decimal
    total_spent: Decimal
    total_remaining: Decimal
    total_overspend: Decimal
    #: Spending in the window that belongs to no envelope, and therefore to
    #: no budget line. With auto-apply off nearly everything still posts to
    #: `Expenses:Unknown`, so a report that omitted this would read as "you
    #: spent nothing".
    unmapped_total: Decimal
    unmapped_accounts: list[str]
    errors: list[str]
    warnings: list[str]


@app.get("/reports/budget", response_model=BudgetReportResponse)
def reports_budget(
    from_date: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> BudgetReportResponse:
    """Allocated vs actually spent per envelope. Both bounds inclusive and optional.

    Defaults to the ledger's own first and last transaction dates rather than
    a wall-clock window, for the reason `/reports/spending` does: a report of
    a fixed ledger must not change meaning because a day passed.

    An unparseable date is a 422 -- there is no useful payload to return,
    since the request cannot be answered at all. A backwards window is a 200
    with `ok: false`, because that request *was* answered: the answer is that
    it describes no window.
    """
    budget_report = _module_callable("bookkeeper.reports.budget", "budget_report")
    entries, errors, options = _ledger_cache.get()
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    try:
        report = budget_report(from_date, to, entries=entries, errors=errors, options=options)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return BudgetReportResponse(
        ok=report.ok,
        summary=report.render(),
        from_date=report.from_date,
        to_date=report.to_date,
        currency=report.currency,
        # Field by field rather than `**asdict(line)`: `overspent` is a
        # derived property, not a dataclass field, so `asdict` would drop it
        # and the model would reject the call. Spelling it out keeps the
        # derivation on the one side that owns it.
        envelopes=[
            BudgetLineModel(
                name=line.name,
                allocated=line.allocated,
                spent=line.spent,
                remaining=line.remaining,
                consumed_ratio=line.consumed_ratio,
                status=line.status,
                overspent=line.overspent,
                overspend=line.overspend,
                carried_in=line.carried_in,
                balance=line.balance,
            )
            for line in report.envelopes
        ],
        total_allocated=report.total_allocated,
        total_spent=report.total_spent,
        total_remaining=report.total_remaining,
        total_overspend=report.total_overspend,
        unmapped_total=report.unmapped_total,
        unmapped_accounts=list(report.unmapped_accounts),
        errors=list(report.errors),
        warnings=list(report.warnings),
    )


class EnvelopeTrendModel(BaseModel):
    """One envelope's direction, with the figures the verdict was read from.

    `slope`, `mean` and `relative_slope` are returned as the values actually
    used, so a client can recompute the classification instead of taking it
    on faith. They are `Decimal` and therefore JSON strings: a trend
    statistic is derived from money and must not be finished in a float in
    the browser.

    `direction` is one of `up`, `down`, `flat`, `insufficient_data`. The
    fourth is abstention and is *not* `flat`: `flat` means the slope was
    measured and is small, which is a finding a user acts on differently
    from "we don't know yet". It is a fourth enum value rather than a null
    `direction` so a client is never asked to infer abstention from an
    absence. `reason`, `periods_observed` and `periods_required` make the
    verdict interrogable either way.
    """

    name: str
    direction: str
    periods_observed: int
    periods_required: int
    total: Decimal
    mean: Decimal
    slope: Decimal
    relative_slope: Decimal | None = None
    reason: str
    points: list[SpendPointModel]


class OutlierModel(BaseModel):
    """One transaction that is unusual for its envelope.

    Deliberately self-contained: `median`, `scale` and `threshold` travel
    with the flag so `(amount - median) / scale` can be recomputed from a
    single card. An outlier a user cannot interrogate is worse than none, and
    a chat surface renders these one at a time.
    """

    envelope: str
    posted_date: date
    description: str
    amount: Decimal
    score: Decimal
    median: Decimal
    scale: Decimal
    scale_method: str
    threshold: Decimal


class OutlierAssessmentModel(BaseModel):
    """Whether an envelope was judged for outliers at all, and on what basis.

    Present for every envelope so that "nothing unusual" stays
    distinguishable from "not enough data to look". Collapsing the two is how
    a report ends up asserting the unfalsifiable "nothing looks unusual".
    """

    envelope: str
    sample_size: int
    judged: bool
    reason: str
    median: Decimal | None = None
    scale: Decimal | None = None
    scale_method: str = ""
    outliers_found: int = 0


class TrendsReportResponse(BaseModel):
    ok: bool
    summary: str
    from_date: date
    to_date: date
    currency: str
    periods: list[str]
    envelopes: list[EnvelopeTrendModel]
    outliers: list[OutlierModel]
    assessments: list[OutlierAssessmentModel]
    unmapped_total: Decimal
    unmapped_accounts: list[str]
    unmapped_transactions: int
    #: The thresholds this run applied, echoed so the response describes its
    #: own method. A tool result that states a finding without the rule that
    #: produced it is exactly the unfalsifiable claim §5.3 warns about.
    min_periods: int
    flat_band: Decimal
    min_transactions: int
    outlier_threshold: Decimal
    errors: list[str]
    warnings: list[str]


@app.get("/reports/trends", response_model=TrendsReportResponse)
def reports_trends(
    from_date: str | None = Query(default=None, alias="from"),
    to: str | None = None,
) -> TrendsReportResponse:
    """Spending direction per envelope, and transactions unusual for theirs.

    Monthly periods only, and deliberately: an envelope budget is a monthly
    instrument (§5.2), and a granularity parameter would let a caller pick
    the one that produced the answer it wanted.

    Same error split as `/reports/budget`: an unparseable date is a 422, a
    window that describes nothing is a 200 with `ok: false`.
    """
    trends_report = _module_callable("bookkeeper.reports.trends", "trends_report")
    entries, errors, options = _ledger_cache.get()
    if errors:
        raise HTTPException(
            status_code=500,
            detail=f"ledger failed to load ({len(errors)} error(s)); see /verify",
        )
    try:
        report = trends_report(from_date, to, entries=entries, errors=errors, options=options)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return TrendsReportResponse(
        ok=report.ok,
        summary=report.render(),
        from_date=report.from_date,
        to_date=report.to_date,
        currency=report.currency,
        periods=list(report.periods),
        envelopes=[
            EnvelopeTrendModel(
                name=t.name,
                direction=t.direction,
                periods_observed=t.periods_observed,
                periods_required=t.periods_required,
                total=t.total,
                mean=t.mean,
                slope=t.slope,
                relative_slope=t.relative_slope,
                reason=t.reason,
                points=[SpendPointModel(period=p.period, amount=p.amount) for p in t.points],
            )
            for t in report.envelopes
        ],
        outliers=[OutlierModel(**asdict(o)) for o in report.outliers],
        assessments=[OutlierAssessmentModel(**asdict(a)) for a in report.assessments],
        unmapped_total=report.unmapped_total,
        unmapped_accounts=list(report.unmapped_accounts),
        unmapped_transactions=report.unmapped_transactions,
        min_periods=report.min_periods,
        flat_band=report.flat_band,
        min_transactions=report.min_transactions,
        outlier_threshold=report.outlier_threshold,
        errors=list(report.errors),
        warnings=list(report.warnings),
    )


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn

    uvicorn.run(app, host=host, port=port)
