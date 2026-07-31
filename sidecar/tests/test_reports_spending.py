"""Unit tests for `spending_report`.

Three properties carry the weight here, and each has a specific failure mode
it exists to prevent:

- **The envelope rollup is the ledger's, not the report's.** Grouping comes
  from the `custom "envelope" "map"` directives via `envelope.directives`,
  the same code `compute.py` and `verify.py` use. A second, report-local
  notion of "which accounts are groceries" would drift from the budget it
  describes, and the drift would show as a chart disagreeing with
  `/envelopes`.
- **Spending outside every envelope is reported, not dropped.** Auto-apply is
  off, so today essentially everything still posts to `Expenses:Unknown`,
  which no `map` directive targets. A report that silently summed only mapped
  accounts would render as an empty chart and read as "you spent nothing" --
  exactly the silent drift §5.2 builds `verify` to prevent.
- **Empty periods are emitted.** Every series carries a point for every
  period in the window, so a chart's x-axis is the range that was asked for
  rather than the subset that happened to have activity.

The ledger is built with `loader.load_string` and injected through the
`entries`/`errors`/`options` seam, so no filesystem is involved.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from beancount import loader

from bookkeeper.reports.spending import PERIODS, spending_report

LEDGER = """\
option "title" "spending fixture"
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking               USD
2026-01-01 open Assets:EuroCash               EUR
2026-01-01 open Expenses:Food:Groceries       USD
2026-01-01 open Expenses:Food:Dining          USD
2026-01-01 open Expenses:Utilities:Electric   USD
2026-01-01 open Expenses:Utilities:Water      USD
2026-01-01 open Expenses:Books                USD
2026-01-01 open Expenses:Travel               EUR
2026-01-01 open Expenses:Unknown              USD

;; Two accounts rolling into one envelope, so the rollup is exercised rather
;; than being a one-to-one relabelling. Books is mapped but never spent.
2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"      "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Dining"         "Dining Out"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Electric"  "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Water"     "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Books"               "Books"
2026-01-01 custom "envelope" "map" "Expenses:Travel"              "Travel"

2026-01-01 * "Opening balance"
  Assets:Checking                          2000.00 USD
  Equity:Opening-Balances

2026-01-01 * "Opening euro float"
  Assets:EuroCash                           500.00 EUR
  Equity:Opening-Balances

2026-01-05 * "SAFEWAY #1842"
  Assets:Checking                           -80.00 USD
  Expenses:Food:Groceries

2026-01-20 * "BERKELEY BOWL"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Groceries

2026-01-12 * "PG&E"
  Assets:Checking                           -90.00 USD
  Expenses:Utilities:Electric

2026-01-25 * "EBMUD"
  Assets:Checking                           -40.00 USD
  Expenses:Utilities:Water

2026-02-10 * "TRADER JOES"
  Assets:Checking                           -50.00 USD
  Expenses:Food:Groceries

2026-02-15 * "CHEZ PANISSE"
  Assets:Checking                           -30.00 USD
  Expenses:Food:Dining

2026-02-20 * "Paris dinner"
  Assets:EuroCash                           -60.00 EUR
  Expenses:Travel

2026-03-05 * "UNIDENTIFIED CHARGE"
  Assets:Checking                           -25.00 USD
  Expenses:Unknown
"""

AMBIGUOUS_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking           USD
2026-01-01 open Expenses:Food:Groceries   USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Food"

2026-01-05 * "SAFEWAY"
  Assets:Checking                         -80.00 USD
  Expenses:Food:Groceries
"""

EMPTY_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Assets:Checking   USD
"""


def _load(text):
    entries, errors, options = loader.load_string(text)
    assert not errors, errors
    return entries, errors, options


@pytest.fixture(scope="module")
def ledger():
    return _load(LEDGER)


@pytest.fixture(scope="module")
def ambiguous_ledger():
    return _load(AMBIGUOUS_LEDGER)


@pytest.fixture(scope="module")
def empty_ledger():
    return _load(EMPTY_LEDGER)


def report(ledger, from_date=None, to_date=None, period="month"):
    entries, errors, options = ledger
    return spending_report(
        from_date, to_date, period, entries=entries, errors=errors, options=options
    )


def series(result, name):
    return next(s for s in result.envelopes if s.name == name)


def points(result, name):
    return {p.period: p.amount for p in series(result, name).points}


# --- bucketing ------------------------------------------------------------


def test_spending_is_bucketed_by_month(ledger):
    result = report(ledger, "2026-01-01", "2026-03-31")

    assert result.ok is True
    assert result.period == "month"
    assert result.periods == ("2026-01", "2026-02", "2026-03")
    assert points(result, "Groceries") == {
        "2026-01": Decimal("100.00"),
        "2026-02": Decimal("50.00"),
        "2026-03": Decimal(0),
    }
    assert series(result, "Groceries").total == Decimal("150.00")


def test_two_accounts_mapped_to_one_envelope_are_rolled_up(ledger):
    """The rollup is the ledger's, not this module's. Electric and Water are
    separate accounts and one budget line."""
    result = report(ledger, "2026-01-01", "2026-03-31")

    assert points(result, "Utilities")["2026-01"] == Decimal("130.00")
    assert series(result, "Utilities").total == Decimal("130.00")


def test_every_period_in_the_window_gets_a_point_even_with_no_activity(ledger):
    """So a chart's x-axis is the requested range rather than the subset that
    happened to have spending, and a series does not appear and disappear
    between requests."""
    result = report(ledger, "2026-01-01", "2026-03-31")

    for spend_series in result.envelopes:
        assert [p.period for p in spend_series.points] == list(result.periods), spend_series.name


def test_an_envelope_with_a_mapping_but_no_spending_still_gets_a_row(ledger):
    """"We budget for this and spent nothing" is a real, useful reading, and
    a vanishing series breaks a chart's legend."""
    result = report(ledger, "2026-01-01", "2026-03-31")

    assert "Books" in {s.name for s in result.envelopes}
    assert series(result, "Books").total == Decimal(0)
    assert all(p.amount == Decimal(0) for p in series(result, "Books").points)


def test_spending_is_bucketed_by_year(ledger):
    result = report(ledger, "2026-01-01", "2026-03-31", period="year")

    assert result.periods == ("2026",)
    assert series(result, "Groceries").total == Decimal("150.00")
    assert len(series(result, "Groceries").points) == 1


def test_the_window_narrows_the_figures(ledger):
    result = report(ledger, "2026-02-01", "2026-02-28")

    assert result.periods == ("2026-02",)
    assert series(result, "Groceries").total == Decimal("50.00")
    assert series(result, "Utilities").total == Decimal(0)


def test_both_bounds_are_inclusive(ledger):
    """A single-day window containing one transaction must contain it."""
    result = report(ledger, "2026-01-05", "2026-01-05")

    assert series(result, "Groceries").total == Decimal("80.00")


def test_the_default_window_is_the_ledgers_own_first_and_last_transaction(ledger):
    """Not "the last six months of wall-clock time": a report of a fixed
    ledger must not change meaning because a day passed, and a demo ledger
    outside that window would render as an empty chart."""
    result = report(ledger)

    assert result.from_date == date(2026, 1, 1)
    assert result.to_date == date(2026, 3, 5)


def test_dates_may_be_strings_or_date_objects(ledger):
    from_string = report(ledger, "2026-01-01", "2026-03-31")
    from_dates = report(ledger, date(2026, 1, 1), date(2026, 3, 31))

    assert from_string.to_dict() == from_dates.to_dict()


# --- what is counted, and what is not -------------------------------------


def test_funding_and_equity_legs_are_not_counted(ledger):
    """They are the other side of the same money; counting them would net
    every expense back to zero."""
    result = report(ledger, "2026-01-01", "2026-03-31")

    assert result.total == Decimal("310.00")
    assert result.total > 0
    assert not any(s.name.startswith(("Assets", "Equity")) for s in result.envelopes)


def test_the_total_is_the_sum_of_the_series(ledger):
    result = report(ledger, "2026-01-01", "2026-03-31")
    assert result.total == sum((s.total for s in result.envelopes), Decimal(0))


def test_unmapped_spending_is_reported_separately_not_dropped(ledger):
    """Auto-apply is off, so nearly everything currently posts to
    `Expenses:Unknown`. Summing only mapped accounts would render as "you
    spent nothing"."""
    result = report(ledger, "2026-01-01", "2026-03-31")

    assert result.unmapped_total == Decimal("25.00")
    assert result.unmapped_accounts == ("Expenses:Unknown",)
    assert any("not mapped to any envelope" in w for w in result.warnings), result.warnings
    assert any("verify" in w for w in result.warnings)
    # It is excluded from the per-envelope figures, not silently folded in.
    assert result.total == Decimal("310.00")


def test_a_ledger_with_nothing_unmapped_says_nothing_about_it(ledger):
    result = report(ledger, "2026-01-01", "2026-02-28")

    assert result.unmapped_total == Decimal(0)
    assert result.unmapped_accounts == ()
    assert not any("not mapped" in w for w in result.warnings)


def test_spending_in_another_currency_is_excluded_and_declared(ledger):
    """This report is single-currency. Silently mixing EUR into a USD total
    would be worse than omitting it, so the omission is stated."""
    result = report(ledger, "2026-01-01", "2026-03-31")

    assert result.currency == "USD"
    assert series(result, "Travel").total == Decimal(0)
    assert any("EUR" in w and "single-currency" in w for w in result.warnings), result.warnings


# --- refusals -------------------------------------------------------------


@pytest.mark.parametrize("period", ["decade", "week", "day", "", "MONTH"])
def test_an_unknown_period_raises_for_the_caller_to_classify(ledger, period):
    """`ValueError`, not a failed result: the caller decides whether that is
    a 422 or a CLI message. `period` selects between two constant queries, so
    it never becomes SQL."""
    with pytest.raises(ValueError, match="period must be one of"):
        report(ledger, period=period)


def test_the_supported_periods_are_month_and_year():
    assert PERIODS == ("month", "year")


def test_a_backwards_window_is_a_failed_result_not_an_exception(ledger):
    result = report(ledger, "2026-03-01", "2026-01-01")

    assert result.ok is False
    assert result.errors == ["from (2026-03-01) is after to (2026-01-01)"]
    assert result.envelopes == ()


def test_an_unusable_envelope_mapping_fails_the_report(ambiguous_ledger):
    """Unlike `search`, where the label is decoration, here the mapping *is*
    the report. A rollup over an ambiguous mapping would be a number no other
    view could reproduce."""
    result = report(ambiguous_ledger, "2026-01-01", "2026-01-31")

    assert result.ok is False
    assert "envelope mapping is unusable" in result.errors[0]


def test_an_unparseable_date_raises(ledger):
    with pytest.raises(ValueError):
        report(ledger, "last tuesday")


def test_a_ledger_with_no_transactions_does_not_crash(empty_ledger):
    result = report(empty_ledger)

    assert result.ok is True
    assert result.total == Decimal(0)
    assert result.envelopes == ()
    assert result.from_date == result.to_date


# --- serialization --------------------------------------------------------


def test_to_dict_keeps_money_as_strings_and_dates_as_iso(ledger):
    payload = report(ledger, "2026-01-01", "2026-03-31").to_dict()

    assert payload["from_date"] == "2026-01-01"
    assert payload["to_date"] == "2026-03-31"
    assert payload["total"] == "310.00"
    assert payload["unmapped_total"] == "25.00"
    groceries = next(e for e in payload["envelopes"] if e["name"] == "Groceries")
    assert groceries["total"] == "150.00"
    assert groceries["points"][0] == {"period": "2026-01", "amount": "100.00"}


def test_envelopes_are_ordered_by_name_so_a_legend_is_stable(ledger):
    result = report(ledger, "2026-01-01", "2026-03-31")
    names = [s.name for s in result.envelopes]
    assert names == sorted(names)


def test_render_tabulates_the_periods_and_calls_out_unmapped_spending(ledger):
    text = report(ledger, "2026-01-01", "2026-03-31").render()

    assert "Spending by envelope, 2026-01-01 to 2026-03-31 (by month, USD)" in text
    assert "2026-01" in text and "2026-02" in text and "2026-03" in text
    assert "Groceries" in text
    assert "25.00 USD of spending belongs to no envelope:" in text
    assert "Expenses:Unknown" in text


def test_render_reports_a_failure_with_its_reason(ledger):
    text = report(ledger, "2026-03-01", "2026-01-01").render()
    assert text.startswith("spending report failed:")
    assert "is after" in text
