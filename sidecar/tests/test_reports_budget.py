"""Unit tests for `budget_report`.

The fixture is built so that every claim the module makes about itself has a
case that would fail if the claim were false:

- **The window figures are differences, not totals.** `Groceries` is
  allocated in both months and spent in both, so a report over February that
  quietly used cumulative figures would show January's money too.
- **Carry-in makes the arithmetic checkable.** `Utilities` is funded in
  January and spent in February -- the case where window `allocated` is zero
  but the envelope is not broke, which is unreadable without `carried_in`.
- **A zero allocation has no percentage.** `Gifts` is spent against nothing
  and `Books` is mapped and untouched; neither is 0% and neither is 100%.
- **Overspend is surfaced, not clipped.** `Groceries` overspends its
  February allocation while staying in the black overall, so window
  `overspend` and cumulative `balance` have to be separately correct.
- **Unmapped spend is its own figure.** `Expenses:Unknown` is in the window
  and in no envelope, which with auto-apply off is the ordinary case.

The ledger is built with `loader.load_string` and injected through the
`entries`/`errors`/`options` seam, so no filesystem is involved.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from beancount import loader

from bookkeeper.envelope.compute import compute_envelope_state
from bookkeeper.reports.budget import STATUSES, budget_report

LEDGER = """\
option "title" "budget fixture"
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking               USD
2026-01-01 open Assets:EuroCash               EUR
2026-01-01 open Expenses:Food:Groceries       USD
2026-01-01 open Expenses:Food:Dining          USD
2026-01-01 open Expenses:Utilities:Electric   USD
2026-01-01 open Expenses:Utilities:Water      USD
2026-01-01 open Expenses:Books                USD
2026-01-01 open Expenses:Gifts                USD
2026-01-01 open Expenses:Travel               EUR
2026-01-01 open Expenses:Unknown              USD

;; Two accounts rolling into one envelope, so the rollup is the ledger's.
;; Books is mapped and never touched; Gifts is spent and never funded.
2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"      "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Dining"         "Dining Out"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Electric"  "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Water"     "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Books"               "Books"
2026-01-01 custom "envelope" "map" "Expenses:Gifts"               "Gifts"
2026-01-01 custom "envelope" "map" "Expenses:Travel"              "Travel"

2026-01-01 custom "envelope" "allocate" "Groceries"   300.00 USD
2026-01-01 custom "envelope" "allocate" "Dining Out"   50.00 USD
2026-01-01 custom "envelope" "allocate" "Utilities"    90.00 USD

2026-02-01 custom "envelope" "allocate" "Groceries"   100.00 USD
2026-02-01 custom "envelope" "allocate" "Dining Out"   50.00 USD

2026-01-01 * "Opening balance"
  Assets:Checking                          5000.00 USD
  Equity:Opening-Balances

2026-01-01 * "Opening euro float"
  Assets:EuroCash                           500.00 EUR
  Equity:Opening-Balances

2026-01-10 * "SAFEWAY"
  Assets:Checking                          -120.00 USD
  Expenses:Food:Groceries

2026-01-12 * "CHEZ PANISSE"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Dining

2026-02-05 * "SAFEWAY"
  Assets:Checking                          -150.00 USD
  Expenses:Food:Groceries

2026-02-08 * "PIZZAIOLO"
  Assets:Checking                           -30.00 USD
  Expenses:Food:Dining

2026-02-11 * "PG&E"
  Assets:Checking                           -40.00 USD
  Expenses:Utilities:Electric

2026-02-13 * "EBMUD"
  Assets:Checking                           -60.00 USD
  Expenses:Utilities:Water

2026-02-18 * "Birthday present"
  Assets:Checking                           -45.00 USD
  Expenses:Gifts

2026-02-20 * "Paris dinner"
  Assets:EuroCash                           -60.00 EUR
  Expenses:Travel

2026-02-25 * "UNIDENTIFIED CHARGE"
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


def report(ledger, from_date=None, to_date=None):
    entries, errors, options = ledger
    return budget_report(from_date, to_date, entries=entries, errors=errors, options=options)


def line(result, name):
    return next(e for e in result.envelopes if e.name == name)


@pytest.fixture(scope="module")
def february(ledger):
    return report(ledger, "2026-02-01", "2026-02-28")


# --- the window is a difference, not a running total ------------------------


def test_allocated_and_spent_are_the_windows_own_activity(february):
    """January's 300 allocated and 120 spent must not appear in a February
    report; they are carry-in, and they have their own field."""
    groceries = line(february, "Groceries")

    assert groceries.allocated == Decimal("100.00")
    assert groceries.spent == Decimal("150.00")
    assert groceries.carried_in == Decimal("180.00")


def test_the_full_window_sees_everything(ledger):
    result = report(ledger, "2026-01-01", "2026-02-28")
    groceries = line(result, "Groceries")

    assert groceries.allocated == Decimal("400.00")
    assert groceries.spent == Decimal("270.00")
    assert groceries.carried_in == Decimal(0)


def test_balance_equals_carry_in_plus_allocated_minus_spent(february):
    """The identity that lets a reader reconcile this report against
    `/envelopes` instead of finding two numbers and no explanation."""
    for e in february.envelopes:
        assert e.balance == e.carried_in + e.allocated - e.spent, e.name


def test_balance_is_the_same_number_slash_envelopes_reports(ledger, february):
    """`spent` and `balance` here are the envelope engine's, by construction.
    A second definition of "what counts as Groceries" would drift from the
    budget it describes, and the drift would be silent (§5.2)."""
    entries, _errors, _options = ledger
    envelopes = compute_envelope_state(entries, date(2026, 2, 28))
    expected = {e.name: e.balance for e in envelopes.envelopes}

    assert {e.name: e.balance for e in february.envelopes} == expected


def test_two_accounts_mapped_to_one_envelope_are_rolled_up(february):
    utilities = line(february, "Utilities")
    assert utilities.spent == Decimal("100.00")


# --- percent consumed -------------------------------------------------------


def test_consumed_ratio_is_spent_over_allocated(february):
    """A fraction, not a percentage: 0.6 is 60% consumed."""
    assert line(february, "Dining Out").consumed_ratio == 0.6
    assert line(february, "Groceries").consumed_ratio == 1.5


def test_consumed_ratio_against_a_zero_allocation_is_undefined_not_zero(february):
    """Not 0% (which reads as untouched) and not 100% (which reads as
    exhausted). Both are claims the data does not support."""
    gifts = line(february, "Gifts")

    assert gifts.allocated == Decimal(0)
    assert gifts.spent == Decimal("45.00")
    assert gifts.consumed_ratio is None


def test_an_untouched_envelope_also_has_no_percentage(february):
    books = line(february, "Books")

    assert (books.allocated, books.spent) == (Decimal(0), Decimal(0))
    assert books.consumed_ratio is None


def test_consumed_ratio_is_a_float_and_every_money_field_is_not(february):
    """The one deliberate type asymmetry: a ratio is not money, so it is a
    real number; everything beside it is `Decimal` and reaches the wire as a
    string. The division is still done in `Decimal` -- only the finished
    ratio is narrowed, so no amount is routed through binary floating point."""
    for e in february.envelopes:
        assert e.consumed_ratio is None or isinstance(e.consumed_ratio, float), e.name
        for money in (e.allocated, e.spent, e.remaining, e.overspend, e.balance):
            assert isinstance(money, Decimal), e.name


# --- status -----------------------------------------------------------------


def test_spending_against_no_allocation_is_its_own_status(february):
    """"Over budget" and "has no budget" need different fixes: one wants a
    bigger number, the other wants a budget line at all."""
    assert line(february, "Gifts").status == "unbudgeted"
    assert line(february, "Books").status == "unused"
    assert line(february, "Groceries").status == "over"
    assert line(february, "Dining Out").status == "within"


def test_an_envelope_funded_earlier_and_spent_now_reads_as_unbudgeted(february):
    """Utilities was funded in January and spent in February. The window
    allocation is genuinely zero, so there is genuinely no percentage -- and
    `carried_in` is what says the envelope was not broke."""
    utilities = line(february, "Utilities")

    assert utilities.allocated == Decimal(0)
    assert utilities.status == "unbudgeted"
    assert utilities.consumed_ratio is None
    assert utilities.carried_in == Decimal("90.00")
    assert utilities.balance == Decimal("-10.00")


def test_every_status_is_one_of_the_declared_set(february):
    assert {e.status for e in february.envelopes} <= set(STATUSES)


# --- overspend is surfaced, not clipped ------------------------------------


def test_remaining_goes_negative_rather_than_being_floored(february):
    """Clipping at zero is the shape of the §5.2 defect: an overspent
    envelope that reads as merely empty."""
    assert line(february, "Groceries").remaining == Decimal("-50.00")
    assert line(february, "Gifts").remaining == Decimal("-45.00")


def test_overspend_is_reported_per_line_and_summed(february):
    assert line(february, "Groceries").overspend == Decimal("50.00")
    assert line(february, "Gifts").overspend == Decimal("45.00")
    assert line(february, "Utilities").overspend == Decimal("100.00")
    assert line(february, "Dining Out").overspend == Decimal(0)
    # Travel's 60 EUR is in here uncoverted, which is what the mixed-currency
    # warning exists to say out loud.
    assert february.total_overspend == Decimal("255.00")


def test_overspent_is_a_server_computed_flag_agreeing_with_overspend(february):
    """Derived from `overspend`, never stored beside it, so the two cannot
    disagree -- the discipline `EnvelopeBalance` follows for the same pair.
    It exists so a client reads a boolean instead of parsing a money string:
    a browser cannot do `Decimal` arithmetic and must not try."""
    for e in february.envelopes:
        assert e.overspent == (e.overspend > 0), e.name

    assert line(february, "Groceries").overspent is True
    assert line(february, "Dining Out").overspent is False


def test_overspent_describes_the_window_not_the_running_balance(february):
    """Groceries overspends February and is still in the black overall. The
    cumulative reading has its own field and is not this one."""
    groceries = line(february, "Groceries")

    assert groceries.overspent is True
    assert groceries.balance > 0


def test_window_overspend_and_cumulative_balance_are_separate_readings(february):
    """Groceries overspends February and is still in the black overall. A
    report that conflated the two would call a healthy envelope broke."""
    groceries = line(february, "Groceries")

    assert groceries.overspend == Decimal("50.00")
    assert groceries.balance == Decimal("130.00")


def test_totals_are_the_sums_of_the_lines(february):
    assert february.total_allocated == sum(
        (e.allocated for e in february.envelopes), Decimal(0)
    )
    assert february.total_spent == sum((e.spent for e in february.envelopes), Decimal(0))
    assert february.total_remaining == february.total_allocated - february.total_spent


# --- unmapped spend ---------------------------------------------------------


def test_unmapped_spending_gets_its_own_figure(february):
    """Auto-apply is off, so nearly everything still posts to
    `Expenses:Unknown`. A budget report that omitted it would read as "you
    spent nothing"."""
    assert february.unmapped_total == Decimal("25.00")
    assert february.unmapped_accounts == ("Expenses:Unknown",)
    assert any("not mapped to any envelope" in w for w in february.warnings)
    assert any("verify" in w for w in february.warnings)


def test_unmapped_spending_is_not_folded_into_a_budget_line(february):
    assert not any(e.name == "Expenses:Unknown" for e in february.envelopes)
    assert all(e.spent != Decimal("25.00") for e in february.envelopes)


def test_a_window_with_nothing_unmapped_says_nothing_about_it(ledger):
    result = report(ledger, "2026-01-01", "2026-01-31")

    assert result.unmapped_total == Decimal(0)
    assert result.unmapped_accounts == ()
    assert not any("not mapped" in w for w in result.warnings)


def test_funding_and_equity_legs_are_never_counted_as_unmapped(ledger):
    """They are the other side of the same money; counting them would net
    every expense back to zero."""
    result = report(ledger, "2026-01-01", "2026-02-28")

    assert not any(a.startswith(("Assets", "Equity", "Income")) for a in result.unmapped_accounts)


def test_a_mixed_currency_ledger_declares_that_its_figures_mix(february):
    """`compute_envelope_state` sums `units.number` without conversion -- the
    ledger's own budget arithmetic, which this module does not second-guess
    but does declare."""
    assert any("mix currencies" in w and "EUR" in w for w in february.warnings), february.warnings
    # And it is not an abstract caveat: Travel's 60 EUR really is inside the
    # USD-labelled figures, which is precisely why the sentence is printed.
    assert line(february, "Travel").spent == Decimal("60.00")


# --- window handling and refusals ------------------------------------------


def test_the_default_window_is_the_ledgers_own_first_and_last_transaction(ledger):
    result = report(ledger)

    assert result.from_date == date(2026, 1, 1)
    assert result.to_date == date(2026, 2, 25)


def test_both_bounds_are_inclusive(ledger):
    result = report(ledger, "2026-02-05", "2026-02-05")
    assert line(result, "Groceries").spent == Decimal("150.00")


def test_dates_may_be_strings_or_date_objects(ledger):
    from_strings = report(ledger, "2026-02-01", "2026-02-28")
    from_dates = report(ledger, date(2026, 2, 1), date(2026, 2, 28))

    assert from_strings.to_dict() == from_dates.to_dict()


def test_a_backwards_window_is_a_failed_result_not_an_exception(ledger):
    result = report(ledger, "2026-03-01", "2026-01-01")

    assert result.ok is False
    assert result.errors == ["from (2026-03-01) is after to (2026-01-01)"]
    assert result.envelopes == ()


def test_an_unusable_envelope_mapping_fails_the_report(ambiguous_ledger):
    """The mapping *is* the report; a rollup over an ambiguous one would be a
    number no other view could reproduce."""
    result = report(ambiguous_ledger, "2026-01-01", "2026-01-31")

    assert result.ok is False
    assert "envelope mapping is unusable" in result.errors[0]


def test_an_unparseable_date_raises_for_the_caller_to_classify(ledger):
    with pytest.raises(ValueError):
        report(ledger, "last tuesday")


def test_a_ledger_with_no_transactions_does_not_crash(empty_ledger):
    result = report(empty_ledger)

    assert result.ok is True
    assert result.envelopes == ()
    assert result.total_allocated == Decimal(0)
    assert result.total_overspend == Decimal(0)


# --- serialization and rendering -------------------------------------------


def test_to_dict_keeps_money_as_strings_and_dates_as_iso(february):
    payload = february.to_dict()

    assert payload["from_date"] == "2026-02-01"
    assert payload["to_date"] == "2026-02-28"
    assert payload["total_overspend"] == "255.00"
    assert payload["unmapped_total"] == "25.00"
    groceries = next(e for e in payload["envelopes"] if e["name"] == "Groceries")
    assert groceries["allocated"] == "100.00"
    assert groceries["spent"] == "150.00"
    assert groceries["remaining"] == "-50.00"
    assert groceries["consumed_ratio"] == 1.5


def test_to_dict_carries_an_undefined_percentage_as_null(february):
    """`null`, not `"0"` and not the field going missing: the browser has to
    be able to tell "undefined" from "zero" without a convention."""
    payload = february.to_dict()
    gifts = next(e for e in payload["envelopes"] if e["name"] == "Gifts")

    assert gifts["consumed_ratio"] is None
    assert gifts["overspend"] == "45.00"
    assert gifts["overspent"] is True


def test_to_dict_carries_a_zero_overspend_as_a_string_and_a_false_flag(february):
    dining = next(e for e in february.to_dict()["envelopes"] if e["name"] == "Dining Out")

    assert dining["overspend"] == "0"
    assert dining["overspent"] is False


def test_envelopes_are_ordered_by_name_so_a_table_is_stable(february):
    names = [e.name for e in february.envelopes]
    assert names == sorted(names)


def test_render_tabulates_the_lines_and_marks_an_undefined_percentage(february):
    text = february.render()

    assert "Budget vs actual, 2026-02-01 to 2026-02-28 (USD)" in text
    assert "Groceries" in text
    assert "over" in text
    assert "unbudgeted" in text
    # "--" rather than a plausible-looking 0.00 or 100.00.
    assert "--" in text
    assert "Overspent (total): 255.00 USD" in text
    assert "belongs to no envelope and no budget line:" in text
    assert "Expenses:Unknown" in text


def test_render_always_prints_the_overspend_line_even_at_zero(ledger):
    text = report(ledger, "2026-01-01", "2026-01-31").render()
    assert "Overspent (total): 0.00 USD" in text


def test_render_reports_a_failure_with_its_reason(ledger):
    text = report(ledger, "2026-03-01", "2026-01-01").render()

    assert text.startswith("budget report failed:")
    assert "is after" in text
