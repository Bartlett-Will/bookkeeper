"""Paginated backfill: window arithmetic, the budget, and idempotency.

The properties under test are the ones that cost real money to get wrong.
SimpleFIN allows ~24 requests a day and caps a range at 90 days *without
failing* -- HTTP 200, truncated body, a soft error in `errors`. So a bug
here does not raise; it produces a ledger with holes in it and a run that
says it finished.

Every test drives a fake SimpleFIN through `pytest-httpx` that honours
`start-date` / `end-date` the way the real bridge does. Nothing here touches
a real server, and `bookkeeper_root` points every path at a temp tree.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from bookkeeper import paths
from bookkeeper.ingest.backfill import (
    DAILY_REQUEST_BUDGET,
    STATE_ALREADY_DONE,
    STATE_CAPPED,
    STATE_DONE,
    STATE_FAILED,
    STATE_PLANNED,
    STATE_SKIPPED,
    WINDOW_MAX_DAYS,
    plan_windows,
    requests_used_today,
    response_was_capped,
    run_backfill,
    state_path,
)
from bookkeeper.ingest.sync import _since_to_epoch, _until_to_epoch

ACCESS_URL = "https://demo:demopass@bridge.example.com/simplefin"
ACCOUNTS_URL = ACCESS_URL + "/accounts"

#: The demo password above, on its own. Asserted absent from everything the
#: backfill writes or renders -- an Access URL is banking-credential grade.
CREDENTIAL = "demopass"

CAP_ERROR = "Requested date range exceeds limit of 90 days and was capped."

ACCOUNTS = (
    ("ACT-1", "Checking", Decimal("1234.56")),
    ("ACT-2", "Savings", Decimal("9000.00")),
)


def _seed_access_url() -> None:
    dest = paths.access_url_file()
    dest.write_text(ACCESS_URL, encoding="utf-8")
    os.chmod(dest, 0o600)


def _midday(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=UTC).timestamp())


class FakeBridge:
    """A SimpleFIN that honours the window it is given, like the real one.

    One transaction per account per day means every window boundary lands on
    a day that has data, which is what makes the deliberate one-day overlap
    between consecutive windows worth testing rather than incidental.
    """

    def __init__(
        self,
        history_from: date,
        history_to: date,
        *,
        cap_on_request: int | None = None,
        fail_on_request: int | None = None,
    ) -> None:
        self.history_from = history_from
        self.history_to = history_to
        self.cap_on_request = cap_on_request
        self.fail_on_request = fail_on_request
        #: Every (start-date, end-date) pair actually requested, in order.
        self.requests: list[tuple[int | None, int | None]] = []

    def install(self, httpx_mock) -> None:
        httpx_mock.add_callback(self._handle, method="GET", is_reusable=True)

    def _days(self, start: int | None, end: int | None) -> list[date]:
        days = []
        day = self.history_from
        while day <= self.history_to:
            posted = _midday(day)
            if (start is None or posted >= start) and (end is None or posted <= end):
                days.append(day)
            day += timedelta(days=1)
        return days

    def _handle(self, request):
        import httpx

        params = request.url.params
        start = int(params["start-date"]) if "start-date" in params else None
        end = int(params["end-date"]) if "end-date" in params else None
        self.requests.append((start, end))
        n = len(self.requests)

        if n == self.fail_on_request:
            return httpx.Response(500, json={"errors": ["upstream exploded"]})

        days = self._days(start, end)
        errors = []
        if n == self.cap_on_request:
            # The shape that matters: HTTP 200, real data attached, and the
            # only sign of truncation is a string in `errors`.
            days = days[: len(days) // 2]
            errors.append(CAP_ERROR)

        return httpx.Response(
            200,
            json={
                "errlist": [],
                "errors": errors,
                "accounts": [
                    {
                        "id": account_id,
                        "name": name,
                        "currency": "USD",
                        "balance": str(balance),
                        "balance-date": _midday(self.history_to),
                        "transactions": [
                            {
                                "id": f"{account_id}-{day.isoformat()}",
                                "posted": _midday(day),
                                "amount": "-10.00",
                                "description": f"COFFEE {day.isoformat()}",
                            }
                            for day in days
                        ],
                    }
                    for account_id, name, balance in ACCOUNTS
                ],
            },
        )


def _hash_ledger_tree() -> dict[str, str]:
    hashes: dict[str, str] = {}
    for f in sorted(paths.transactions_dir().glob("*.beancount")):
        hashes[f"transactions/{f.name}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    for path in (paths.balances_ledger(), paths.accounts_simplefin_ledger()):
        if path.exists():
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


_POSTING_RE = re.compile(r"^ {2}(Assets:\S+)\s+(-?\d+(?:\.\d+)?) [A-Z]{3}\s*$", re.MULTILINE)


def _posting_totals() -> dict[str, Decimal]:
    """Every `Assets:` posting in the ledger, summed per account.

    Deliberately re-derived here rather than borrowing the module's own
    scanner: the property is "the ledger adds up to the asserted balance",
    and checking it with the code that produced it would prove nothing.
    """
    totals: dict[str, Decimal] = {}
    for f in sorted(paths.transactions_dir().glob("*.beancount")):
        for account, amount in _POSTING_RE.findall(f.read_text(encoding="utf-8")):
            totals[account] = totals.get(account, Decimal(0)) + Decimal(amount)
    return totals


# --- window arithmetic ----------------------------------------------------


def test_a_year_is_five_windows_that_cover_it_exactly():
    windows = plan_windows(date(2025, 8, 9), date(2026, 8, 9))

    assert len(windows) == 5
    assert windows[0].start == date(2025, 8, 9)
    assert windows[-1].end == date(2026, 8, 9)
    assert [w.index for w in windows] == [1, 2, 3, 4, 5]
    assert all(w.days <= WINDOW_MAX_DAYS for w in windows)


def test_consecutive_windows_share_their_boundary_day():
    """Overlap, never a gap.

    A bank's posted timestamps are not aligned to UTC midnight, so abutting
    windows could drop a transaction into the crack between them and nothing
    would say so. An overlap costs one dedup hit; a gap costs a transaction.
    """
    windows = plan_windows(date(2025, 1, 1), date(2025, 12, 31))

    for earlier, later in itertools.pairwise(windows):
        assert later.start == earlier.end


def test_each_window_stays_inside_the_servers_ninety_day_limit():
    """The arithmetic that keeps a window from being capped, in seconds.

    90 calendar days inclusive is 89 days plus 86,399 seconds once the
    window closes at the last second of its end date -- one second inside
    the limit. This is the assertion that would fail if `_until_to_epoch`
    ever became a plain midnight.
    """
    limit_seconds = WINDOW_MAX_DAYS * 86_400

    for window in plan_windows(date(2024, 1, 1), date(2026, 8, 9)):
        start = _since_to_epoch(window.start.isoformat())
        end = _until_to_epoch(window.end.isoformat())
        assert end - start < limit_seconds
        assert end > start


def test_a_single_day_range_is_a_single_window():
    windows = plan_windows(date(2026, 1, 1), date(2026, 1, 1))
    assert len(windows) == 1
    assert windows[0].days == 1


def test_a_reversed_range_is_refused_rather_than_silently_emptied():
    with pytest.raises(ValueError, match="precedes"):
        plan_windows(date(2026, 2, 1), date(2026, 1, 1))


def test_run_backfill_refuses_a_reversed_range(bookkeeper_root):
    result = run_backfill("2026-02-01", "2026-01-01")
    assert result.ok is False
    assert "precedes" in result.render()


def test_run_backfill_refuses_an_unparseable_date(bookkeeper_root):
    result = run_backfill("last tuesday")
    assert result.ok is False
    assert "bad --from date" in result.render()


# --- the silent cap -------------------------------------------------------


def test_the_cap_is_detected_from_the_soft_error_not_the_status_code():
    assert response_was_capped([CAP_ERROR]) is True
    assert response_was_capped(["CONNECTION_ERROR: bank unreachable"]) is False
    assert response_was_capped([]) is False


def test_a_capped_window_stops_the_run_and_is_not_counted_as_coverage(
    bookkeeper_root, httpx_mock
):
    """HTTP 200 is not success.

    The second window comes back capped: real transactions attached, half of
    them missing, and only a string in `errors` to say so. Coverage must
    stop at the end of window one, and the run must not spend the rest of
    the day's budget on windows built by the same arithmetic.
    """
    _seed_access_url()
    bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9), cap_on_request=2)
    bridge.install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert result.ok is False
    assert result.stopped_reason == "capped"
    assert len(bridge.requests) == 2, "kept spending budget after a capped window"
    states = [w.state for w in result.windows]
    assert states == [STATE_DONE, STATE_CAPPED, STATE_SKIPPED, STATE_SKIPPED, STATE_SKIPPED]

    # Coverage stops at the last *clean* window, not the last window that
    # returned data.
    assert result.honoured_through == result.windows[0].end
    assert result.complete is False
    assert any("capped" in e for e in result.errors)


def test_a_capped_window_is_retried_by_the_next_run(bookkeeper_root, httpx_mock):
    """A window whose data is on disk but incomplete is not "done".

    Its transactions were written -- they are real -- but the coverage claim
    was never earned, so resuming must request it again rather than skip it.
    """
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9), cap_on_request=2).install(httpx_mock)
    run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    preview = run_backfill("2025-08-09", "2026-08-09")
    assert [w.state for w in preview.windows] == [
        STATE_ALREADY_DONE,
        STATE_PLANNED,
        STATE_PLANNED,
        STATE_PLANNED,
        STATE_PLANNED,
    ]
    assert preview.requests_planned == 4


# --- the request budget ---------------------------------------------------


def test_a_dry_run_lists_the_windows_and_sends_nothing(bookkeeper_root, httpx_mock):
    """The required property, asserted the only way that proves it.

    No response is registered with `httpx_mock`, so any HTTP request at all
    raises. A dry run that passes this test cannot have made one.
    """
    _seed_access_url()

    result = run_backfill("2025-08-09", "2026-08-09")

    assert result.ok is True
    assert result.dry_run is True
    assert result.requests_made == 0
    assert result.requests_planned == 5
    assert [w.state for w in result.windows] == [STATE_PLANNED] * 5
    assert not state_path(date(2025, 8, 9), date(2026, 8, 9)).exists(), (
        "a preview wrote state; it must spend and touch nothing"
    )
    rendered = result.render()
    assert "dry run" in rendered
    assert "2025-08-09 -> 2025-11-06" in rendered


def test_a_dry_run_that_does_not_fit_todays_budget_says_so(bookkeeper_root):
    """A plan you cannot afford is an answer, and it is `ok: false`.

    Cron reads the exit code; "you have three requests left and need five"
    must not look like success.
    """
    _seed_access_url()
    _spend_budget(DAILY_REQUEST_BUDGET - 3)

    result = run_backfill("2025-08-09", "2026-08-09")

    assert result.ok is False
    assert result.requests_remaining_today == 3
    assert result.requests_planned == 5
    assert "--max-requests" in result.render()


def _spend_budget(count: int) -> None:
    """Fake `count` responses already archived today, at distinct seconds."""
    raw = paths.raw_dir()
    raw.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()
    for i in range(count):
        (raw / f"simplefin-{today}T{i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}Z.json").write_text(
            "{}", encoding="utf-8"
        )


def test_requests_used_today_counts_the_raw_archive_and_ignores_other_days(bookkeeper_root):
    """The request ledger is the archive `fetch.py` already writes.

    A counter we maintained separately could disagree with what was actually
    sent; this cannot.
    """
    raw = paths.raw_dir()
    raw.mkdir(parents=True, exist_ok=True)
    (raw / "simplefin-2026-08-09T10:00:00Z.json").write_text("{}", encoding="utf-8")
    (raw / "simplefin-2026-08-09T11:00:00Z.json").write_text("{}", encoding="utf-8")
    (raw / "simplefin-2026-08-08T10:00:00Z.json").write_text("{}", encoding="utf-8")

    assert requests_used_today(date(2026, 8, 9)) == 2
    assert requests_used_today(date(2026, 8, 8)) == 1
    assert requests_used_today(date(2026, 8, 7)) == 0


def test_a_run_capped_by_max_requests_reports_what_it_finished(bookkeeper_root, httpx_mock):
    """Budget exhausted mid-run: stop, say where, stay resumable.

    A partial backfill nobody can characterise is the failure mode worth
    avoiding -- so the windows that did land are named, the ones that did
    not are `skipped`, and coverage stops where the requests stopped.
    """
    _seed_access_url()
    bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9))
    bridge.install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False, max_requests=2)

    assert len(bridge.requests) == 2
    assert result.ok is False
    assert result.stopped_reason == "budget"
    assert result.requests_made == 2
    assert [w.state for w in result.windows] == [
        STATE_DONE,
        STATE_DONE,
        STATE_SKIPPED,
        STATE_SKIPPED,
        STATE_SKIPPED,
    ]
    assert result.honoured_through == result.windows[1].end
    assert result.complete is False
    assert "resume" in result.render()


def test_no_budget_left_means_no_request_is_sent(bookkeeper_root, httpx_mock):
    """Nothing is registered on the mock: sending anything would raise."""
    _seed_access_url()
    _spend_budget(DAILY_REQUEST_BUDGET)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert result.ok is False
    assert result.requests_made == 0
    assert result.stopped_reason == "budget"
    assert result.requests_remaining_today == 0


# --- resumability ---------------------------------------------------------


def test_a_run_resumes_at_the_window_it_died_on(bookkeeper_root, httpx_mock):
    """Windows one and two are already paid for; re-fetching them is money."""
    _seed_access_url()
    first_bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9))
    first_bridge.install(httpx_mock)
    first = run_backfill("2025-08-09", "2026-08-09", dry_run=False, max_requests=2)
    assert first.requests_made == 2

    httpx_mock.reset()
    second_bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9))
    second_bridge.install(httpx_mock)
    second = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert second.requests_made == 3, "re-fetched windows an earlier run had paid for"
    assert [w.state for w in second.windows] == [
        STATE_ALREADY_DONE,
        STATE_ALREADY_DONE,
        STATE_DONE,
        STATE_DONE,
        STATE_DONE,
    ]
    assert second.ok is True
    assert second.complete is True
    assert second.honoured_through == date(2026, 8, 9)

    # And the resumed run asked for exactly the windows it had not paid for.
    assert [_since_to_epoch(w.start.isoformat()) for w in second.windows[2:]] == [
        start for start, _end in second_bridge.requests
    ]


def test_progress_is_readable_on_disk_rather_than_implicit(bookkeeper_root, httpx_mock):
    """`cat` is the intended UI for "where did it get to"."""
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9)).install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False, max_requests=2)

    path = state_path(date(2025, 8, 9), date(2026, 8, 9))
    assert result.state_path == str(path)
    state = json.loads(path.read_text(encoding="utf-8"))

    assert state["requested_from"] == "2025-08-09"
    assert state["requested_to"] == "2026-08-09"
    assert state["window_days"] == WINDOW_MAX_DAYS
    # All five windows, not just the ones reached: "two of two" and "two of
    # five" must not look the same to a reader.
    assert len(state["windows"]) == 5
    assert [w["state"] for w in state["windows"]] == [
        STATE_DONE,
        STATE_DONE,
        STATE_SKIPPED,
        STATE_SKIPPED,
        STATE_SKIPPED,
    ]


def test_state_for_one_range_does_not_resume_a_different_range(bookkeeper_root, httpx_mock):
    _seed_access_url()
    FakeBridge(date(2024, 1, 1), date(2026, 8, 9)).install(httpx_mock)
    run_backfill("2025-08-09", "2026-08-09", dry_run=False, max_requests=1)

    other = run_backfill("2024-01-01", "2024-06-30")
    assert [w.state for w in other.windows] == [STATE_PLANNED, STATE_PLANNED, STATE_PLANNED]


def test_restart_refetches_windows_a_previous_run_completed(bookkeeper_root, httpx_mock):
    _seed_access_url()
    bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9))
    bridge.install(httpx_mock)
    run_backfill("2025-08-09", "2026-08-09", dry_run=False, max_requests=2)

    run_backfill("2025-08-09", "2026-08-09", dry_run=False, restart=True, max_requests=2)
    assert len(bridge.requests) == 4


def test_a_failed_window_stops_the_run_and_is_recorded_as_failed(bookkeeper_root, httpx_mock):
    _seed_access_url()
    bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9), fail_on_request=3)
    bridge.install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert result.ok is False
    assert result.stopped_reason == "failed"
    assert [w.state for w in result.windows][:3] == [STATE_DONE, STATE_DONE, STATE_FAILED]
    assert len(bridge.requests) == 3
    assert result.honoured_through == result.windows[1].end


# --- idempotency across windows -------------------------------------------


def test_overlapping_window_boundaries_are_absorbed_by_dedup(bookkeeper_root, httpx_mock):
    """The boundary day is fetched twice on purpose; it is recorded once.

    Windows share their end/start day, and the fake bridge has a transaction
    on every day, so each of the four boundaries is served twice. `seen`
    therefore exceeds `added` by exactly two accounts x four boundaries.
    """
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9)).install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    days = (date(2026, 8, 9) - date(2025, 8, 9)).days + 1
    expected_added = days * len(ACCOUNTS)
    boundaries = len(result.windows) - 1

    assert result.transactions_added == expected_added
    assert result.transactions_seen == expected_added + boundaries * len(ACCOUNTS)

    ids = re.findall(r'simplefin-id: "([^"]+)"', _all_transactions_text())
    assert len(ids) == len(set(ids)) == expected_added


def _all_transactions_text() -> str:
    return "".join(
        f.read_text(encoding="utf-8") for f in sorted(paths.transactions_dir().glob("*.beancount"))
    )


def test_backfilling_the_same_range_twice_leaves_a_byte_identical_ledger(
    bookkeeper_root, httpx_mock
):
    """Phase 1's exit criterion, extended across pagination.

    `restart=True` on the second run so it really re-fetches all five
    windows -- resuming would prove only that the state file works, not that
    dedup absorbs a whole re-served year.
    """
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9)).install(httpx_mock)

    first = run_backfill("2025-08-09", "2026-08-09", dry_run=False)
    assert first.ok is True
    hashes_after_first = _hash_ledger_tree()
    assert hashes_after_first

    second = run_backfill("2025-08-09", "2026-08-09", dry_run=False, restart=True)

    assert second.ok is True
    assert second.requests_made == 5, "did not actually re-fetch, so this proves nothing"
    assert second.transactions_added == 0
    assert second.opening_balances_written == 0
    assert _hash_ledger_tree() == hashes_after_first, (
        "ledger changed between two identical backfills -- idempotency broken"
    )


def test_resuming_a_finished_backfill_spends_nothing(bookkeeper_root, httpx_mock):
    _seed_access_url()
    bridge = FakeBridge(date(2025, 8, 9), date(2026, 8, 9))
    bridge.install(httpx_mock)
    run_backfill("2025-08-09", "2026-08-09", dry_run=False)
    hashes = _hash_ledger_tree()

    again = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert len(bridge.requests) == 5
    assert again.requests_made == 0
    assert again.ok is True
    assert again.complete is True
    assert _hash_ledger_tree() == hashes


# --- the three date ranges ------------------------------------------------


def test_requested_honoured_and_data_ranges_are_reported_separately(
    bookkeeper_root, httpx_mock
):
    """Three facts, three fields, none inferred from another.

    The range asked for runs a full year. The server honours all of it. The
    accounts were only active for eight months of it. Collapsing any pair
    here is the confident wrong answer this reporting exists to prevent.
    """
    _seed_access_url()
    FakeBridge(date(2025, 3, 1), date(2025, 10, 31)).install(httpx_mock)

    result = run_backfill("2025-01-01", "2025-12-31", dry_run=False)

    assert (result.requested_from, result.requested_to) == (date(2025, 1, 1), date(2025, 12, 31))
    assert (result.honoured_from, result.honoured_through) == (
        date(2025, 1, 1),
        date(2025, 12, 31),
    )
    assert (result.data_from, result.data_through) == (date(2025, 3, 1), date(2025, 10, 31))
    assert result.complete is True

    rendered = result.render()
    assert "requested:  2025-01-01 -> 2025-12-31" in rendered
    assert "data spans: 2025-03-01 -> 2025-10-31" in rendered


def test_an_empty_range_reports_coverage_without_inventing_a_data_span(
    bookkeeper_root, httpx_mock
):
    """Covered and empty is not the same as never fetched."""
    _seed_access_url()
    FakeBridge(date(2026, 1, 1), date(2026, 1, 2)).install(httpx_mock)

    result = run_backfill("2024-01-01", "2024-03-01", dry_run=False)

    assert result.complete is True
    assert result.honoured_through == date(2024, 3, 1)
    assert result.data_from is None
    assert result.data_through is None
    assert "data spans: none" in result.render()


# --- opening balances -----------------------------------------------------


def test_opening_plugs_are_written_once_the_whole_range_is_covered(
    bookkeeper_root, httpx_mock
):
    """A plug is `balance - everything recorded`, so it has to be last.

    Written per window it would be right for window one and wrong by the sum
    of every later one, and the balance assertion would fail by exactly the
    history the backfill was run to fetch.
    """
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9)).install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert result.ok is True
    assert result.opening_balances_written == len(ACCOUNTS)

    # The property the plug exists for: every asset posting in the ledger
    # sums to the balance SimpleFIN asserted.
    totals = _posting_totals()
    for _account_id, name, balance in ACCOUNTS:
        assert totals[f"Assets:SimpleFIN:{name}"] == balance


def test_a_partial_backfill_writes_no_plug_and_says_why(bookkeeper_root, httpx_mock):
    """Failing loudly beats reconciling against a number that is wrong.

    With no plug the balance assertion fails and bean-check says so. With a
    plug computed from two windows of five it would pass in some places and
    be quietly wrong in others.
    """
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9)).install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False, max_requests=2)

    assert result.opening_balances_written == 0
    assert 'simplefin-opening: "true"' not in _all_transactions_text()
    assert any("not written until the whole range is covered" in w for w in result.warnings)


def test_a_plug_left_stale_by_an_earlier_sync_is_reported_by_name(bookkeeper_root, httpx_mock):
    """`sync` then `backfill` leaves a plug short by the backfilled history.

    Backfill will not rewrite an entry it did not write, so the least it can
    do is name the account and both figures -- which is a much better place
    to start than a bean-check failure reading "off by 1,240.55".
    """
    from bookkeeper.ingest.sync import run_sync

    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9)).install(httpx_mock)
    # A plain sync of the last month only: plugs written against a fraction
    # of the history.
    assert run_sync(since="2026-07-10").ok

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)

    assert result.opening_balances_written == 0
    stale = [w for w in result.warnings if "plug predates these transactions" in w]
    assert len(stale) == len(ACCOUNTS)
    assert any("Assets:SimpleFIN:Checking" in w for w in stale)


# --- credential hygiene ---------------------------------------------------


def test_nothing_backfill_writes_or_renders_contains_the_access_url(
    bookkeeper_root, httpx_mock
):
    """The Access URL is banking-credential grade (§7, High).

    Checked across the state file, the rendered output, the structured
    payload and the ledger -- every artefact this module produces -- and
    against a run that failed, since an error path is where a credential
    most often escapes.
    """
    _seed_access_url()
    FakeBridge(date(2025, 8, 9), date(2026, 8, 9), fail_on_request=2).install(httpx_mock)

    result = run_backfill("2025-08-09", "2026-08-09", dry_run=False)
    assert result.ok is False

    surfaces = [
        result.render(),
        json.dumps(result.to_dict()),
        state_path(date(2025, 8, 9), date(2026, 8, 9)).read_text(encoding="utf-8"),
        _all_transactions_text(),
    ]
    for surface in surfaces:
        assert CREDENTIAL not in surface
        assert ACCESS_URL not in surface


def test_a_credential_bearing_error_string_is_redacted_before_it_is_stored(
    bookkeeper_root, monkeypatch
):
    """Belt to the fetch layer's braces.

    `simplefin/fetch.py` already redacts to a bare hostname, so this covers
    the case it cannot: an error carrying a credential URL arriving from
    somewhere else entirely. The host survives -- it is not sensitive and it
    is what makes the error useful.
    """
    from bookkeeper.ingest import backfill as backfill_module
    from bookkeeper.ingest.sync import SyncResult

    monkeypatch.setattr(
        backfill_module,
        "run_sync",
        lambda **kw: SyncResult(ok=False, errors=[f"request to {ACCESS_URL}/accounts failed"]),
    )

    result = run_backfill("2026-01-01", "2026-01-10", dry_run=False)

    rendered = result.render()
    assert CREDENTIAL not in rendered
    assert "bridge.example.com" in rendered
    assert CREDENTIAL not in state_path(date(2026, 1, 1), date(2026, 1, 10)).read_text(
        encoding="utf-8"
    )
