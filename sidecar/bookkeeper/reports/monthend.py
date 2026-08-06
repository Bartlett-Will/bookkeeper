"""The month-end report (PLAN.md §6 Phase 5 exit criterion).

What someone actually wants at the end of a month: how each envelope came
out, what was budgeted against what was spent, which way things are moving,
what was unusual, and what is still uncategorized.

**Every figure here is computed by the module that owns it.** This one joins
and narrates; it does no financial arithmetic of its own. That is
load-bearing rather than tidy — two definitions of "spent on Groceries in
July" drift, and the drift surfaces as a month-end report that disagrees with
`/envelopes` about the user's own money, which is the bug class §5.2 exists
to prevent.

    per-envelope allocated / spent / carried in / balance   reports.budget
    unmapped spending                                       reports.budget
    direction per envelope, unusual transactions            reports.trends
    cash on hand, available to budget, total overspend      envelope.compute

`reports.budget` itself differences two `compute_envelope_state` reports
across the window, so the table below is provably the same arithmetic
`/envelopes` performs.

**An uncategorized month must read as one.** Auto-apply is off (Phase 3), so
today essentially all spend still posts to `Expenses:Unknown`, which no
envelope claims. A report that showed only the per-envelope table would
render a month of real spending as a month of zeros — the single most likely
way this report lies to someone. So the categorization state is computed
first, stated *above* the table rather than footnoted beneath it, and the
table is explicitly qualified by the share of the month's spending it
actually describes. A qualification a reader meets after forming a conclusion
has already failed.

**"So far this month" and "for July" are different claims.** A user acts on
them differently, so `coverage` distinguishes a month that has not started, a
month in progress, a month whose data stops part-way through, a month with no
transactions at all, and a month fully recorded. The closing figures for an
in-progress month are computed as of *today*, never as of a month-end that
has not happened, so an allocation dated the 31st cannot inflate a report run
on the 5th.

**Trends need more than one month.** Direction is read over a trailing window
of `TRAILING_MONTHS` ending with the reported month — a single month is one
data point and `trends` would rightly abstain on it. Outliers are then
filtered to transactions *within* the reported month, judged against that
longer baseline: filtering what to show is not recomputing it, and the wider
baseline is what makes the judgement worth anything.

Every statement the render makes carries the figure it was read from. "You're
on track" is one short unverifiable sentence someone would act on; "Dining
Out spent 210.00 against 150.00 allocated" is checkable against the table
directly above it. Where a judgement could not be made, the report says so
rather than defaulting to the reassuring answer.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from beancount.core.data import Transaction

from bookkeeper.envelope.compute import coerce_asof, compute_envelope_state, load_ledger
from bookkeeper.envelope.directives import build_account_map, parse_envelope_directives
from bookkeeper.reports.budget import budget_report
from bookkeeper.reports.trends import Outlier, trends_report

_FALLBACK_CURRENCY = "USD"

#: Only `Expenses:` accounts can be "uncategorized" — the same rule
#: `reports.spending` and `reports.budget` apply. Funding and equity legs are
#: the other side of the same money; counting them would make every paycheck
#: an uncategorized transaction.
EXPENSE_ACCOUNT_PREFIX = "Expenses:"

#: Precision for ratios leaving this module. Matches
#: `budget.BudgetLine.consumed_ratio` so the two ratios in one payload do not
#: disagree about how precise they are.
_RATIO_PLACES = Decimal("0.0001")

#: How many months of history the direction verdicts are read over, the
#: reported month included. `trends.MIN_PERIODS` is 3, so this leaves room to
#: abstain on genuinely new envelopes rather than on every envelope; and it
#: is short enough that a habit changed in the spring does not still be
#: shaping the verdict in the autumn.
TRAILING_MONTHS = 6

#: Month names as a constant rather than `strftime("%B")`, which is
#: locale-dependent: a report of a fixed ledger must not change because the
#: host's locale did.
MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

#: How much of the month the ledger can speak for. Distinguished because the
#: claims differ: "for July" and "so far this month" are not the same
#: statement, and "no data" and "hasn't happened yet" are not the same reason
#: for an empty report.
COVERAGE_FUTURE = "future"
COVERAGE_IN_PROGRESS = "in-progress"
COVERAGE_NO_DATA = "no-data"
COVERAGE_PARTIAL = "partial"
COVERAGE_COMPLETE = "complete"

#: What `trends` reports when it could not tell which way an envelope is
#: moving. Spelled out rather than indexed out of `DIRECTIONS` — but checked
#: against it by `tests/test_reports_monthend.py`, so a rename over there
#: fails loudly instead of leaving this module quietly substituting a verdict
#: `trends` never gave.
DIRECTION_UNKNOWN = "insufficient_data"

#: How much of the month's spending reached an envelope.
CATEGORIZATION_NONE = "none"
CATEGORIZATION_PARTIAL = "partial"
CATEGORIZATION_FULL = "full"
CATEGORIZATION_NO_SPEND = "no-spend"


@dataclass(frozen=True)
class MonthEndEnvelope:
    """One envelope's month: a `budget.BudgetLine` joined to its trend.

    Field-for-field the budget line — this type exists to carry `direction`
    alongside it, not to restate it. `overspent` and `over_budget` are two
    different failures and are kept apart deliberately: an envelope is
    *overspent* when its running balance is negative, money already gone with
    nothing behind it and a §5.2 obligation against next month's allocation.
    It is *over budget* when this month's spend exceeded this month's
    allocation, which an envelope carrying a healthy balance can do without
    ever going negative. Collapsing them raises a false alarm or hides a real
    one.
    """

    name: str
    opening_balance: Decimal
    allocated: Decimal
    spent: Decimal
    closing_balance: Decimal
    remaining: Decimal
    overspend: Decimal
    #: A fraction of the allocation, not money, so it is a `float` — the type
    #: `budget.BudgetLine` gives it. `None` when nothing was allocated: 0.0
    #: reads as untouched and 1.0 as exhausted, and a zero budget supports
    #: neither claim.
    consumed_ratio: float | None
    status: str
    direction: str
    direction_reason: str
    #: How many periods the direction was read over, and how many `trends`
    #: requires before it will commit. Both zero when no trend ran at all.
    #: These make `insufficient_data` checkable instead of merely stated.
    periods_observed: int = 0
    periods_required: int = 0

    @property
    def overspent(self) -> bool:
        """Cumulative: the running balance is negative.

        Deliberately *not* `budget.BudgetLine.overspent`, which is the
        window-only reading of the same word — an envelope can blow this
        month's allocation while staying comfortably in the black overall.
        `over_budget` below is that other reading, named apart so the two
        cannot be confused at a call site.
        """
        return self.closing_balance < 0

    @property
    def over_budget(self) -> bool:
        """This period only: spending exceeded this period's allocation."""
        return self.status == "over"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "opening_balance": str(self.opening_balance),
            "allocated": str(self.allocated),
            "spent": str(self.spent),
            "closing_balance": str(self.closing_balance),
            "remaining": str(self.remaining),
            "overspend": str(self.overspend),
            "consumed_ratio": self.consumed_ratio,
            "status": self.status,
            "direction": self.direction,
            "periods_observed": self.periods_observed,
            "periods_required": self.periods_required,
            "direction_reason": self.direction_reason,
            "overspent": self.overspent,
            "over_budget": self.over_budget,
        }


@dataclass
class MonthEndReport:
    ok: bool
    month: str
    label: str
    from_date: date
    to_date: date
    #: The date the closing figures are computed at: the month's last day, or
    #: today when the month is not over. Never a future date, so an
    #: allocation dated the 31st cannot appear in a report run on the 5th.
    asof: date
    coverage: str = COVERAGE_NO_DATA
    #: The last date in the month the ledger actually has a transaction on.
    #: `None` when the month is empty. This is what makes `partial` visible: a
    #: month whose data stops on the 12th is not a month with no spending
    #: after the 12th, it is a month nobody has synced since.
    data_through: date | None = None
    #: The last date *within the reported month* the ledger accounts for.
    #:
    #: Not the same as `data_through`, and the difference is the point.
    #: `data_through` is the last transaction in the month; `through` is how
    #: far the ledger can speak for it, which is the month's end whenever the
    #: ledger continues past it. A January whose last transaction is the 28th
    #: is covered to the 31st — nothing happened in those three days. A March
    #: the ledger stops inside is covered only to where it stops.
    #:
    #: `None` only when there is genuinely no covered date: a month that has
    #: not started, or one that begins after the ledger's last transaction.
    #: A fabricated date here would be exactly the confident-but-wrong claim
    #: this report exists to avoid.
    through: date | None = None
    #: Days of the month elapsed as of `asof` — the whole month once it is
    #: over, the day-of-month while it is running, zero before it starts.
    #: With `days_in_month`, this is what lets a caller say "4 of 31 days"
    #: rather than rendering four days of August like a finished July.
    days_elapsed: int = 0
    days_in_month: int = 0
    transactions: int = 0
    #: Transactions in the month touching an envelope-mapped account, and
    #: touching an unmapped `Expenses:` account, respectively.
    #:
    #: Counts, not money, and required alongside `unmapped_total` rather than
    #: instead of it. With auto-apply off, "$0.00 in envelopes" and "$4,102
    #: across 87 transactions nobody has filed" are the same underlying state,
    #: and only the second reads as something to act on. An amount alone lets
    #: an authoritative-looking table of zeros pass for a quiet month.
    #:
    #: These need not sum to `transactions`: a split transaction with one
    #: mapped and one unmapped leg is honestly in both, and a transaction with
    #: no expense leg at all (a transfer, a paycheck) is in neither.
    categorized_count: int = 0
    uncategorized_count: int = 0
    currency: str = _FALLBACK_CURRENCY
    envelopes: tuple[MonthEndEnvelope, ...] = ()
    opening_total: Decimal = Decimal(0)
    allocated_total: Decimal = Decimal(0)
    spent_total: Decimal = Decimal(0)
    closing_total: Decimal = Decimal(0)
    unmapped_total: Decimal = Decimal(0)
    unmapped_accounts: tuple[str, ...] = ()
    categorization: str = CATEGORIZATION_NO_SPEND
    budgeted_cash: Decimal = Decimal(0)
    available: Decimal = Decimal(0)
    total_overspend: Decimal = Decimal(0)
    #: Unusual transactions dated within the reported month, judged against
    #: the trailing window below.
    outliers: tuple[Outlier, ...] = ()
    trend_from: date | None = None
    trend_to: date | None = None
    #: Envelopes `trends` declined to judge for outliers, and why. Carried so
    #: "nothing unusual" stays distinguishable from "not enough data to look"
    #: — the difference between a finding and an absence of one.
    unjudged: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """The month is over *and* the ledger accounts for all of it.

        Derived from `through` rather than stored, so it cannot disagree with
        the date it is a claim about. Deliberately not `coverage ==
        "complete"`: a finished month the ledger continues past but in which
        nothing happened is `no-data` and is nonetheless fully covered, and a
        caller asking "can I treat these figures as final" deserves `True`
        there. `coverage` says *why* the month looks the way it does; this
        says whether the figures are done moving.
        """
        return self.through is not None and self.through == self.to_date

    @property
    def total_spend(self) -> Decimal:
        """Everything spent this period, envelope or not.

        The denominator that matters: the per-envelope table describes
        `spent_total` out of *this*, and reading the table's own total as the
        month's spending is the misreading this report exists to prevent.
        """
        return self.spent_total + self.unmapped_total

    @property
    def categorized_share(self) -> Decimal:
        """Fraction of the period's spending that reached an envelope, 0–1.

        Quantized to `_RATIO_PLACES` rather than left at `Decimal`'s default
        28 significant digits. The tool and browser layers are forbidden from
        doing arithmetic on money-derived figures, so a figure that arrives
        un-rounded forces the one thing they must not do; it has to arrive
        final. Four places matches `budget.BudgetLine.consumed_ratio`, so the
        two ratios in this payload agree on precision.

        Zero when nothing was spent, so a caller never divides by zero to
        discover there is nothing to divide — but *quantized* zero, not bare
        `Decimal(0)`. Two reasons, both found the hard way at the render edge:

        - One shape, always. `"0"` from this branch beside `"0.6154"` from
          the other invites a client to compare the string against `"0"`,
          which then works for a month with no spending and fails for a month
          with a share that rounds to nothing.
        - No exponent notation. `Decimal` division takes the exponent of the
          dividend less that of the divisor, so `Decimal(0) / Decimal("150.00")`
          is `Decimal("0E+2")` and serializes as the string `"0E+2"`. That
          parses as a number and compares as garbage. Quantizing pins the
          exponent, which is what keeps it out of the payload.
        """
        total = self.total_spend
        if total <= 0:
            return Decimal(0).quantize(_RATIO_PLACES)
        return (self.spent_total / total).quantize(_RATIO_PLACES)

    @property
    def overspent_envelopes(self) -> tuple[MonthEndEnvelope, ...]:
        return tuple(e for e in self.envelopes if e.overspent)

    @property
    def over_budget_envelopes(self) -> tuple[MonthEndEnvelope, ...]:
        return tuple(e for e in self.envelopes if e.over_budget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "month": self.month,
            "label": self.label,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "asof": self.asof.isoformat(),
            "coverage": self.coverage,
            "data_through": None if self.data_through is None else self.data_through.isoformat(),
            "through": None if self.through is None else self.through.isoformat(),
            "complete": self.complete,
            "days_elapsed": self.days_elapsed,
            "days_in_month": self.days_in_month,
            "transactions": self.transactions,
            "categorized_count": self.categorized_count,
            "uncategorized_count": self.uncategorized_count,
            "currency": self.currency,
            "envelopes": [e.to_dict() for e in self.envelopes],
            "opening_total": str(self.opening_total),
            "allocated_total": str(self.allocated_total),
            "spent_total": str(self.spent_total),
            "closing_total": str(self.closing_total),
            "unmapped_total": str(self.unmapped_total),
            "unmapped_accounts": list(self.unmapped_accounts),
            "total_spend": str(self.total_spend),
            "categorization": self.categorization,
            "categorized_share": str(self.categorized_share),
            "budgeted_cash": str(self.budgeted_cash),
            "available": str(self.available),
            "total_overspend": str(self.total_overspend),
            "outliers": [o.to_dict() for o in self.outliers],
            "trend_from": None if self.trend_from is None else self.trend_from.isoformat(),
            "trend_to": None if self.trend_to is None else self.trend_to.isoformat(),
            "unjudged": list(self.unjudged),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    # -- rendering ---------------------------------------------------------

    def _coverage_line(self) -> str:
        """One sentence saying what period this report is actually about.

        Printed before any figure, because every number below means something
        different depending on it.
        """
        if self.coverage == COVERAGE_FUTURE:
            return f"{self.label} has not started. There is nothing to report yet."
        if self.coverage == COVERAGE_IN_PROGRESS:
            through = (
                f", data through {self.data_through.isoformat()}" if self.data_through else ""
            )
            return (
                f"So far this month: day {self.days_elapsed} of {self.days_in_month}, "
                f"{self.from_date.isoformat()} to {self.asof.isoformat()}{through}. "
                f"{self.label} is not over, so these are running figures for a "
                "part-month, not a final month."
            )
        if self.coverage == COVERAGE_NO_DATA:
            return (
                f"The ledger has no transactions in {self.label}. The balances below are "
                "carried in from earlier; nothing moved."
            )
        if self.coverage == COVERAGE_PARTIAL:
            through = self.data_through.isoformat() if self.data_through else "?"
            return (
                f"{self.label} is over, but the ledger stops at {through} — the rest of the "
                "month is missing, not empty. Sync before treating this as final."
            )
        return (
            f"{self.label}, complete: {self.from_date.isoformat()} to "
            f"{self.to_date.isoformat()}."
        )

    def _categorization_lines(self) -> list[str]:
        if self.categorization == CATEGORIZATION_NO_SPEND:
            return ["No spending recorded in this period."]
        if self.categorization == CATEGORIZATION_FULL:
            return [
                (
                    f"All {self.total_spend:.2f} {self.currency} of spending this period "
                    "is filed to an envelope."
                )
            ]

        # The transaction count travels with the amount, always. "$0.00 in
        # envelopes" and "4,102.00 across 87 transactions nobody has filed"
        # are the same state, and only the second reads as work to do.
        across = f" across {self.uncategorized_count} transaction(s)"
        if self.categorization == CATEGORIZATION_NONE:
            headline = (
                f"THIS PERIOD IS NOT CATEGORIZED. All {self.unmapped_total:.2f} "
                f"{self.currency} of spending{across} belongs to no envelope, so the table "
                "below describes none of it. Those envelopes read as unspent because "
                "nothing has been filed to them yet, not because the money was not spent."
            )
        else:
            pct = self.categorized_share * 100
            headline = (
                f"PARTIALLY CATEGORIZED: {self.unmapped_total:.2f} of "
                f"{self.total_spend:.2f} {self.currency} spent this period belongs to no "
                f"envelope{across}. The table below describes {pct:.0f}% of the period's "
                "spending."
            )
        lines = [headline, "Uncategorized spending sits in:"]
        lines.extend(f"  - {a}" for a in self.unmapped_accounts)
        lines.append("Run `bookkeeper review` to categorize it, then re-run this report.")
        return lines

    def _table_lines(self) -> list[str]:
        name_width = max(
            max((len(e.name) for e in self.envelopes), default=0), len("Envelope")
        )
        header = (
            f"{'Envelope':<{name_width}}  {'Opening':>11}  {'Allocated':>11}  {'Spent':>11}  "
            f"{'Remaining':>11}  {'Closing':>11}  Trend"
        )
        lines = [header, "-" * len(header)]
        for e in self.envelopes:
            # Markers only on the rows that earn one, so the columns stay put
            # and the exceptions are the only thing that stands out.
            marks = []
            if e.over_budget:
                marks.append("OVER BUDGET")
            if e.overspent:
                marks.append("OVERSPENT")
            # The trend column is padded only when something follows it, so a
            # row without a marker does not end in trailing whitespace.
            trend = f"{e.direction:<13}  " + ", ".join(marks) if marks else e.direction
            lines.append(
                f"{e.name:<{name_width}}  {e.opening_balance:>11.2f}  {e.allocated:>11.2f}  "
                f"{e.spent:>11.2f}  {e.remaining:>11.2f}  {e.closing_balance:>11.2f}  {trend}"
            )
        lines.append("-" * len(header))
        lines.append(
            f"{'Total':<{name_width}}  {self.opening_total:>11.2f}  "
            f"{self.allocated_total:>11.2f}  {self.spent_total:>11.2f}  "
            f"{self.allocated_total - self.spent_total:>11.2f}  {self.closing_total:>11.2f}"
        )
        return lines

    def _trend_lines(self) -> list[str]:
        """Unusual transactions, or an explicit account of why none are shown.

        "Nothing looks unusual" is exactly the unfalsifiable one-liner §5.3's
        amendment warns about, so the absence of outliers is never reported
        without saying how many envelopes were actually judged.
        """
        if self.trend_from is None or self.trend_to is None:
            return []
        window = f"{self.trend_from.isoformat()} to {self.trend_to.isoformat()}"
        lines: list[str] = []
        if self.outliers:
            lines.append(f"Unusual for their envelope this period (baseline {window}):")
            for o in self.outliers:
                lines.append(
                    f"  {o.posted_date.isoformat()}  {o.amount:>10.2f}  {o.description} "
                    f"[{o.envelope}]"
                )
                lines.append(
                    f"      {o.score:.1f}x the usual variation — typical is "
                    f"{o.median:.2f}, spread {o.scale:.2f} by {o.scale_method}"
                )
        elif len(self.envelopes) - len(self.unjudged) == 0:
            # "No unusual transactions among the 0 envelopes we checked" is
            # true and reads as reassurance. Nothing was checked; say that.
            lines.append(
                "No envelope had enough history to check for unusual spending this period "
                f"(baseline {window})."
            )
        else:
            judged = len(self.envelopes) - len(self.unjudged)
            lines.append(
                f"No unusual transactions this period among the {judged} envelope(s) with "
                f"enough history to judge (baseline {window})."
            )
        if self.unjudged:
            lines.append(
                "Not checked for unusual spending — too few transactions to have a normal: "
                + ", ".join(self.unjudged)
            )
        return lines

    def _what_moved_lines(self) -> list[str]:
        """The findings, each stated with the figure that makes it checkable.

        No overall verdict is offered. There is no way to tell from here
        whether "you're on track" is true, and it is the kind of sentence a
        reader acts on without checking.
        """
        lines: list[str] = []
        movers = sorted(
            (e for e in self.envelopes if e.spent), key=lambda e: e.spent, reverse=True
        )[:3]
        if movers:
            lines.append(
                "Most spent this period: " + ", ".join(f"{e.name} {e.spent:.2f}" for e in movers)
            )
        for e in self.over_budget_envelopes:
            lines.append(
                f"{e.name} spent {e.spent:.2f} against {e.allocated:.2f} allocated this "
                f"period — {e.overspend:.2f} over."
            )
        for e in self.overspent_envelopes:
            lines.append(
                f"{e.name} is overspent: balance {e.closing_balance:.2f}. That money is "
                "already gone and comes out of the next allocation."
            )
        return lines

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["month-end report failed:", *(f"  - {e}" for e in self.errors)])

        lines = [f"Month-end report — {self.label}", "", self._coverage_line()]
        if self.coverage == COVERAGE_FUTURE:
            return "\n".join(lines)

        lines.append("")
        lines.extend(self._categorization_lines())

        if self.envelopes:
            lines.append("")
            lines.extend(self._table_lines())

        lines.append("")
        lines.append(f"Spent this period, all accounts:  {self.total_spend:>12.2f}")
        lines.append(f"  of which filed to an envelope:  {self.spent_total:>12.2f}")
        lines.append(f"  of which uncategorized:         {self.unmapped_total:>12.2f}")
        lines.append("")
        lines.append(f"Cash as of {self.asof.isoformat()}:           {self.budgeted_cash:>12.2f}")
        lines.append(f"Envelope balances (total):        {self.closing_total:>12.2f}")
        lines.append(f"Overspent (total):                {self.total_overspend:>12.2f}")
        lines.append(f"Available to budget:              {self.available:>12.2f}")

        for block in (self._what_moved_lines(), self._trend_lines()):
            if block:
                lines.append("")
                lines.extend(block)

        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def parse_month(value: str) -> tuple[int, int]:
    """`"YYYY-MM"` as `(year, month)`.

    Strict on purpose. The argument reaches here from a chat tool call made by
    an 8B model (§3.3), and a silently-accepted `"July"` or `"2026-7"` would
    produce a confident report about a month nobody asked for.
    """
    parts = (value or "").strip().split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"month must be YYYY-MM, got {value!r}")
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(f"month must be YYYY-MM, got {value!r}") from exc
    if not 1 <= month <= 12:
        raise ValueError(f"month must be between 01 and 12, got {value!r}")
    return year, month


def month_bounds(year: int, month: int) -> tuple[date, date]:
    """First and last day of the month, both inclusive."""
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _months_before(year: int, month: int, count: int) -> tuple[int, int]:
    """The (year, month) `count` months earlier. Clamped at `date.min`'s year."""
    index = year * 12 + (month - 1) - count
    return max(index // 12, date.min.year), index % 12 + 1


def _ledger_bounds(entries: list[Any]) -> tuple[date, date] | None:
    """First and last transaction dates in the ledger, or `None` if empty."""
    dates = [e.date for e in entries if isinstance(e, Transaction)]
    return (min(dates), max(dates)) if dates else None


def _ledger_last_transaction(entries: list[Any]) -> date | None:
    bounds = _ledger_bounds(entries)
    return bounds[1] if bounds else None


def _transaction_dates_in(entries: list[Any], start: date, end: date) -> list[date]:
    return [e.date for e in entries if isinstance(e, Transaction) and start <= e.date <= end]


def _default_month(entries: list[Any], today: date) -> tuple[int, int]:
    """The month to report on when the caller did not name one.

    The ledger's own last transaction, not the wall clock — the same reasoning
    `reports.spending` defaults its window off the ledger's bounds: a fixed
    ledger must not start reporting an empty month because a day passed, and
    a demo ledger's data does not move.
    """
    last = _ledger_last_transaction(entries)
    return (last.year, last.month) if last else (today.year, today.month)


def _classify_coverage(
    month_start: date,
    month_end: date,
    today: date,
    dates_in_month: list[date],
    ledger_last: date | None,
) -> str:
    if month_start > today:
        return COVERAGE_FUTURE
    if month_end >= today:
        # The current month, whether or not anything is in it yet: "nothing so
        # far this month" is a different statement from "this month has no
        # data", and only the first is true on the 1st.
        return COVERAGE_IN_PROGRESS
    if not dates_in_month:
        return COVERAGE_NO_DATA
    if ledger_last is not None and ledger_last < month_end:
        return COVERAGE_PARTIAL
    return COVERAGE_COMPLETE


def _coverage_through(
    month_start: date, asof: date, bounds: tuple[date, date] | None
) -> date | None:
    """How far into the reported month the ledger can actually speak.

    `asof` whenever the ledger runs to or past it — a month whose last
    transaction is the 28th is still covered to the 31st, because the ledger
    continuing into February is evidence that nothing happened in those three
    days rather than that nobody looked.

    `None` rather than a guess when nothing is covered: the month has not
    started, or it begins after the ledger's last transaction. A date invented
    to keep the field non-null would be a confident claim about coverage that
    does not exist.
    """
    if asof < month_start:
        return None
    if bounds is None:
        return None
    if bounds[1] >= asof:
        return asof
    return bounds[1] if bounds[1] >= month_start else None


def _elapsed_days(month_start: date, month_end: date, asof: date) -> tuple[int, int]:
    """`(days_elapsed, days_in_month)` as of `asof`, both inclusive counts."""
    days_in_month = (month_end - month_start).days + 1
    if asof < month_start:
        return 0, days_in_month
    return min((asof - month_start).days + 1, days_in_month), days_in_month


def _categorization_counts(
    entries: list[Any], account_to_envelope: dict[str, str], start: date, end: date
) -> tuple[int, int]:
    """`(categorized, uncategorized)` transaction counts in `[start, end]`.

    Counting, not money — which is why it is done here rather than asked of
    `budget`, and why doing it here creates no drift risk: no figure produced
    by this function can disagree with an amount computed elsewhere.

    What it does *not* decide locally is which accounts belong to an envelope.
    That comes from `account_to_envelope`, built from the ledger's own `map`
    directives, so "categorized" means exactly what it means everywhere else.

    A transaction is counted once per side it touches, and can touch both: a
    split with one mapped and one unmapped leg is genuinely half-filed, and
    forcing it into one bucket would either overstate the work done or
    overstate the work left.
    """
    categorized = uncategorized = 0
    for entry in entries:
        if not isinstance(entry, Transaction) or not start <= entry.date <= end:
            continue
        accounts = [p.account for p in entry.postings if p.units is not None]
        if any(a in account_to_envelope for a in accounts):
            categorized += 1
        if any(
            a not in account_to_envelope and a.startswith(EXPENSE_ACCOUNT_PREFIX)
            for a in accounts
        ):
            uncategorized += 1
    return categorized, uncategorized


def _classify_categorization(spent: Decimal, unmapped: Decimal) -> str:
    if spent + unmapped <= 0:
        return CATEGORIZATION_NO_SPEND
    if unmapped <= 0:
        return CATEGORIZATION_FULL
    if spent <= 0:
        return CATEGORIZATION_NONE
    return CATEGORIZATION_PARTIAL


def _join_trends(budget: Any, trends: Any) -> tuple[MonthEndEnvelope, ...]:
    """Budget lines joined to their trend verdict, by envelope name.

    An envelope the trends run did not cover — because it is new, or because
    the run failed entirely — gets `DIRECTION_UNKNOWN` with the reason
    attached, never `flat`. "We measured it and it was flat" and "we could not
    tell" are different answers and only one of them is reassuring.
    """
    direction = {t.name: t for t in getattr(trends, "envelopes", ())}
    rows = []
    for line in budget.envelopes:
        trend = direction.get(line.name)
        rows.append(
            MonthEndEnvelope(
                name=line.name,
                opening_balance=line.carried_in,
                allocated=line.allocated,
                spent=line.spent,
                closing_balance=line.balance,
                remaining=line.remaining,
                overspend=line.overspend,
                consumed_ratio=line.consumed_ratio,
                status=line.status,
                direction=trend.direction if trend else DIRECTION_UNKNOWN,
                direction_reason=(
                    trend.reason if trend else "no trend was computed for this envelope"
                ),
                # Carried so an abstention is interrogable from the payload
                # rather than only from the prose: "2 of 3 periods" is a fact
                # a caller can act on, "insufficient_data" alone is not.
                periods_observed=trend.periods_observed if trend else 0,
                periods_required=trend.periods_required if trend else 0,
            )
        )
    return tuple(rows)


def month_end_report(
    month: str | None = None,
    *,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> MonthEndReport:
    """The composite month-end report for `month` (`"YYYY-MM"`).

    `month` defaults to the month of the ledger's last transaction.
    `entries`/`errors`/`options` let the sidecar feed its mtime-cached ledger
    in instead of reloading it (§5.1).

    Raises `ValueError` on an unparseable month; the caller decides whether
    that is a 422 or a CLI message, exactly as `spending_report` does.
    """
    if entries is None:
        entries, errors, options = load_ledger()

    today = coerce_asof(None)
    year, number = parse_month(month) if month is not None else _default_month(entries, today)
    month_start, month_end = month_bounds(year, number)
    currency = str((options or {}).get("operating_currency", [_FALLBACK_CURRENCY])[0])

    bounds = _ledger_bounds(entries)
    dates_in_month = _transaction_dates_in(entries, month_start, month_end)
    coverage = _classify_coverage(
        month_start, month_end, today, dates_in_month, bounds[1] if bounds else None
    )

    # Clamped to today so an in-progress month reports what has happened, not
    # what is scheduled. A future month collapses to a zero-width window at
    # today, which is correct: nothing has happened in it.
    asof = min(month_end, today)
    window_start = min(month_start, asof)
    through = _coverage_through(month_start, asof, bounds)
    days_elapsed, days_in_month = _elapsed_days(month_start, month_end, asof)

    budget = budget_report(
        window_start, asof, entries=entries, errors=errors, options=options
    )
    if not budget.ok:
        return MonthEndReport(
            ok=False,
            month=f"{year:04d}-{number:02d}",
            label=f"{MONTH_NAMES[number - 1]} {year}",
            from_date=month_start,
            to_date=month_end,
            asof=asof,
            # Coverage still holds on a failed report: which month was asked
            # for, and how much of it exists, are answerable even when the
            # figures are not, and a caller showing the error still has to
            # name the period it applies to.
            through=through,
            days_elapsed=days_elapsed,
            days_in_month=days_in_month,
            currency=currency,
            errors=[f"budget figures are unavailable: {e}" for e in budget.errors],
        )

    closing = compute_envelope_state(entries, asof)
    warnings = [f"budget: {w}" for w in budget.warnings]

    # Direction needs more than one month; outliers need a baseline wider than
    # the thing being judged. Both come from one trailing-window run, and the
    # outliers are then filtered to the reported month.
    #
    # The window is clamped to the ledger's first transaction, and that clamp
    # is not cosmetic: months before the ledger begins contain no spending,
    # and `trends` cannot distinguish "nothing was spent" from "nothing was
    # recorded". Left unclamped, a ledger that starts in January makes every
    # envelope "rising" in January — including a rent that has never changed —
    # because five empty months precede it. An abstention on a short history
    # is the correct answer; a confident wrong direction is the failure mode
    # §5.3's amendment is about.
    trend_year, trend_month = _months_before(year, number, TRAILING_MONTHS - 1)
    trend_start = month_bounds(trend_year, trend_month)[0]
    if bounds is not None:
        trend_start = max(trend_start, bounds[0])
    trends = trends_report(
        min(trend_start, asof), asof, entries=entries, errors=errors, options=options
    )
    if not trends.ok:
        warnings.extend(f"trends unavailable: {e}" for e in trends.errors)

    outliers = tuple(
        o for o in getattr(trends, "outliers", ()) if month_start <= o.posted_date <= month_end
    )
    unjudged = tuple(
        sorted(a.envelope for a in getattr(trends, "assessments", ()) if not a.judged)
    )

    # `budget.ok` means the mapping already parsed, so this cannot raise here;
    # it is re-read rather than passed through because `BudgetReport` does not
    # carry it, and re-deriving it from the same directives is not a second
    # definition — it is the same one.
    account_to_envelope = build_account_map(parse_envelope_directives(entries).maps)
    categorized_count, uncategorized_count = _categorization_counts(
        entries, account_to_envelope, month_start, min(month_end, asof)
    )

    envelopes = _join_trends(budget, trends)
    return MonthEndReport(
        ok=True,
        month=f"{year:04d}-{number:02d}",
        label=f"{MONTH_NAMES[number - 1]} {year}",
        from_date=month_start,
        to_date=month_end,
        asof=asof,
        coverage=coverage,
        data_through=max(dates_in_month) if dates_in_month else None,
        through=through,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        transactions=len(dates_in_month),
        categorized_count=categorized_count,
        uncategorized_count=uncategorized_count,
        currency=budget.currency,
        envelopes=envelopes,
        opening_total=sum((e.opening_balance for e in envelopes), Decimal(0)),
        allocated_total=budget.total_allocated,
        spent_total=budget.total_spent,
        # From the envelope engine, not summed here: this is the figure
        # `/envelopes` prints, and the two must not be able to disagree.
        closing_total=closing.total_envelope_balance,
        unmapped_total=budget.unmapped_total,
        unmapped_accounts=tuple(budget.unmapped_accounts),
        categorization=_classify_categorization(budget.total_spent, budget.unmapped_total),
        budgeted_cash=closing.budgeted_cash,
        available=closing.available,
        total_overspend=closing.total_overspend,
        outliers=outliers,
        trend_from=trends.from_date if trends.ok else None,
        trend_to=trends.to_date if trends.ok else None,
        unjudged=unjudged,
        warnings=warnings,
    )
