"""Unit tests for `month_end_report`.

Two properties matter more than the rest, and most of this file is about
them.

**It must not lie about an uncategorized month.** Auto-apply is off, so today
essentially all spend posts to `Expenses:Unknown`, which no envelope claims.
The failure this report is most likely to commit is rendering a month of real
spending as a month of zeros, because the per-envelope table is genuinely all
zeros and looks complete. So there are tests that the money is still counted,
that the state is named, and that the render says so *before* the table.

**"For March" and "so far this month" are different claims.** A user acts on
them differently, and a report that phrased a part-month as a finished one
would be believed. Each coverage state is asserted separately, including the
two ways a month can be empty — no data recorded, and not yet begun.

The composition is also tested as composition: the per-envelope figures must
equal what `budget_report` and `compute_envelope_state` say over the same
window, because the entire reason this module does no arithmetic of its own is
that a second definition of "spent on Groceries in March" would drift from
`/envelopes`. A test that only checked the numbers were *plausible* would let
that drift through.

Everything runs against a ledger built with `loader.load_string` and injected
via the `entries`/`errors`/`options` seam, so no filesystem is involved and
the real `ledger/` is unreachable from here.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from beancount import loader

from bookkeeper.envelope.compute import compute_envelope_state
from bookkeeper.reports.budget import budget_report
from bookkeeper.reports.monthend import (
    CATEGORIZATION_FULL,
    CATEGORIZATION_NO_SPEND,
    CATEGORIZATION_NONE,
    CATEGORIZATION_PARTIAL,
    COVERAGE_COMPLETE,
    COVERAGE_FUTURE,
    COVERAGE_IN_PROGRESS,
    COVERAGE_NO_DATA,
    COVERAGE_PARTIAL,
    DIRECTION_UNKNOWN,
    TRAILING_MONTHS,
    month_bounds,
    month_end_report,
    parse_month,
)
from bookkeeper.reports.trends import DIRECTIONS

#: The six months the main fixture covers, oldest first. Six because
#: `TRAILING_MONTHS` is six: the trend window for the last of them is exactly
#: the fixture, so the direction verdicts are over known data rather than over
#: a window that runs off the front of the ledger.
MONTHS = ((2025, 10), (2025, 11), (2025, 12), (2026, 1), (2026, 2), (2026, 3))

HEADER = """\
option "title" "month-end fixture"
option "operating_currency" "USD"

2025-10-01 open Equity:Opening-Balances
2025-10-01 open Assets:Checking            USD
2025-10-01 open Expenses:Housing:Rent      USD
2025-10-01 open Expenses:Transport:Gas     USD
2025-10-01 open Expenses:Food:Groceries    USD
2025-10-01 open Expenses:Unknown           USD

2025-10-01 custom "envelope" "map" "Expenses:Housing:Rent"    "Rent"
2025-10-01 custom "envelope" "map" "Expenses:Transport:Gas"   "Transport"
2025-10-01 custom "envelope" "map" "Expenses:Food:Groceries"  "Groceries"

2025-10-01 * "Opening balance"
    Assets:Checking          20000.00 USD
    Equity:Opening-Balances
"""

#: Transport rises month over month while rent does not, so `up` and `flat`
#: are both exercised against data whose direction is known by construction
#: rather than asserted from whatever the fixture happened to do.
_TRANSPORT = ("20.00", "40.00", "60.00", "80.00", "90.00", "95.00")


def _build_ledger() -> str:
    """Six months of transactions, generated rather than hand-written.

    Forty-odd near-identical postings typed out by hand is where an
    off-by-one in a date or a transposed amount hides, and the tests below
    assert on totals that such a typo would silently change.
    """
    parts = [HEADER]
    for index, (year, month) in enumerate(MONTHS):
        parts.append(
            f'{year:04d}-{month:02d}-01 custom "envelope" "allocate" "Rent"       1000.00 USD\n'
            f'{year:04d}-{month:02d}-01 custom "envelope" "allocate" "Transport"   100.00 USD\n'
            f'{year:04d}-{month:02d}-01 custom "envelope" "allocate" "Groceries"   700.00 USD\n'
        )
        parts.append(
            f'{year:04d}-{month:02d}-03 * "Rent"\n'
            "    Assets:Checking          -1000.00 USD\n"
            "    Expenses:Housing:Rent\n"
        )
        parts.append(
            f'{year:04d}-{month:02d}-10 * "Gas"\n'
            f"    Assets:Checking          -{_TRANSPORT[index]} USD\n"
            "    Expenses:Transport:Gas\n"
        )
        for day, amount in (("05", "50.00"), ("19", "60.00")):
            parts.append(
                f'{year:04d}-{month:02d}-{day} * "Groceries"\n'
                f"    Assets:Checking            -{amount} USD\n"
                "    Expenses:Food:Groceries\n"
            )
    # One transaction far outside the Groceries norm, in the last month, so
    # the outlier path runs against something an outlier detector should
    # actually catch rather than against noise.
    parts.append(
        '2026-03-21 * "APPLIANCE WAREHOUSE"\n'
        "    Assets:Checking            -900.00 USD\n"
        "    Expenses:Food:Groceries\n"
    )
    return "\n".join(parts)


#: Envelopes and allocations exist, but every posting lands in
#: `Expenses:Unknown`. This is the shipped state of the system: auto-apply is
#: off, so nothing has been filed yet.
UNCATEGORIZED_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
2026-01-01 open Expenses:Food:Groceries     USD
2026-01-01 open Expenses:Unknown            USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
2026-01-01 custom "envelope" "allocate" "Groceries" 400.00 USD

2026-01-01 * "Opening balance"
    Assets:Checking            2000.00 USD
    Equity:Opening-Balances

2026-01-05 * "SAFEWAY #1842"
    Assets:Checking             -80.00 USD
    Expenses:Unknown

2026-01-12 * "BLUE BOTTLE COFFEE"
    Assets:Checking             -25.00 USD
    Expenses:Unknown

2026-01-20 * "CHEVRON"
    Assets:Checking             -45.00 USD
    Expenses:Unknown
"""

#: Half filed, half not — the state a partly-reviewed month is actually in.
PARTIAL_LEDGER = UNCATEGORIZED_LEDGER + """
2026-01-25 * "TRADER JOES"
    Assets:Checking             -50.00 USD
    Expenses:Food:Groceries
"""

EMPTY_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Assets:Checking             USD
"""

#: 10.00 filed of 30.00 spent, so the categorized share is a repeating
#: decimal and full `Decimal` precision would show as 28 significant digits.
REPEATING_SHARE_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
2026-01-01 open Expenses:Food:Groceries     USD
2026-01-01 open Expenses:Unknown            USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
2026-01-01 custom "envelope" "allocate" "Groceries" 100.00 USD

2026-01-01 * "Opening balance"
    Assets:Checking            1000.00 USD
    Equity:Opening-Balances

2026-01-05 * "Filed"
    Assets:Checking             -10.00 USD
    Expenses:Food:Groceries

2026-01-06 * "Not filed"
    Assets:Checking             -20.00 USD
    Expenses:Unknown
"""

#: January and March, nothing in February. February is genuinely finished and
#: genuinely empty -- the case where `coverage` and `complete` come apart.
GAPPED_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
2026-01-01 open Expenses:Food:Groceries     USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
2026-01-01 custom "envelope" "allocate" "Groceries" 400.00 USD

2026-01-01 * "Opening balance"
    Assets:Checking            2000.00 USD
    Equity:Opening-Balances

2026-01-15 * "Groceries"
    Assets:Checking             -60.00 USD
    Expenses:Food:Groceries

2026-03-15 * "Groceries"
    Assets:Checking             -70.00 USD
    Expenses:Food:Groceries
"""


def _load(text):
    entries, errors, options = loader.load_string(text)
    assert not errors, errors
    return entries, errors, options


@pytest.fixture(scope="module")
def ledger():
    return _load(_build_ledger())


@pytest.fixture(scope="module")
def uncategorized():
    return _load(UNCATEGORIZED_LEDGER)


@pytest.fixture(scope="module")
def partly_categorized():
    return _load(PARTIAL_LEDGER)


@pytest.fixture(scope="module")
def empty():
    return _load(EMPTY_LEDGER)


@pytest.fixture(scope="module")
def gapped():
    return _load(GAPPED_LEDGER)


def report(ledger, month=None):
    entries, errors, options = ledger
    return month_end_report(month, entries=entries, errors=errors, options=options)


def envelope(result, name):
    return next(e for e in result.envelopes if e.name == name)


def today() -> date:
    """The same UTC day `coerce_asof(None)` resolves to.

    Not `date.today()`: the sidecar may not run in the host's timezone, and a
    test that disagreed with the module about what day it is would fail once
    a day for a few hours.
    """
    return datetime.now(UTC).date()


# --- the month argument ---------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["july", "2026", "2026-1", "2026-13", "2026-00", "26-01", "2026-01-01", "", "   ", None],
)
def test_a_month_that_is_not_yyyy_mm_is_refused_not_guessed(bad):
    """The argument arrives from an 8B model. A silently-accepted `"July"`
    would produce a confident report about a month nobody asked for."""
    with pytest.raises(ValueError, match="month must be"):
        parse_month(bad)


def test_month_end_raises_on_an_unparseable_month_so_the_caller_can_choose(ledger):
    """`ValueError`, not a failed result: the CLI turns it into a message and
    the API into a 422, exactly as `spending_report` does."""
    with pytest.raises(ValueError):
        report(ledger, "last july")


def test_the_default_month_is_the_ledgers_last_not_the_wall_clock(ledger):
    """A fixed ledger must not start reporting an empty month because a day
    passed -- the reasoning `reports.spending` defaults its window off the
    ledger's own bounds."""
    assert report(ledger).month == "2026-03"


def test_an_empty_ledger_defaults_to_the_current_month(empty):
    now = today()
    assert report(empty).month == f"{now.year:04d}-{now.month:02d}"


# --- coverage: five different claims --------------------------------------


def test_a_finished_month_with_data_either_side_is_complete(ledger):
    result = report(ledger, "2026-01")

    assert result.coverage == COVERAGE_COMPLETE
    assert result.from_date == date(2026, 1, 1)
    assert result.to_date == date(2026, 1, 31)
    assert result.asof == date(2026, 1, 31)
    assert "complete" in result.render()


def test_a_finished_month_the_ledger_stops_inside_is_partial(ledger):
    """The ledger's last transaction is 2026-03-21, so the last ten days of
    March are missing, not empty. A user reading that as a quiet month would
    conclude they had stopped spending."""
    result = report(ledger, "2026-03")

    assert result.coverage == COVERAGE_PARTIAL
    assert result.data_through == date(2026, 3, 21)
    rendered = result.render()
    assert "2026-03-21" in rendered
    assert "missing, not empty" in rendered


def test_a_finished_month_with_no_transactions_is_no_data(ledger):
    """Distinct from `partial`: the ledger continues past this month, so the
    month really is empty rather than unsynced."""
    result = report(ledger, "2025-11")
    assert result.transactions > 0  # self-check: November does have data

    empty_month = month_end_report(
        "2025-09", entries=ledger[0], errors=ledger[1], options=ledger[2]
    )
    assert empty_month.coverage == COVERAGE_NO_DATA
    assert empty_month.transactions == 0
    assert empty_month.data_through is None
    assert "no transactions" in empty_month.render()


def test_the_current_month_is_in_progress_and_says_so_far(ledger):
    now = today()
    result = report(ledger, f"{now.year:04d}-{now.month:02d}")

    assert result.coverage == COVERAGE_IN_PROGRESS
    rendered = result.render()
    assert "So far this month" in rendered
    assert "not a final month" in rendered


def test_an_in_progress_month_is_computed_as_of_today_not_month_end(ledger):
    """The clamp that stops an allocation dated the 31st from appearing in a
    report run on the 5th. `to_date` still describes the whole month, because
    that is the month being reported on."""
    now = today()
    result = report(ledger, f"{now.year:04d}-{now.month:02d}")

    assert result.asof == now
    assert result.to_date == date(now.year, now.month, calendar.monthrange(now.year, now.month)[1])
    assert result.asof <= result.to_date


def test_a_month_that_has_not_started_says_so_rather_than_reporting_zeros(ledger):
    """"No data" and "hasn't happened yet" are different answers, and only one
    of them is a reason to go and sync."""
    later = today() + timedelta(days=400)
    result = report(ledger, f"{later.year:04d}-{later.month:02d}")

    assert result.coverage == COVERAGE_FUTURE
    rendered = result.render()
    assert "has not started" in rendered
    # No table, no totals: there is nothing to tabulate, and a table of zeros
    # is exactly what a reader would mistake for a month of no spending.
    assert "Envelope" not in rendered
    assert "Available to budget" not in rendered


# --- coverage, as the fields a card renders from --------------------------


def test_a_finished_month_the_ledger_covers_is_complete(ledger):
    """`complete` is what stops a part-month rendering as a finished one."""
    result = report(ledger, "2026-01")

    assert result.complete is True
    assert result.through == date(2026, 1, 31)
    assert result.days_elapsed == result.days_in_month == 31


def test_coverage_runs_to_month_end_even_when_the_last_transaction_is_earlier(ledger):
    """A January whose last transaction is the 19th is still covered to the
    31st: the ledger continuing into February is evidence that nothing
    happened in those days, not that nobody looked. `data_through` and
    `through` answer different questions and must not be conflated."""
    result = report(ledger, "2026-01")

    assert result.data_through == date(2026, 1, 19)
    assert result.through == date(2026, 1, 31)
    assert result.complete is True


def test_a_month_the_ledger_stops_inside_is_not_complete(ledger):
    result = report(ledger, "2026-03")

    assert result.complete is False
    assert result.through == date(2026, 3, 21)
    assert result.through < result.to_date


def test_an_empty_month_the_ledger_spans_is_still_complete(gapped):
    """`coverage` says *why* a month looks the way it does; `complete` says
    whether the figures are done moving. These come apart here and both
    answers are right: February has no transactions, so `coverage` is
    `no-data`, but the ledger runs either side of it, so nothing about
    February is still to arrive and `complete` is True.

    A caller asking "can I treat these figures as final" gets `True`; a
    caller asking "why is this empty" gets `no-data`. Collapsing the two
    would lose one of those answers.
    """
    result = report(gapped, "2026-02")

    assert result.transactions == 0
    assert result.coverage == COVERAGE_NO_DATA
    assert result.complete is True
    assert result.through == date(2026, 2, 28)


def test_a_month_beginning_after_the_ledger_ends_covers_nothing(ledger):
    """`None` rather than a date invented to keep the field non-null. A
    fabricated coverage date is exactly the confident-but-wrong claim this
    report exists to avoid."""
    result = report(ledger, "2026-06")

    assert result.through is None
    assert result.complete is False
    assert result.transactions == 0


def test_an_in_progress_month_counts_the_days_elapsed(ledger):
    """"day 4 of 31" is what lets a card avoid rendering four days of August
    exactly like a finished July."""
    now = today()
    result = report(ledger, f"{now.year:04d}-{now.month:02d}")

    assert result.complete is False
    assert result.days_elapsed == now.day
    assert result.days_in_month == calendar.monthrange(now.year, now.month)[1]
    assert f"day {now.day} of {result.days_in_month}" in result.render()


def test_a_month_that_has_not_started_has_no_days_elapsed(ledger):
    later = today() + timedelta(days=400)
    result = report(ledger, f"{later.year:04d}-{later.month:02d}")

    assert result.days_elapsed == 0
    assert result.days_in_month > 0
    assert result.through is None
    assert result.complete is False


# --- the uncategorized month, which is the shipped state ------------------


def test_an_uncategorized_month_is_named_as_such_not_rendered_as_zeros(uncategorized):
    """The single most likely way this report lies to someone.

    Every envelope figure is legitimately zero, and a report that stopped
    there would show a month with 150.00 of spending as a month with none.
    """
    result = report(uncategorized, "2026-01")

    assert result.categorization == CATEGORIZATION_NONE
    assert result.spent_total == Decimal(0)
    assert result.unmapped_total == Decimal("150.00")
    # The money is still counted. This is the assertion that matters.
    assert result.total_spend == Decimal("150.00")
    assert result.categorized_share == Decimal(0)
    assert result.unmapped_accounts == ("Expenses:Unknown",)


def test_the_uncategorized_headline_precedes_the_table_it_qualifies(uncategorized):
    """A qualification a reader meets *after* forming a conclusion from the
    table has already failed."""
    rendered = report(uncategorized, "2026-01").render()

    assert "THIS PERIOD IS NOT CATEGORIZED" in rendered
    assert rendered.index("NOT CATEGORIZED") < rendered.index("Envelope")
    assert "Expenses:Unknown" in rendered
    # It must explain the zeros rather than leave them to be misread.
    assert "not because the money was not spent" in rendered


def test_the_uncategorized_amount_travels_with_a_transaction_count(uncategorized):
    """The amount alone is not enough. "$0.00 in envelopes" and "150.00 across
    3 transactions nobody has filed" are the same underlying state, and only
    the second reads as work to do -- which is what stops an authoritative
    table of zeros passing for a quiet month."""
    result = report(uncategorized, "2026-01")

    assert result.uncategorized_count == 3
    assert result.categorized_count == 0
    assert isinstance(result.uncategorized_count, int)
    assert "across 3 transaction(s)" in result.render()


def test_both_counts_are_reported_when_a_month_is_half_filed(partly_categorized):
    result = report(partly_categorized, "2026-01")

    assert result.uncategorized_count == 3
    assert result.categorized_count == 1


def test_only_expense_legs_can_be_uncategorized(uncategorized):
    """The opening balance is a funding-and-equity transaction with no expense
    leg. Counting it would make every paycheck an uncategorized transaction
    and put the count permanently, quietly wrong."""
    result = report(uncategorized, "2026-01")

    assert result.transactions == 4  # opening balance + three spends
    assert result.uncategorized_count == 3
    assert result.categorized_count + result.uncategorized_count < result.transactions


def test_the_counts_use_the_ledgers_own_envelope_mapping(ledger):
    """"Categorized" has to mean what it means everywhere else, so the map
    comes from the `map` directives rather than from a rule written here."""
    result = report(ledger, "2026-02")

    assert result.uncategorized_count == 0
    assert result.categorized_count == 4  # rent, gas, two grocery runs
    assert result.categorized_count == result.transactions


def test_a_partly_categorized_month_reports_the_share_it_describes(partly_categorized):
    result = report(partly_categorized, "2026-01")

    assert result.categorization == CATEGORIZATION_PARTIAL
    assert result.spent_total == Decimal("50.00")
    assert result.unmapped_total == Decimal("150.00")
    assert result.total_spend == Decimal("200.00")
    assert result.categorized_share == Decimal("0.2500")
    assert "25%" in result.render()


def test_the_categorized_share_arrives_rounded_not_at_full_precision():
    """The tool and browser layers must not do arithmetic on money-derived
    figures, so a ratio arriving at `Decimal`'s 28 significant digits forces
    them to do the one thing they are forbidden. It has to arrive final.

    10 of 30 is a repeating decimal -- the case that exposes the default and
    that an exact ratio like 50/200 would not.
    """
    result = report(_load(REPEATING_SHARE_LEDGER), "2026-01")

    assert result.spent_total == Decimal("10.00")
    assert result.total_spend == Decimal("30.00")
    assert result.categorized_share == Decimal("0.3333")
    assert -result.categorized_share.as_tuple().exponent == 4


def test_the_categorized_share_has_one_serialized_shape(ledger, uncategorized, empty):
    """`"0"` from the no-spend branch beside `"0.6154"` from the other invites
    a client to compare the string against `"0"` -- which then works for a
    month with no spending and fails for one whose share rounds to nothing."""
    for fixture, month in ((ledger, "2026-02"), (uncategorized, "2026-01"), (empty, "2026-01")):
        share = report(fixture, month).categorized_share
        assert -share.as_tuple().exponent == 4, f"{month}: {share!r}"
        assert str(share) == f"{share:.4f}"


#: A number written in exponent form, e.g. `0E+2` or `-1.5e-3`. Matched as a
#: whole string, so prose that merely contains an "e" is not a false positive.
EXPONENT_FORM = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")


def _string_values(value, path=""):
    """Every string in a nested payload, with the path that reached it."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _string_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _string_values(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


@pytest.mark.parametrize("month", ["2026-02", "2025-09", "2026-03", "2027-01"])
def test_no_figure_leaves_this_module_in_exponent_notation(ledger, uncategorized, month):
    """`Decimal` division takes the dividend's exponent less the divisor's, so
    `Decimal(0) / Decimal("150.00")` is `Decimal("0E+2")` and serializes as
    the string `"0E+2"`. That parses as a number and compares as garbage, and
    it reached the browser once before quantizing pinned the exponent.

    Every string in the payload is checked rather than the one field that was
    caught, because the next one will be a different field.
    """
    for fixture in (ledger, uncategorized):
        payload = report(fixture, month).to_dict()
        offenders = [
            (path, text)
            for path, text in _string_values(payload)
            if EXPONENT_FORM.match(text)
        ]
        assert offenders == [], f"{month}: {offenders}"


def test_a_fully_categorized_month_says_so_without_a_warning_banner(ledger):
    result = report(ledger, "2026-01")

    assert result.categorization == CATEGORIZATION_FULL
    assert result.unmapped_total == Decimal(0)
    rendered = result.render()
    assert "NOT CATEGORIZED" not in rendered
    assert "filed to an envelope" in rendered


def test_a_month_with_no_spending_is_not_a_month_with_no_categorization(ledger):
    """Zero spend and zero *categorized* spend look identical in the figures
    and mean opposite things."""
    result = report(ledger, "2025-09")

    assert result.categorization == CATEGORIZATION_NO_SPEND
    assert "No spending recorded" in result.render()


# --- composition: the figures must be the other modules' figures ----------


def test_per_envelope_figures_are_budget_reports_own(ledger):
    """Not "close to" -- identical. Two definitions of "spent on Groceries in
    March" would drift, and the drift would surface as a month-end report that
    disagrees with `/envelopes` about the user's own money."""
    entries, errors, options = ledger
    result = report(ledger, "2026-02")
    budget = budget_report(
        date(2026, 2, 1), date(2026, 2, 28), entries=entries, errors=errors, options=options
    )

    assert {e.name for e in result.envelopes} == {line.name for line in budget.envelopes}
    for line in budget.envelopes:
        row = envelope(result, line.name)
        assert row.allocated == line.allocated
        assert row.spent == line.spent
        assert row.opening_balance == line.carried_in
        assert row.closing_balance == line.balance
        assert row.remaining == line.remaining
        assert row.status == line.status
    assert result.allocated_total == budget.total_allocated
    assert result.spent_total == budget.total_spent
    assert result.unmapped_total == budget.unmapped_total


def test_the_cash_summary_is_the_envelope_engines_own(ledger):
    """`budgeted_cash` / `available` / `total_overspend` are what `/envelopes`
    prints for the same date, not a second reading of the ledger."""
    entries, _errors, _options = ledger
    result = report(ledger, "2026-02")
    state = compute_envelope_state(entries, date(2026, 2, 28))

    assert result.budgeted_cash == state.budgeted_cash
    assert result.available == state.available
    assert result.total_overspend == state.total_overspend
    assert result.closing_total == state.total_envelope_balance


def test_the_table_adds_up_opening_plus_allocated_less_spent(ledger):
    """The identity that makes the printed table checkable by eye. It holds by
    construction, which is the point: it would stop holding the moment any of
    these four figures were computed here instead of composed."""
    result = report(ledger, "2026-02")

    for row in result.envelopes:
        assert row.opening_balance + row.allocated - row.spent == row.closing_balance
    assert (
        result.opening_total + result.allocated_total - result.spent_total
        == result.closing_total
    )


# --- budget vs actual: two different failures -----------------------------


def test_over_budget_and_overspent_are_reported_separately(ledger):
    """Groceries spends 1010.00 against 700.00 allocated in March, but has
    carried enough from earlier months that its balance stays positive. One
    envelope, over budget, not overspent -- collapsing the two would raise a
    false alarm here and hide a real one elsewhere."""
    groceries = envelope(report(ledger, "2026-03"), "Groceries")

    assert groceries.spent == Decimal("1010.00")
    assert groceries.allocated == Decimal("700.00")
    assert groceries.over_budget is True
    assert groceries.overspend == Decimal("310.00")
    assert groceries.overspent is False
    assert groceries.closing_balance > 0


def test_an_envelope_spending_exactly_its_allocation_is_not_over_budget(ledger):
    rent = envelope(report(ledger, "2026-02"), "Rent")

    assert rent.spent == rent.allocated == Decimal("1000.00")
    assert rent.over_budget is False
    assert rent.status == "within"


def test_the_findings_cite_the_figures_they_were_read_from(ledger):
    """"You're on track" is one short unverifiable sentence a reader acts on.
    Every finding here is checkable against the table above it."""
    rendered = report(ledger, "2026-03").render()

    assert "Groceries spent 1010.00 against 700.00 allocated" in rendered
    assert "310.00 over" in rendered
    assert "Most spent this period" in rendered


# --- trends: composed, and made to abstain when it should -----------------


def test_direction_is_read_over_a_trailing_window_not_the_month_alone(ledger):
    """A single month is one data point; `trends` would rightly abstain on it.
    The window ends with the reported month and runs `TRAILING_MONTHS` back."""
    result = report(ledger, "2026-03")

    assert result.trend_to == date(2026, 3, 31)
    assert result.trend_from == date(2025, 10, 1)
    assert (result.trend_to.year * 12 + result.trend_to.month) - (
        result.trend_from.year * 12 + result.trend_from.month
    ) == TRAILING_MONTHS - 1
    assert envelope(result, "Transport").direction == "up"
    assert envelope(result, "Rent").direction == "flat"


def test_the_abstention_direction_is_still_the_one_trends_reports():
    """`monthend` spells `DIRECTION_UNKNOWN` out rather than importing a name
    `trends` does not export. This is what stops that from silently becoming a
    verdict `trends` never gave if the vocabulary is renamed again."""
    assert DIRECTION_UNKNOWN in DIRECTIONS


def test_the_trend_window_never_runs_off_the_front_of_the_ledger(ledger):
    """Months before the ledger begins hold no spending, and `trends` cannot
    tell "nothing was spent" from "nothing was recorded". Unclamped, the five
    empty months before a ledger that starts in October would make every
    envelope -- including a rent that has never changed -- read as trending
    up. Verified: unclamped, this fixture returns `up` for all three."""
    result = report(ledger, "2025-10")

    assert result.trend_from == date(2025, 10, 1)
    assert envelope(result, "Rent").direction == DIRECTION_UNKNOWN
    assert "up" not in {e.direction for e in result.envelopes}


def test_an_envelope_that_could_not_be_judged_abstains_rather_than_reading_flat(ledger):
    """"We measured it and it was flat" and "we could not tell" are different
    answers, and only one of them is reassuring."""
    rent = envelope(report(ledger, "2025-10"), "Rent")

    assert rent.direction == DIRECTION_UNKNOWN
    assert rent.direction != "flat"
    assert rent.direction_reason


def test_outliers_are_limited_to_the_reported_month(ledger):
    """Judged against the trailing baseline, shown for the month being
    reported: a month-end report that listed February's surprises under March
    would send someone looking in the wrong place."""
    result = report(ledger, "2026-03")

    assert [o.description for o in result.outliers] == ["APPLIANCE WAREHOUSE"]
    assert all(date(2026, 3, 1) <= o.posted_date <= date(2026, 3, 31) for o in result.outliers)
    assert result.outliers[0].amount == Decimal("900.00")


def test_no_outliers_is_reported_with_how_many_envelopes_were_checked(ledger):
    """"Nothing looks unusual" is the unfalsifiable one-liner §5.3's amendment
    is about. The absence has to come with its own denominator."""
    result = report(ledger, "2026-02")
    rendered = result.render()

    assert len(result.envelopes) > len(result.unjudged)  # self-check: some were judged
    assert "No unusual transactions this period among the" in rendered
    assert "envelope(s) with enough history to judge" in rendered


def test_checking_nothing_does_not_read_as_finding_nothing(uncategorized):
    """"No unusual transactions among the 0 envelopes we checked" is true, and
    reads as reassurance. When nothing could be checked, say that instead."""
    result = report(uncategorized, "2026-01")

    assert result.outliers == ()
    assert len(result.envelopes) == len(result.unjudged)  # nothing was judged
    rendered = result.render()
    assert "No envelope had enough history to check" in rendered
    assert "No unusual transactions" not in rendered


def test_envelopes_with_too_little_history_are_listed_as_unjudged(ledger):
    """The difference between a finding and an absence of one."""
    result = report(ledger, "2025-10")

    assert "Rent" in result.unjudged
    assert "too few transactions to have a normal" in result.render()


# --- serialization --------------------------------------------------------


def test_to_dict_keeps_money_as_strings_and_dates_as_iso(ledger):
    payload = report(ledger, "2026-02").to_dict()

    assert payload["month"] == "2026-02"
    assert payload["label"] == "February 2026"
    assert payload["from_date"] == "2026-02-01"
    assert payload["asof"] == "2026-02-28"
    assert payload["spent_total"] == "1200.00"
    assert isinstance(payload["total_spend"], str)
    assert isinstance(payload["categorized_share"], str)
    assert all(isinstance(e["allocated"], str) for e in payload["envelopes"])
    assert all(isinstance(o["amount"], str) for o in payload["outliers"])


def test_the_label_does_not_depend_on_the_hosts_locale(ledger):
    """`strftime("%B")` would; a report of a fixed ledger must not change
    because the host's locale did."""
    assert report(ledger, "2026-03").label == "March 2026"
    assert report(ledger, "2025-12").label == "December 2025"


def test_repeated_reports_over_an_unchanged_ledger_are_identical(ledger):
    assert report(ledger, "2026-02").to_dict() == report(ledger, "2026-02").to_dict()


def test_ok_is_true_and_render_is_a_string_per_the_cli_contract(ledger):
    result = report(ledger, "2026-02")

    assert result.ok is True
    assert isinstance(result.render(), str)


def test_month_bounds_covers_february_in_a_leap_year():
    assert month_bounds(2024, 2) == (date(2024, 2, 1), date(2024, 2, 29))
    assert month_bounds(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
