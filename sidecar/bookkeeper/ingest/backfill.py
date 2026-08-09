"""Paginated historical backfill in <=90-day windows (PLAN.md 3.1, Phase 6).

SimpleFIN caps a request at 90 days, and it does not do so by failing: a
wider range comes back **HTTP 200** with a silently truncated body and
`"errors": ["Requested date range exceeds limit of 90 days and was
capped."]`. So a year of history is not one request, it is roughly five --
and a run that treats 200 as success reports a complete backfill with holes
in it.

Three things make this module the shape it is.

**The budget is ~24 requests/day and there is no retrying out of a bug.**
A botched five-window run can eat a quarter of the day's budget and leave
the user unable to sync until tomorrow. So: the windows are computed up
front and printable (`plan_windows`), a dry run is the default and spends
nothing, every request is counted, and a run that dies at window three
resumes at window three rather than starting over. The request ledger is
not a number we keep -- it is `data/raw/simplefin-<timestamp>.json`, one
file per response `simplefin/fetch.py` has ever received, which is
evidence rather than bookkeeping.

**Three date ranges, not one.** The range *requested*, the range the server
*honoured* (a capped window honoured less than it was asked), and the range
the data actually *spans* are three different facts, and collapsing them
produces exactly the confident wrong answer `reports/monthend.py` draws the
`through` / `data_through` distinction to avoid. `BackfillResult` carries
all three and never infers one from another.

**Idempotency has to survive pagination.** Windows deliberately overlap by
one day at each boundary -- a bank's posted timestamps are not aligned to
UTC midnight, and a gap between windows would lose transactions silently
while an overlap costs nothing, because dedup is keyed on
`(asset_account, simplefin_id)` and absorbs it. Phase 1's property (sync
twice, byte-identical ledger) is proven here across a whole multi-window
run in `tests/test_ingest_backfill.py`.

Windows run oldest-first. `balances.beancount` is rewritten wholesale from
whatever the *last* request reported, so the last request should be the
most recent one.

The Access URL never appears in this module. Backfill does not resolve it,
does not hold it, and `_redact` scrubs any embedded credential out of an
upstream error string before it reaches the state file, the response, or
the terminal.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from bookkeeper import paths
from bookkeeper.categorize.gitcommit import CommitResult, commit_ledger
from bookkeeper.ingest.normalize import OpeningBalance
from bookkeeper.ingest.render import render_opening_balance
from bookkeeper.ingest.sync import run_sync

#: Calendar days per window, inclusive of both endpoints. 90 days inclusive
#: spans 89 days plus 86,399 seconds once `run_sync` closes the window at
#: the last second of its end date -- one second inside the server's limit,
#: which is the side of it to be on. `WINDOW_STRIDE` is one less: consecutive
#: windows share their boundary day on purpose (see the module docstring).
WINDOW_MAX_DAYS = 90
WINDOW_STRIDE = WINDOW_MAX_DAYS - 1

#: Requests SimpleFIN allows per day (PLAN.md 3.1, "~24"). Treated as hard.
DAILY_REQUEST_BUDGET = 24

#: Substrings that mean "we got 200 and the server truncated the answer."
#: Matched case-insensitively against every soft error a fetch reports, both
#: the documented `errlist` and the live-observed top-level `errors`. Two
#: markers rather than the exact sentence: the wording is not in the
#: protocol doc, so it is not something we can hold the server to.
_CAP_MARKERS = ("exceeds limit", "was capped", "date range exceeds")

#: Window outcomes. `done` and `already-done` are the only two that count as
#: coverage; everything else means the window must be requested again.
STATE_PLANNED = "planned"  # dry run: this is what would be requested
STATE_DONE = "done"  # fetched cleanly in this run
STATE_ALREADY_DONE = "already-done"  # completed by an earlier run; costs nothing
STATE_CAPPED = "capped"  # HTTP 200, server said it truncated -- not coverage
STATE_FAILED = "failed"  # the fetch or the write failed
STATE_SKIPPED = "skipped"  # never attempted: the run stopped before reaching it

_COVERING_STATES = frozenset({STATE_DONE, STATE_ALREADY_DONE})

#: Any URL carrying `user:password@`. The Access URL is banking-credential
#: grade and must never reach a log, a response, or a file we write; the
#: fetch layer already redacts to a bare hostname, and this is the belt to
#: that pair of braces for error text arriving from anywhere else.
_CREDENTIAL_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/@]*:[^\s/@]*@(\S*)")

_RAW_ARCHIVE_PREFIX = "simplefin-"


def _redact(text: str) -> str:
    return _CREDENTIAL_URL_RE.sub(r"<redacted-credential-url>/\1", text)


def _redact_all(items: Any) -> tuple[str, ...]:
    return tuple(_redact(str(item)) for item in items)


# ---------------------------------------------------------------------------
# Window arithmetic
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackfillWindow:
    """One <=90-day request, both endpoints inclusive.

    `index` is 1-based and stable for a given (from, to) pair: it is how a
    resumed run, a state file and a rendered plan all refer to the same
    window without re-deriving it.
    """

    index: int
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1


def plan_windows(
    from_date: date, to_date: date, window_days: int = WINDOW_MAX_DAYS
) -> tuple[BackfillWindow, ...]:
    """Split `[from_date, to_date]` into consecutive <=`window_days` windows.

    Computed up front and returned rather than iterated lazily, because the
    first thing a caller must be able to do is *look* at what a run would
    spend before spending it.

    Consecutive windows share a boundary day (window N ends on the day
    window N+1 starts), so each one advances `window_days - 1`. That overlap
    is the point: transaction timestamps are not UTC-midnight aligned, a gap
    would drop transactions with nothing to show for it, and a duplicate
    costs exactly one dedup hit on `(asset_account, simplefin_id)`.
    """
    if window_days < 2:
        raise ValueError("window_days must be at least 2")
    if to_date < from_date:
        raise ValueError(f"to_date {to_date.isoformat()} precedes from_date {from_date.isoformat()}")

    stride = window_days - 1
    windows: list[BackfillWindow] = []
    cursor = from_date
    while True:
        end = min(cursor + timedelta(days=window_days - 1), to_date)
        windows.append(BackfillWindow(index=len(windows) + 1, start=cursor, end=end))
        if end >= to_date:
            return tuple(windows)
        cursor = cursor + timedelta(days=stride)


def response_was_capped(errors: list[str] | tuple[str, ...]) -> bool:
    """Did the server say it truncated the range, on an otherwise fine 200?

    Checked on every window even though every window we build is <=90 days
    by construction. If this ever fires, our arithmetic and the server's
    disagree, and the correct response is to stop the run rather than spend
    the rest of the day's budget on windows built the same way.
    """
    return any(marker in str(e).lower() for e in errors for marker in _CAP_MARKERS)


# ---------------------------------------------------------------------------
# Request budget, counted from evidence
# ---------------------------------------------------------------------------


def requests_used_today(today: date | None = None) -> int:
    """Requests spent today, counted from the raw response archive.

    `simplefin/fetch.py` archives every response it receives as
    `data/raw/simplefin-<ISO-8601 UTC>.json` before parsing it, so the
    archive is already a per-request ledger and does not need a second one
    that could disagree with it.

    A **lower bound**, and deliberately described as one: a request that
    failed its status check spent budget without producing an archive file.
    Under-counting means a backfill can be allowed to start when the real
    remaining budget is smaller, which is why a run that hits a rate-limit
    rejection mid-way stops and reports rather than retrying.
    """
    day = today or datetime.now(UTC).date()
    raw = paths.raw_dir()
    if not raw.exists():
        return 0
    prefix = f"{_RAW_ARCHIVE_PREFIX}{day.isoformat()}T"
    return sum(1 for p in raw.glob(f"{_RAW_ARCHIVE_PREFIX}*.json") if p.name.startswith(prefix))


# ---------------------------------------------------------------------------
# Resumable state
# ---------------------------------------------------------------------------

#: On-disk state format. Bumped if the window layout or field meanings
#: change, so a stale file is ignored loudly instead of resumed wrongly.
STATE_VERSION = 1


def state_dir() -> Path:
    return paths.data_dir() / "backfill"


def state_path(from_date: date, to_date: date) -> Path:
    """Where progress for one requested range lives.

    One file per range, named after the range. Backfilling 2025 and then
    2024 are two runs with two answers, and a single shared state file would
    make starting the second forget the first -- which, at ~24 requests a
    day, is not a cosmetic loss. Plain JSON on purpose: `cat` is the
    intended UI for "where did it get to", so resumability is inspectable
    rather than implicit.
    """
    return state_dir() / f"{from_date.isoformat()}_{to_date.isoformat()}.json"


@dataclass(frozen=True)
class WindowOutcome:
    """What happened to one window. Flat, because it is also a JSON record."""

    index: int
    start: date
    end: date
    state: str
    #: 0 or 1. A window resumed from a previous run costs nothing, and that
    #: is the whole reason the state file exists.
    requests: int = 0
    transactions_seen: int = 0
    transactions_added: int = 0
    #: The span of the data this window actually returned -- not its
    #: endpoints. A quiet fortnight at the start of a window means the data
    #: begins later than the request did.
    data_from: date | None = None
    data_through: date | None = None
    errors: tuple[str, ...] = ()

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    @property
    def covered(self) -> bool:
        return self.state in _COVERING_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "days": self.days,
            "state": self.state,
            "requests": self.requests,
            "transactions_seen": self.transactions_seen,
            "transactions_added": self.transactions_added,
            "data_from": None if self.data_from is None else self.data_from.isoformat(),
            "data_through": None if self.data_through is None else self.data_through.isoformat(),
            "errors": list(self.errors),
        }


def _outcome_from_dict(raw: dict[str, Any]) -> WindowOutcome:
    def _date(value: Any) -> date | None:
        return None if value in (None, "") else date.fromisoformat(str(value))

    start = _date(raw["start"])
    end = _date(raw["end"])
    if start is None or end is None:
        raise ValueError("window record is missing its endpoints")
    return WindowOutcome(
        index=int(raw["index"]),
        start=start,
        end=end,
        state=str(raw["state"]),
        requests=int(raw.get("requests", 0)),
        transactions_seen=int(raw.get("transactions_seen", 0)),
        transactions_added=int(raw.get("transactions_added", 0)),
        data_from=_date(raw.get("data_from")),
        data_through=_date(raw.get("data_through")),
        errors=_redact_all(raw.get("errors", ())),
    )


def load_state(from_date: date, to_date: date) -> dict[int, WindowOutcome]:
    """Completed windows from a previous run of this exact range.

    Returns `{}` -- start over -- rather than raising on anything unexpected:
    a state file we cannot read is worth exactly as much as no state file,
    and the cost of ignoring it is re-fetching, which is safe. The cost of
    *misreading* it is skipping a window that was never fetched, which is a
    hole in the ledger nobody would see.
    """
    path = state_path(from_date, to_date)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if int(raw.get("version", 0)) != STATE_VERSION:
            return {}
        if raw.get("requested_from") != from_date.isoformat():
            return {}
        if raw.get("requested_to") != to_date.isoformat():
            return {}
        if int(raw.get("window_days", 0)) != WINDOW_MAX_DAYS:
            return {}
        return {o.index: o for o in (_outcome_from_dict(w) for w in raw.get("windows", []))}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_state(from_date: date, to_date: date, outcomes: list[WindowOutcome]) -> Path:
    """Write progress after every window, not at the end of the run.

    A run that dies at window three must leave behind the fact that windows
    one and two are done; state written only on success would make a crash
    cost the whole range again.
    """
    path = state_path(from_date, to_date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": STATE_VERSION,
                "requested_from": from_date.isoformat(),
                "requested_to": to_date.isoformat(),
                "window_days": WINDOW_MAX_DAYS,
                "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "windows": [o.to_dict() for o in outcomes],
            },
            indent=2,
            sort_keys=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# Deferred opening balances
# ---------------------------------------------------------------------------

#: `render_transaction` / `render_opening_balance` output. Scanned as text,
#: for the reasons `ingest/dedup.py` documents: a transactions/YYYY.beancount
#: fragment does not load standalone through beancount's parser, and this
#: needs no opinion about whether the surrounding entries are valid.
_ENTRY_DATE_RE = re.compile(r'^(\d{4}-\d{2}-\d{2}) \* "')
_OPENING_MARKER = 'simplefin-opening: "true"'
_ASSET_POSTING_RE = re.compile(r"^ {2}(Assets:\S+)\s+(-?\d+(?:\.\d+)?) ([A-Z]{2,})\s*$")
_BALANCE_LINE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}) balance (\S+)\s+(-?\d+(?:\.\d+)?) ([A-Z]{2,})\s*$"
)


@dataclass(frozen=True)
class _AccountHistory:
    """What the ledger already records for one asset account."""

    total: Decimal
    first_posted: date | None
    opening_amount: Decimal | None  # None when the account has no plug yet


def _scan_account_history(transactions_dir: Path) -> dict[str, _AccountHistory]:
    """Sum every recorded transaction per asset account, plug excluded.

    The plug's own posting is on an `Assets:` line indistinguishable from a
    transaction's, so entries carrying `simplefin-opening: "true"` are
    tracked separately -- counting one into the total would make the plug a
    function of itself.
    """
    totals: dict[str, Decimal] = {}
    firsts: dict[str, date] = {}
    openings: dict[str, Decimal] = {}
    if not transactions_dir.exists():
        return {}

    for path in sorted(transactions_dir.glob("*.beancount")):
        entry_date: date | None = None
        is_opening = False
        for line in path.read_text(encoding="utf-8").splitlines():
            header = _ENTRY_DATE_RE.match(line)
            if header:
                entry_date = date.fromisoformat(header.group(1))
                is_opening = False
                continue
            if line.strip() == _OPENING_MARKER:
                is_opening = True
                continue
            posting = _ASSET_POSTING_RE.match(line)
            if not posting or entry_date is None:
                continue
            account, amount = posting.group(1), Decimal(posting.group(2))
            if is_opening:
                openings[account] = openings.get(account, Decimal(0)) + amount
                continue
            totals[account] = totals.get(account, Decimal(0)) + amount
            if account not in firsts or entry_date < firsts[account]:
                firsts[account] = entry_date

    accounts = set(totals) | set(openings)
    return {
        account: _AccountHistory(
            total=totals.get(account, Decimal(0)),
            first_posted=firsts.get(account),
            opening_amount=openings.get(account),
        )
        for account in accounts
    }


def _scan_balance_assertions(balances_path: Path) -> dict[str, tuple[Decimal, str, date]]:
    """`{account: (asserted balance, currency, balance date)}` from disk.

    The assertion is dated `balance-date + 1` (see `NormalizedBalance`), so
    the balance date is one day back -- which is the date a plug falls back
    to for an account with no transactions at all.
    """
    if not balances_path.exists():
        return {}
    found: dict[str, tuple[Decimal, str, date]] = {}
    for line in balances_path.read_text(encoding="utf-8").splitlines():
        match = _BALANCE_LINE_RE.match(line)
        if match:
            assertion_date = date.fromisoformat(match.group(1))
            found[match.group(2)] = (
                Decimal(match.group(3)),
                match.group(4),
                assertion_date - timedelta(days=1),
            )
    return found


def write_deferred_opening_balances() -> tuple[int, list[str]]:
    """Write the one-time `Equity:Opening-Balances` plug per account, once.

    Called after a backfill has covered its whole requested range, when the
    ledger finally holds every transaction the run is going to add. The
    formula is `run_sync`'s -- `balance - (transactions recorded)` -- with
    the complete on-disk history as its input instead of one window's worth,
    which is the only difference that matters and the whole reason the plug
    is deferred.

    Also *checks* the plugs it does not write: an account plugged by an
    earlier plain `sync` and then backfilled has a plug short by everything
    the backfill added, and its balance assertion will not reconcile. That
    is reported by name with both figures rather than left for bean-check to
    discover, because "your assertion is off by 1,240.55" is a much worse
    place to start than "this plug predates the backfill".

    Returns `(plugs written, warnings)`.
    """
    transactions_dir = paths.transactions_dir()
    history = _scan_account_history(transactions_dir)
    asserted = _scan_balance_assertions(paths.balances_ledger())

    warnings: list[str] = []
    by_year: dict[int, list[OpeningBalance]] = {}
    for account in sorted(asserted):
        balance, currency, balance_date = asserted[account]
        recorded = history.get(account, _AccountHistory(Decimal(0), None, None))
        expected = balance - recorded.total
        if recorded.opening_amount is not None:
            if recorded.opening_amount != expected:
                warnings.append(
                    f"{account}: opening balance plug is {recorded.opening_amount} but the "
                    f"ledger's history now implies {expected} -- the plug predates these "
                    "transactions, so its balance assertion will not reconcile until it is "
                    "corrected by hand"
                )
            continue
        entry_date = (
            recorded.first_posted - timedelta(days=1)
            if recorded.first_posted is not None
            else balance_date
        )
        by_year.setdefault(entry_date.year, []).append(
            OpeningBalance(
                asset_account=account,
                amount=expected,
                currency=currency,
                entry_date=entry_date,
            )
        )

    written = 0
    if by_year:
        transactions_dir.mkdir(parents=True, exist_ok=True)
        for year in sorted(by_year):
            block = "".join(render_opening_balance(ob) for ob in by_year[year])
            with (transactions_dir / f"{year}.beancount").open("a", encoding="utf-8") as f:
                f.write(block)
            written += len(by_year[year])

    return written, warnings


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class BackfillResult:
    ok: bool
    dry_run: bool
    #: The range the caller **asked for**.
    requested_from: date | None = None
    requested_to: date | None = None
    #: The range the server **honoured**: the contiguous run of covered
    #: windows starting at `requested_from`. Stops at the first window that
    #: was capped, failed, or was never attempted -- coverage with a hole in
    #: the middle is not coverage, and reporting the last successful window's
    #: end would claim the hole was covered too.
    honoured_from: date | None = None
    honoured_through: date | None = None
    #: The range the returned data actually **spans**. Narrower than
    #: `honoured_*` whenever the accounts were quiet at either end, and it is
    #: not evidence of anything about coverage: an account with no January
    #: activity and an account nobody fetched January for look identical here
    #: and are told apart by `honoured_through`.
    data_from: date | None = None
    data_through: date | None = None
    windows: tuple[WindowOutcome, ...] = ()
    #: Requests this run actually sent, requests it still needs to send, and
    #: what today's budget looks like around them.
    requests_made: int = 0
    requests_planned: int = 0
    requests_used_today: int = 0
    requests_remaining_today: int = 0
    daily_budget: int = DAILY_REQUEST_BUDGET
    transactions_seen: int = 0
    transactions_added: int = 0
    opening_balances_written: int = 0
    #: Why the run stopped short, if it did: `budget`, `capped`, `failed`.
    #: `None` when it did not.
    stopped_reason: str | None = None
    #: Where progress was recorded, so the next run can be resumed and this
    #: one can be read back with `cat`. `None` on a dry run, which writes
    #: nothing at all.
    state_path: str | None = None
    commit: CommitResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Is the whole requested range covered?

        Derived from `honoured_through` rather than stored, so it cannot
        disagree with the date it is a claim about.
        """
        return self.requested_to is not None and self.honoured_through == self.requested_to

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe, and the exact field set `api.BackfillResponse` declares."""

        def _iso(value: date | None) -> str | None:
            return None if value is None else value.isoformat()

        return {
            "ok": self.ok,
            "summary": self.render(),
            "dry_run": self.dry_run,
            "complete": self.complete,
            "requested_from": _iso(self.requested_from),
            "requested_to": _iso(self.requested_to),
            "honoured_from": _iso(self.honoured_from),
            "honoured_through": _iso(self.honoured_through),
            "data_from": _iso(self.data_from),
            "data_through": _iso(self.data_through),
            "windows": [w.to_dict() for w in self.windows],
            "requests_made": self.requests_made,
            "requests_planned": self.requests_planned,
            "requests_used_today": self.requests_used_today,
            "requests_remaining_today": self.requests_remaining_today,
            "daily_budget": self.daily_budget,
            "transactions_seen": self.transactions_seen,
            "transactions_added": self.transactions_added,
            "opening_balances_written": self.opening_balances_written,
            "stopped_reason": self.stopped_reason,
            "state_path": self.state_path,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
        }

    def render(self) -> str:
        if self.requested_from is None or self.requested_to is None:
            lines = ["backfill failed:"]
            lines.extend(f"  - {e}" for e in self.errors)
            return "\n".join(lines)

        mode = "dry run -- nothing was requested" if self.dry_run else "live"
        lines = [
            (
                f"backfill {self.requested_from.isoformat()} -> "
                f"{self.requested_to.isoformat()} ({mode})"
            ),
            (
                f"windows: {len(self.windows)} x <={WINDOW_MAX_DAYS} days "
                f"(consecutive windows share their boundary day)"
            ),
        ]
        for w in self.windows:
            detail = ""
            if w.state in (STATE_DONE, STATE_ALREADY_DONE):
                detail = f"  +{w.transactions_added} new of {w.transactions_seen} seen"
            lines.append(
                f"  {w.index}. {w.start.isoformat()} -> {w.end.isoformat()} "
                f"({w.days}d) {w.state}{detail}"
            )
        # The three ranges together, deliberately adjacent: read apart they
        # are easy to mistake for each other, and side by side the
        # difference is the first thing visible.
        lines.extend(
            [
                "coverage:",
                (
                    f"  requested:  {self.requested_from.isoformat()} -> "
                    f"{self.requested_to.isoformat()}"
                ),
                f"  honoured:   {_span(self.honoured_from, self.honoured_through)}",
                f"  data spans: {_span(self.data_from, self.data_through)}",
                f"  complete: {'yes' if self.complete else 'no'}",
                (
                    f"requests: {self.requests_made} made, {self.requests_planned} still "
                    f"needed; {self.requests_used_today} of {self.daily_budget} used today, "
                    f"{self.requests_remaining_today} left"
                ),
                (
                    f"transactions: {self.transactions_added} added of "
                    f"{self.transactions_seen} seen"
                ),
            ]
        )
        if not self.dry_run:
            lines.append(f"opening balance plugs written: {self.opening_balances_written}")
        if self.state_path:
            lines.append(f"progress recorded at {self.state_path}")
        if self.stopped_reason:
            lines.append(f"stopped early: {self.stopped_reason}")
        if self.commit is not None:
            lines.append(self.commit.render())
        if self.errors:
            lines.append("errors:")
            lines.extend(f"  - {e}" for e in self.errors)
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _span(start: date | None, end: date | None) -> str:
    if start is None or end is None:
        return "none"
    return f"{start.isoformat()} -> {end.isoformat()}"


def _coverage(
    outcomes: list[WindowOutcome], requested_from: date
) -> tuple[date | None, date | None]:
    """The contiguous covered prefix, which is the only honest reading of it."""
    through: date | None = None
    for outcome in outcomes:
        if not outcome.covered:
            break
        through = outcome.end
    return (requested_from if through is not None else None), through


def _data_span(outcomes: list[WindowOutcome]) -> tuple[date | None, date | None]:
    starts = [o.data_from for o in outcomes if o.data_from is not None]
    ends = [o.data_through for o in outcomes if o.data_through is not None]
    return (min(starts) if starts else None), (max(ends) if ends else None)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def run_backfill(
    from_date: str,
    to_date: str | None = None,
    *,
    dry_run: bool = True,
    restart: bool = False,
    max_requests: int | None = None,
    demo: bool = False,
) -> BackfillResult:
    """Backfill `[from_date, to_date]` in <=90-day windows.

    **`dry_run` defaults to True**, which is the one default worth arguing
    about. `categorize` already establishes that a write is opt-in here, and
    the asymmetry is sharper for this command: a preview that should have
    been a run costs a retype, and a run that should have been a preview
    costs a fifth of a day's requests and cannot be undone by any amount of
    being sorry. `--dry-run` is accepted explicitly all the same, because a
    caller who says what they mean should not have to know the default.

    Resuming is also the default: windows already recorded `done` for this
    exact range are skipped and spend nothing. `restart=True` re-fetches
    them, which is only ever worth it if you believe the earlier fetch was
    wrong -- it is real money against the budget.
    """
    try:
        start = date.fromisoformat(from_date)
    except ValueError as exc:
        return BackfillResult(ok=False, dry_run=dry_run, errors=[f"bad --from date: {exc}"])
    try:
        end = date.fromisoformat(to_date) if to_date else datetime.now(UTC).date()
    except ValueError as exc:
        return BackfillResult(ok=False, dry_run=dry_run, errors=[f"bad --to date: {exc}"])
    if end < start:
        return BackfillResult(
            ok=False,
            dry_run=dry_run,
            errors=[
                (
                    f"--to {end.isoformat()} precedes --from {start.isoformat()}; "
                    "there is no range to backfill"
                )
            ],
        )

    windows = plan_windows(start, end)
    previous = {} if restart else load_state(start, end)

    used_today = requests_used_today()
    remaining_today = max(0, DAILY_REQUEST_BUDGET - used_today)

    # Everything not already recorded as cleanly fetched has to be requested
    # again -- including a window recorded `capped` or `failed`, whose data
    # is on disk but whose coverage is not something we can claim.
    def _already_done(window: BackfillWindow) -> WindowOutcome | None:
        prior = previous.get(window.index)
        if prior is None or prior.start != window.start or prior.end != window.end:
            return None
        if prior.state not in _COVERING_STATES:
            return None
        return WindowOutcome(
            index=window.index,
            start=window.start,
            end=window.end,
            state=STATE_ALREADY_DONE,
            requests=0,
            transactions_seen=prior.transactions_seen,
            transactions_added=0,  # this run added nothing; the earlier one did
            data_from=prior.data_from,
            data_through=prior.data_through,
        )

    resolved = {w.index: _already_done(w) for w in windows}
    needed = [w for w in windows if resolved[w.index] is None]

    if dry_run:
        outcomes = [
            resolved[w.index]
            or WindowOutcome(index=w.index, start=w.start, end=w.end, state=STATE_PLANNED)
            for w in windows
        ]
        honoured_from, honoured_through = _coverage(outcomes, start)
        data_from, data_through = _data_span(outcomes)
        affordable = len(needed) <= remaining_today
        result = BackfillResult(
            ok=affordable,
            dry_run=True,
            requested_from=start,
            requested_to=end,
            honoured_from=honoured_from,
            honoured_through=honoured_through,
            data_from=data_from,
            data_through=data_through,
            windows=tuple(outcomes),
            requests_made=0,
            requests_planned=len(needed),
            requests_used_today=used_today,
            requests_remaining_today=remaining_today,
            transactions_seen=sum(o.transactions_seen for o in outcomes),
        )
        if not affordable:
            result.errors.append(
                f"{len(needed)} request(s) needed but only {remaining_today} left of today's "
                f"{DAILY_REQUEST_BUDGET}; run what fits with --max-requests and resume tomorrow"
            )
        return result

    return _execute(
        start=start,
        end=end,
        windows=windows,
        resolved=resolved,
        needed=needed,
        used_today=used_today,
        remaining_today=remaining_today,
        max_requests=max_requests,
        demo=demo,
    )


def _execute(
    *,
    start: date,
    end: date,
    windows: tuple[BackfillWindow, ...],
    resolved: dict[int, WindowOutcome | None],
    needed: list[BackfillWindow],
    used_today: int,
    remaining_today: int,
    max_requests: int | None,
    demo: bool,
) -> BackfillResult:
    """Spend requests. Every path out of here writes the state file first."""
    allowance = remaining_today if max_requests is None else min(remaining_today, max_requests)

    errors: list[str] = []
    warnings: list[str] = []
    stopped_reason: str | None = None
    requests_made = 0

    outcomes: list[WindowOutcome] = []
    stopped = False
    for window in windows:
        prior = resolved[window.index]
        if prior is not None:
            outcomes.append(prior)
            continue
        if stopped:
            outcomes.append(
                WindowOutcome(
                    index=window.index, start=window.start, end=window.end, state=STATE_SKIPPED
                )
            )
            continue
        if requests_made >= allowance:
            stopped = True
            stopped_reason = "budget"
            errors.append(
                f"stopped before window {window.index}: {requests_made} request(s) made, "
                f"{allowance} allowed today ({used_today} of {DAILY_REQUEST_BUDGET} already "
                "spent). Rerun to resume from this window."
            )
            outcomes.append(
                WindowOutcome(
                    index=window.index, start=window.start, end=window.end, state=STATE_SKIPPED
                )
            )
            continue

        # One window, one request. `run_sync` writes as it goes, which is
        # what makes a crashed run resumable rather than lost -- the cost is
        # that a capped window's (real, but incomplete) data is on disk
        # before we know it was capped. That is the right trade: the data is
        # true, only the coverage claim would have been false.
        sync = run_sync(
            since=window.start.isoformat(),
            demo=demo,
            until=window.end.isoformat(),
            write_opening_balances=False,
        )
        requests_made += 1
        window_errors = _redact_all(sync.errors)

        if not sync.ok:
            stopped = True
            stopped_reason = "failed"
            errors.append(f"window {window.index} failed: {'; '.join(window_errors)}")
            outcomes.append(
                WindowOutcome(
                    index=window.index,
                    start=window.start,
                    end=window.end,
                    state=STATE_FAILED,
                    requests=1,
                    errors=window_errors,
                )
            )
            save_state(start, end, outcomes + _pending_tail(windows, outcomes, resolved))
            continue

        capped = response_was_capped(sync.errors)
        if capped:
            # Our windows are <=90 days by construction, so a cap means the
            # server's arithmetic and ours disagree. Continuing would spend
            # the rest of the budget building windows the same way.
            stopped = True
            stopped_reason = "capped"
            errors.append(
                f"window {window.index} ({window.start.isoformat()} -> "
                f"{window.end.isoformat()}, {window.days} days) came back capped by the "
                "server despite being within the 90-day limit -- its data is incomplete and "
                "the run stopped rather than spend the rest of today's budget"
            )
        outcomes.append(
            WindowOutcome(
                index=window.index,
                start=window.start,
                end=window.end,
                state=STATE_CAPPED if capped else STATE_DONE,
                requests=1,
                transactions_seen=sync.transactions_seen,
                transactions_added=sync.transactions_added,
                data_from=sync.data_from,
                data_through=sync.data_through,
                errors=window_errors,
            )
        )
        if not capped:
            warnings.extend(window_errors)  # soft errors that were not a cap
        save_state(start, end, outcomes + _pending_tail(windows, outcomes, resolved))

    honoured_from, honoured_through = _coverage(outcomes, start)
    data_from, data_through = _data_span(outcomes)
    complete = honoured_through == end

    opening_written = 0
    commit: CommitResult | None = None
    if complete:
        # Only now: a plug is `balance - (everything recorded)`, and until
        # the last window has landed "everything recorded" is a moving
        # target. A run that stops short leaves no plug at all, so its
        # balance assertions fail loudly instead of reconciling against a
        # number that happens to be wrong.
        opening_written, plug_warnings = write_deferred_opening_balances()
        warnings.extend(plug_warnings)
        if opening_written:
            commit = commit_ledger(
                f"Backfill {start.isoformat()}..{end.isoformat()}: "
                f"{opening_written} opening balance plug(s)"
            )
    elif requests_made:
        warnings.append(
            "opening balance plugs are not written until the whole range is covered, so "
            "balance assertions will not reconcile until this backfill completes"
        )

    written_state = save_state(start, end, outcomes)
    return BackfillResult(
        ok=complete,
        dry_run=False,
        requested_from=start,
        requested_to=end,
        honoured_from=honoured_from,
        honoured_through=honoured_through,
        data_from=data_from,
        data_through=data_through,
        windows=tuple(outcomes),
        requests_made=requests_made,
        requests_planned=sum(1 for o in outcomes if not o.covered),
        requests_used_today=used_today + requests_made,
        requests_remaining_today=max(0, DAILY_REQUEST_BUDGET - used_today - requests_made),
        transactions_seen=sum(o.transactions_seen for o in outcomes),
        transactions_added=sum(o.transactions_added for o in outcomes),
        opening_balances_written=opening_written,
        stopped_reason=stopped_reason,
        state_path=str(written_state),
        commit=commit,
        errors=errors,
        warnings=warnings,
    )


def _pending_tail(
    windows: tuple[BackfillWindow, ...],
    done: list[WindowOutcome],
    resolved: dict[int, WindowOutcome | None],
) -> list[WindowOutcome]:
    """Windows not yet reached, so a mid-run state file describes all of them.

    Without this, a crash at window three would leave a file listing two
    windows, and a reader would have to know the range's arithmetic to tell
    "three of five" from "two of two".
    """
    seen = {o.index for o in done}
    return [
        resolved[w.index]
        or WindowOutcome(index=w.index, start=w.start, end=w.end, state=STATE_SKIPPED)
        for w in windows
        if w.index not in seen
    ]
