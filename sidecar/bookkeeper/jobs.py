"""In-process background jobs, for work that must not happen inside a request.

PLAN.md §5.3 rule 3: categorizing 43 transactions at ~1-2s each would stall a
chat turn for a minute, so sync and categorization run *outside* the turn and
the UI polls for progress. This module is what "outside the turn" means here.

An in-process registry over daemon threads is the right size for this system.
Bookkeeper is a single-user localhost app whose whole point is that the data
never leaves the machine; a broker, a worker pool, and a serialization format
would be more moving parts than the thing they schedule. What that choice
*does* oblige us to get right is everything a queue would otherwise have
handled for free:

- **Jobs outlive the request that started them.** The worker runs on its own
  thread, so the `POST` returns as soon as the job is registered.
- **Shared state is locked.** A job is written by its worker thread and read by
  every polling request; every read and write of a record goes through one
  lock, and callers only ever see an immutable `JobSnapshot` so they cannot
  observe a half-updated record.
- **A failure is a result, not a disappearance.** An exception in the worker
  becomes `state=failed` with the exception's text attached. A job that vanished
  on error would leave the UI polling forever for something that will never
  finish.
- **History is bounded.** Finished jobs are kept so a poll that arrives after
  completion still gets an answer, but only the most recent
  `DEFAULT_HISTORY` of them -- an in-process registry with unbounded history is
  a memory leak with a long fuse. Unfinished jobs are never evicted.
- **One job per kind at a time.** SimpleFIN allows on the order of 24 requests
  per day (§3.1), so a double-fired sync is not a wasted goroutine, it is a
  meaningful fraction of the daily budget. Starting a sync while one is already
  running returns the running job instead of launching a second.

Daemon threads, specifically: a sync stuck on a slow bridge must not keep
uvicorn from shutting down. The ledger writes it performs are individually
atomic (`ingest.sync` only opens a file when it has something new to add), so
losing a half-finished sync at shutdown costs a rerun, not consistency.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: How many *finished* jobs to remember. Enough that a UI polling at a human
#: interval never asks about a job that has already been forgotten, small
#: enough that the registry's memory is a rounding error.
DEFAULT_HISTORY = 32


class JobState(str, Enum):
    """Where a job is. `str`-valued so it serializes as itself over HTTP."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: The states from which a job will never move again.
TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.SUCCEEDED, JobState.FAILED})


@dataclass(frozen=True)
class JobSnapshot:
    """One consistent reading of a job.

    Frozen, and the only type that leaves the registry: a caller holding a
    live record would be reading fields the worker thread is concurrently
    writing, and could report `state=succeeded` alongside a `result` that had
    not been stored yet.
    """

    job_id: str
    kind: str
    state: JobState
    progress: int
    total: int
    step: str
    result: dict[str, Any] | None
    error: str | None
    started_at: float
    finished_at: float | None

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "state": self.state.value,
            "progress": self.progress,
            "total": self.total,
            "step": self.step,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    def render(self) -> str:
        lines = [f"job {self.job_id} ({self.kind}): {self.state.value}"]
        if self.total:
            lines.append(f"  step {self.progress}/{self.total}: {self.step}")
        elif self.step:
            lines.append(f"  {self.step}")
        if self.error:
            lines.append(f"  error: {self.error}")
        return "\n".join(lines)


@dataclass
class _JobRecord:
    """The mutable half. Never handed out; only ever read under the lock."""

    job_id: str
    kind: str
    total: int
    state: JobState = JobState.PENDING
    progress: int = 0
    step: str = ""
    result: dict[str, Any] | None = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    finished: threading.Event = field(default_factory=threading.Event)


def _snapshot(record: _JobRecord) -> JobSnapshot:
    return JobSnapshot(
        job_id=record.job_id,
        kind=record.kind,
        state=record.state,
        progress=record.progress,
        total=record.total,
        step=record.step,
        result=record.result,
        error=record.error,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


class JobProgress:
    """The handle a worker uses to say where it has got to.

    Deliberately narrow: a worker can move the progress counter and name the
    step it is on, and can do nothing else to its own record. It cannot mark
    itself succeeded or failed -- that is decided by whether it returns or
    raises, so a worker cannot report success and then crash.
    """

    def __init__(self, registry: JobRegistry, job_id: str) -> None:
        self._registry = registry
        self._job_id = job_id

    def report(
        self,
        *,
        step: str | None = None,
        progress: int | None = None,
        total: int | None = None,
    ) -> None:
        self._registry.update(self._job_id, step=step, progress=progress, total=total)


#: A unit of background work: it is told how to report progress and returns
#: the JSON-safe payload that `GET /sync/status/{job_id}` will hand back.
JobWork = Callable[[JobProgress], "dict[str, Any] | None"]


def _spawn_thread(run: Callable[[], None]) -> None:
    threading.Thread(target=run, daemon=True).start()


class JobRegistry:
    """A bounded, thread-safe registry of background jobs.

    `spawn` is injectable so tests can run a job inline and assert on its
    outcome without sleeping on a thread; production uses a daemon thread.
    """

    def __init__(
        self,
        history: int = DEFAULT_HISTORY,
        spawn: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._records: OrderedDict[str, _JobRecord] = OrderedDict()
        self._history = max(1, history)
        self._spawn = spawn or _spawn_thread

    # -- starting ---------------------------------------------------------

    def start(self, kind: str, work: JobWork, *, total: int = 1) -> tuple[JobSnapshot, bool]:
        """Register and launch `work`, or hand back the `kind` already running.

        Returns `(snapshot, started)`. `started` is False when an unfinished
        job of the same kind already existed -- the caller gets that job's id
        instead, which is what keeps a double-clicked *Sync* from spending two
        of SimpleFIN's ~24 daily requests (§3.1).

        The job is registered in `PENDING` *before* the lock is released, so
        the single-flight check above sees it even if the worker thread has
        not been scheduled yet.
        """
        with self._lock:
            existing = self._active_locked(kind)
            if existing is not None:
                return _snapshot(existing), False
            record = _JobRecord(job_id=uuid.uuid4().hex, kind=kind, total=max(0, total))
            self._records[record.job_id] = record
            self._evict_locked()
            snapshot = _snapshot(record)

        # Outside the lock: spawning must not block a concurrent poll, and a
        # `spawn` that runs inline (tests) would otherwise deadlock on it.
        self._spawn(lambda: self._run(record, work))
        return snapshot, True

    def _run(self, record: _JobRecord, work: JobWork) -> None:
        with self._lock:
            record.state = JobState.RUNNING
        try:
            result = work(JobProgress(self, record.job_id))
        except Exception as exc:  # noqa: BLE001 - the failure is the job's result
            # Every failure mode ends up here, including ones we did not
            # anticipate, because the alternative is a job that stays
            # `running` forever and a UI that polls it forever.
            self._finish(record, JobState.FAILED, error=f"{type(exc).__name__}: {exc}")
            return
        self._finish(record, JobState.SUCCEEDED, result=result)

    def _finish(
        self,
        record: _JobRecord,
        state: JobState,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._lock:
            record.state = state
            record.result = result
            record.error = error
            record.finished_at = time.time()
            if state is JobState.SUCCEEDED:
                # A succeeded job is complete by definition; a worker that
                # forgot its last `report()` must not leave the UI showing a
                # finished job stuck at 1/2.
                record.progress = record.total
        record.finished.set()

    # -- reading ----------------------------------------------------------

    def update(
        self,
        job_id: str,
        *,
        step: str | None = None,
        progress: int | None = None,
        total: int | None = None,
    ) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            if step is not None:
                record.step = step
            if progress is not None:
                record.progress = progress
            if total is not None:
                record.total = total

    def get(self, job_id: str) -> JobSnapshot | None:
        with self._lock:
            record = self._records.get(job_id)
            return None if record is None else _snapshot(record)

    def active(self, kind: str) -> JobSnapshot | None:
        """The unfinished job of `kind`, if there is one."""
        with self._lock:
            record = self._active_locked(kind)
            return None if record is None else _snapshot(record)

    def snapshots(self) -> tuple[JobSnapshot, ...]:
        """Every remembered job, oldest first."""
        with self._lock:
            return tuple(_snapshot(r) for r in self._records.values())

    def wait(self, job_id: str, timeout: float | None = None) -> JobSnapshot | None:
        """Block until `job_id` finishes. For tests and headless CLI runs.

        Never called from a request handler -- the whole point of the registry
        is that requests do not wait.
        """
        with self._lock:
            record = self._records.get(job_id)
        if record is None:
            return None
        record.finished.wait(timeout)
        return self.get(job_id)

    # -- bookkeeping ------------------------------------------------------

    def _active_locked(self, kind: str) -> _JobRecord | None:
        for record in reversed(self._records.values()):
            if record.kind == kind and record.state not in TERMINAL_STATES:
                return record
        return None

    def _evict_locked(self) -> None:
        """Forget the oldest finished jobs beyond `history`.

        Only finished ones: evicting a running job would lose the very record
        its poller is about to ask for, and there is no upper bound on how
        many jobs a caller can have in flight that we would rather enforce by
        silently dropping one.
        """
        finished = [r.job_id for r in self._records.values() if r.state in TERMINAL_STATES]
        for job_id in finished[: max(0, len(finished) - self._history)]:
            del self._records[job_id]


#: The registry the sidecar uses. Module-level because the whole design is
#: that a job outlives the request that started it and is polled by later
#: ones; a per-request registry would forget the job immediately.
registry = JobRegistry()
