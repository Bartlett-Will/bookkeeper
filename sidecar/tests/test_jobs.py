"""Unit tests for the background job registry.

`jobs.py` is an in-process registry over daemon threads, chosen because a
broker and a worker pool would be more moving parts than the thing they
schedule. What that choice *obliges* is everything a queue would have handled
for free -- shared state under a lock, failures that surface as results,
bounded history, one job per kind -- and those obligations are what this file
tests. They are all concurrency properties, so several tests here run real
threads rather than asserting on the shape of the code.

The `spawn` seam is what makes that tractable: `JobRegistry(spawn=...)` lets a
test run a job inline and assert on its outcome without sleeping, or decline
to run it at all so a job can be pinned in an unfinished state. Where the
property under test *is* threading (`wait`, concurrent `start`), the real
daemon-thread spawn is used instead.
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

import bookkeeper.jobs as jobs_module
from bookkeeper.jobs import DEFAULT_HISTORY, TERMINAL_STATES, JobRegistry, JobState


def _inline() -> JobRegistry:
    """A registry whose jobs run on the calling thread, so `start` returns
    only once the work has finished and a test can assert without waiting."""
    return JobRegistry(spawn=lambda run: run())


def _never() -> JobRegistry:
    """A registry that registers jobs but never runs them, pinning them in
    `pending`. For the properties that are about *unfinished* jobs."""
    return JobRegistry(spawn=lambda run: None)


def _ok(_progress):
    return {"fetched": 1}


# --- one job per kind -----------------------------------------------------


def test_a_second_start_of_a_running_kind_joins_the_existing_job():
    """SimpleFIN allows on the order of 24 requests a day (§3.1).

    A double-fired sync is therefore not a wasted thread, it is a meaningful
    fraction of the daily budget -- so the second caller is handed the
    running job rather than launching a second one.
    """
    registry = _never()
    first, started_first = registry.start("sync", _ok)
    second, started_second = registry.start("sync", _ok)

    assert started_first is True
    assert started_second is False
    assert second.job_id == first.job_id


def test_different_kinds_do_not_block_each_other():
    registry = _never()
    sync, _ = registry.start("sync", _ok)
    categorize, started = registry.start("categorize", _ok)

    assert started is True
    assert categorize.job_id != sync.job_id


def test_a_finished_kind_can_be_started_again():
    """The guard is on *unfinished* jobs. A sync that has completed must not
    block the next one, or the app would sync exactly once per process."""
    registry = _inline()
    first, _ = registry.start("sync", _ok)
    second, started = registry.start("sync", _ok)

    assert started is True
    assert second.job_id != first.job_id


def test_the_job_is_registered_before_the_worker_is_scheduled():
    """The single-flight check must see a job whose thread has not run yet.

    `start` registers in `pending` while still holding the lock, precisely so
    a second caller arriving before the scheduler gets to the worker still
    collides with it.
    """
    registry = _never()
    snapshot, _ = registry.start("sync", _ok)

    assert snapshot.state is JobState.PENDING
    assert registry.get(snapshot.job_id) is not None
    assert registry.active("sync") is not None


def test_concurrent_starts_of_one_kind_produce_exactly_one_job():
    """The guard under real contention, not just sequential calls.

    Sequential calls would pass even if the check happened outside the lock.
    Twenty threads released simultaneously is what actually exercises it, and
    a lost race here spends real requests against a bank's rate limit.
    """
    registry = _never()
    threads_count = 20
    barrier = threading.Barrier(threads_count)
    results: list[tuple[str, bool]] = []
    guard = threading.Lock()

    def race():
        barrier.wait(timeout=5)
        snapshot, started = registry.start("sync", _ok)
        with guard:
            results.append((snapshot.job_id, started))

    threads = [threading.Thread(target=race) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == threads_count
    assert sum(1 for _id, started in results if started) == 1, results
    assert len({job_id for job_id, _started in results}) == 1


# --- a failure is a result, not a disappearance ---------------------------


def test_a_raising_worker_becomes_a_failed_job_with_its_reason():
    """A job that vanished on error would leave the UI polling forever for
    something that will never finish."""
    registry = _inline()

    def boom(_progress):
        raise ValueError("no Access URL on file")

    snapshot, _ = registry.start("sync", boom)
    finished = registry.get(snapshot.job_id)

    assert finished is not None
    assert finished.state is JobState.FAILED
    assert finished.done is True
    assert finished.error == "ValueError: no Access URL on file"
    assert finished.result is None


def test_an_unanticipated_exception_type_is_still_caught():
    """Every failure mode ends up as a failed job, including ones nobody
    thought of -- the alternative is a job stuck `running` forever.

    A bare `except Exception` is usually a smell; here it is the point, so it
    is pinned against an exception type this module has never heard of.
    """
    registry = _inline()

    class Surprising(Exception):
        pass

    def boom(_progress):
        raise Surprising("nobody planned for this")

    snapshot, _ = registry.start("sync", boom)
    finished = registry.get(snapshot.job_id)

    assert finished is not None
    assert finished.state is JobState.FAILED
    assert finished.error == "Surprising: nobody planned for this"


def test_a_worker_cannot_report_success_and_then_crash():
    """`JobProgress` can move the counter and name the step, and nothing else.

    Success is decided by whether the worker returns or raises, never by the
    worker asserting it -- so progress reported right up to the end does not
    make a crashed job look finished.
    """
    registry = _inline()

    def almost(progress):
        progress.report(step="nearly there", progress=99, total=100)
        raise RuntimeError("fell over at the last step")

    snapshot, _ = registry.start("sync", almost, total=100)
    finished = registry.get(snapshot.job_id)

    assert finished is not None
    assert finished.state is JobState.FAILED
    assert finished.progress == 99


def test_the_progress_handle_exposes_nothing_but_report():
    """Deliberately narrow. If a worker could reach the registry's mutators
    through its handle, the guarantee above would only be a convention."""
    registry = _inline()
    captured: list = []

    def work(progress):
        captured.append(progress)

    registry.start("sync", work)

    public = sorted(name for name in dir(captured[0]) if not name.startswith("_"))
    assert public == ["report"]


# --- results and progress -------------------------------------------------


def test_a_successful_worker_stores_its_payload():
    registry = _inline()
    snapshot, _ = registry.start("sync", lambda _p: {"transactions_added": 40})

    finished = registry.get(snapshot.job_id)
    assert finished is not None
    assert finished.state is JobState.SUCCEEDED
    assert finished.result == {"transactions_added": 40}
    assert finished.error is None


def test_a_worker_that_returns_nothing_still_succeeds():
    registry = _inline()
    snapshot, _ = registry.start("sync", lambda _p: None)

    finished = registry.get(snapshot.job_id)
    assert finished is not None
    assert finished.state is JobState.SUCCEEDED
    assert finished.result is None


def test_success_completes_the_progress_counter():
    """A worker that forgot its last `report()` must not leave the UI showing
    a finished job stuck at 1 of 2."""
    registry = _inline()
    snapshot, _ = registry.start("sync", lambda _p: None, total=5)

    finished = registry.get(snapshot.job_id)
    assert finished is not None
    assert finished.progress == 5
    assert finished.total == 5


def test_progress_is_observable_while_the_job_runs():
    registry = _inline()
    observed = []

    def work(progress):
        progress.report(step="fetching from SimpleFIN", progress=1, total=3)
        observed.append(registry.snapshots()[-1])

    registry.start("sync", work, total=3)

    mid = observed[0]
    assert mid.state is JobState.RUNNING
    assert mid.step == "fetching from SimpleFIN"
    assert mid.progress == 1
    assert mid.total == 3
    assert mid.done is False


def test_a_worker_can_revise_the_total_it_was_started_with():
    registry = _inline()

    def work(progress):
        progress.report(total=43)

    snapshot, _ = registry.start("sync", work, total=1)
    finished = registry.get(snapshot.job_id)
    assert finished is not None
    assert finished.total == 43
    assert finished.progress == 43


def test_a_snapshot_is_a_frozen_reading_that_never_changes_underneath_a_caller():
    """The only type that leaves the registry is immutable.

    A caller holding a live record would be reading fields the worker thread
    is concurrently writing, and could report `succeeded` beside a `result`
    that had not been stored yet.
    """
    registry = _inline()
    observed = []

    def work(progress):
        progress.report(step="halfway")
        observed.append(registry.snapshots()[-1])
        return {"done": True}

    snapshot, _ = registry.start("sync", work)

    mid = observed[0]
    assert mid.state is JobState.RUNNING
    assert mid.result is None
    # The job has since succeeded; the snapshot taken mid-flight has not moved.
    assert registry.get(snapshot.job_id).state is JobState.SUCCEEDED
    assert mid.state is JobState.RUNNING
    with pytest.raises(dataclasses.FrozenInstanceError):
        mid.state = JobState.FAILED


# --- bounded history ------------------------------------------------------


def test_finished_jobs_are_forgotten_oldest_first():
    """An in-process registry with unbounded history is a memory leak with a
    long fuse; one that forgets too eagerly strands a poller."""
    registry = JobRegistry(history=2, spawn=lambda run: run())
    ids = [registry.start("sync", _ok)[0].job_id for _ in range(6)]

    remembered = [s.job_id for s in registry.snapshots()]
    assert remembered == ids[-3:]
    assert registry.get(ids[0]) is None
    assert registry.get(ids[-1]) is not None


def test_an_unfinished_job_is_never_evicted():
    """Evicting a running job would lose the very record its poller is about
    to ask for."""
    should_run = [False]
    registry = JobRegistry(history=1, spawn=lambda run: run() if should_run[0] else None)

    stuck, _ = registry.start("stuck", _ok)
    should_run[0] = True
    for kind in ("a", "b", "c", "d"):
        registry.start(kind, _ok)

    assert registry.get(stuck.job_id) is not None
    assert registry.get(stuck.job_id).state is JobState.PENDING


def test_history_is_never_zero():
    """`history=0` would evict a job the instant it finished, so the poll that
    follows a completed sync would 404."""
    registry = JobRegistry(history=0, spawn=lambda run: run())
    snapshot, _ = registry.start("sync", _ok)
    assert registry.get(snapshot.job_id) is not None


def test_the_shipped_registry_is_module_level_with_a_bounded_history():
    """Module-level because a job outlives the request that started it and is
    polled by later ones; a per-request registry would forget it at once."""
    assert isinstance(jobs_module.registry, JobRegistry)
    assert DEFAULT_HISTORY == 32


# --- lookups --------------------------------------------------------------


def test_get_and_wait_answer_none_for_an_unknown_job():
    registry = _inline()
    assert registry.get("nope") is None
    assert registry.wait("nope", timeout=0.1) is None


def test_updating_an_unknown_job_is_a_no_op_not_an_error():
    """A late `report()` from a worker whose record has been evicted must not
    take the worker down with it."""
    registry = _inline()
    registry.update("nope", step="x", progress=1, total=2)


def test_active_reports_only_unfinished_jobs():
    should_run = [False]
    registry = JobRegistry(spawn=lambda run: run() if should_run[0] else None)

    pending, _ = registry.start("sync", _ok)
    assert registry.active("sync").job_id == pending.job_id
    assert registry.active("categorize") is None

    should_run[0] = True
    registry.start("categorize", _ok)
    assert registry.active("categorize") is None


def test_wait_blocks_on_a_real_thread_until_the_job_finishes():
    """The default spawn is a real daemon thread, and `wait` is what the
    headless CLI path uses. Exercised against the real thing rather than the
    inline seam, since threading is the property under test."""
    registry = JobRegistry()
    release = threading.Event()

    def work(progress):
        progress.report(step="waiting")
        assert release.wait(timeout=5)
        return {"finished": True}

    snapshot, started = registry.start("sync", work)
    assert started is True

    assert registry.get(snapshot.job_id).done is False
    release.set()

    finished = registry.wait(snapshot.job_id, timeout=5)
    assert finished is not None
    assert finished.state is JobState.SUCCEEDED
    assert finished.result == {"finished": True}


def test_wait_on_an_already_finished_job_returns_immediately():
    registry = _inline()
    snapshot, _ = registry.start("sync", _ok)
    assert registry.wait(snapshot.job_id, timeout=5).state is JobState.SUCCEEDED


def test_snapshots_are_ordered_oldest_first():
    registry = _inline()
    ids = [registry.start(f"kind-{n}", _ok)[0].job_id for n in range(3)]
    assert [s.job_id for s in registry.snapshots()] == ids


# --- serialization --------------------------------------------------------


def test_job_state_serializes_as_its_own_string():
    """`str`-valued so it crosses HTTP as itself rather than as an enum repr."""
    assert JobState.RUNNING == "running"
    assert JobState.SUCCEEDED.value == "succeeded"
    assert TERMINAL_STATES == frozenset({JobState.SUCCEEDED, JobState.FAILED})
    assert JobState.PENDING not in TERMINAL_STATES
    assert JobState.RUNNING not in TERMINAL_STATES


def test_to_dict_carries_every_field_a_poller_needs():
    registry = _inline()
    snapshot, _ = registry.start("sync", lambda _p: {"added": 2}, total=2)

    payload = registry.get(snapshot.job_id).to_dict()
    assert set(payload) == {
        "job_id",
        "kind",
        "state",
        "progress",
        "total",
        "step",
        "result",
        "error",
        "started_at",
        "finished_at",
    }
    assert payload["state"] == "succeeded"
    assert payload["result"] == {"added": 2}
    assert payload["finished_at"] >= payload["started_at"]


def test_render_describes_progress_and_failure():
    registry = _inline()

    def work(progress):
        progress.report(step="fetching", progress=1, total=2)
        raise RuntimeError("bridge unreachable")

    snapshot, _ = registry.start("sync", work, total=2)
    text = registry.get(snapshot.job_id).render()

    assert "(sync): failed" in text
    assert "step 1/2: fetching" in text
    assert "error: RuntimeError: bridge unreachable" in text


def test_render_omits_the_counter_for_an_untotalled_job():
    registry = _inline()

    def work(progress):
        progress.report(step="working")

    snapshot, _ = registry.start("sync", work, total=0)
    text = registry.get(snapshot.job_id).render()

    assert "step" not in text
    assert "working" in text
