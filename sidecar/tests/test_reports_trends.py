"""Unit tests for `trends_report`.

The fixture is six months long and each envelope in it exists to pin one
claim the module makes, because this is the report most able to mislead:

- `Groceries` rises by a fixed 20/month and `Dining Out` falls by 30/month --
  the two directions, on data whose least-squares slope is exact.
- `Utilities` is 100 every month, split across two accounts in one envelope,
  so `flat` is a *measured* verdict and the split is one observation rather
  than two half-sized ones.
- `Rent` is 1200 every month except one doubled charge: the case the
  MAD-is-zero fallback exists for. More than half the observations share an
  amount, so the robust scale collapses and the published mean-absolute
  deviation fallback is what finds the doubled month.
- `Coffee` ends in a large refund, so a *low* outlier and a negative score
  are covered, not just large charges.
- `Gifts` has three transactions -- below the sample floor, so it is
  declined rather than judged.
- `Travel` nets negative over the window, which makes a relative trend
  undefined; `Books` is mapped and never spent, which is flat, not
  an abstention.

Abstention is asserted as a distinct outcome throughout: `insufficient_data` must
never be reachable by a reader as "flat", and "not judged" must never be
reachable as "nothing unusual found".
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from beancount import loader

from bookkeeper.reports.trends import (
    DIRECTIONS,
    FLAT_BAND,
    MIN_PERIODS,
    MIN_TRANSACTIONS,
    OUTLIER_THRESHOLD,
    trends_report,
)

LEDGER = """\
option "title" "trends fixture"
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking               USD
2026-01-01 open Expenses:Food:Groceries       USD
2026-01-01 open Expenses:Food:Dining          USD
2026-01-01 open Expenses:Food:Coffee          USD
2026-01-01 open Expenses:Utilities:Electric   USD
2026-01-01 open Expenses:Utilities:Water      USD
2026-01-01 open Expenses:Housing:Rent         USD
2026-01-01 open Expenses:Books                USD
2026-01-01 open Expenses:Gifts                USD
2026-01-01 open Expenses:Travel               USD
2026-01-01 open Expenses:Unknown              USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"      "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Dining"         "Dining Out"
2026-01-01 custom "envelope" "map" "Expenses:Food:Coffee"         "Coffee"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Electric"  "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Water"     "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Housing:Rent"        "Rent"
2026-01-01 custom "envelope" "map" "Expenses:Books"               "Books"
2026-01-01 custom "envelope" "map" "Expenses:Gifts"               "Gifts"
2026-01-01 custom "envelope" "map" "Expenses:Travel"              "Travel"

2026-01-01 * "Opening balance"
  Assets:Checking                         40000.00 USD
  Equity:Opening-Balances

;; Groceries: +20 every month.
2026-01-05 * "SAFEWAY"
  Assets:Checking                          -100.00 USD
  Expenses:Food:Groceries
2026-02-05 * "SAFEWAY"
  Assets:Checking                          -120.00 USD
  Expenses:Food:Groceries
2026-03-05 * "SAFEWAY"
  Assets:Checking                          -140.00 USD
  Expenses:Food:Groceries
2026-04-05 * "SAFEWAY"
  Assets:Checking                          -160.00 USD
  Expenses:Food:Groceries
2026-05-05 * "SAFEWAY"
  Assets:Checking                          -180.00 USD
  Expenses:Food:Groceries
2026-06-05 * "SAFEWAY"
  Assets:Checking                          -200.00 USD
  Expenses:Food:Groceries

;; Dining Out: -30 every month.
2026-01-07 * "CHEZ PANISSE"
  Assets:Checking                          -200.00 USD
  Expenses:Food:Dining
2026-02-07 * "CHEZ PANISSE"
  Assets:Checking                          -170.00 USD
  Expenses:Food:Dining
2026-03-07 * "CHEZ PANISSE"
  Assets:Checking                          -140.00 USD
  Expenses:Food:Dining
2026-04-07 * "CHEZ PANISSE"
  Assets:Checking                          -110.00 USD
  Expenses:Food:Dining
2026-05-07 * "CHEZ PANISSE"
  Assets:Checking                           -80.00 USD
  Expenses:Food:Dining
2026-06-07 * "CHEZ PANISSE"
  Assets:Checking                           -50.00 USD
  Expenses:Food:Dining

;; Utilities: one bill a month, split across two accounts in one envelope.
2026-01-11 * "Utility bills"
  Assets:Checking                          -100.00 USD
  Expenses:Utilities:Electric                60.00 USD
  Expenses:Utilities:Water                   40.00 USD
2026-02-11 * "Utility bills"
  Assets:Checking                          -100.00 USD
  Expenses:Utilities:Electric                60.00 USD
  Expenses:Utilities:Water                   40.00 USD
2026-03-11 * "Utility bills"
  Assets:Checking                          -100.00 USD
  Expenses:Utilities:Electric                60.00 USD
  Expenses:Utilities:Water                   40.00 USD
2026-04-11 * "Utility bills"
  Assets:Checking                          -100.00 USD
  Expenses:Utilities:Electric                60.00 USD
  Expenses:Utilities:Water                   40.00 USD
2026-05-11 * "Utility bills"
  Assets:Checking                          -100.00 USD
  Expenses:Utilities:Electric                60.00 USD
  Expenses:Utilities:Water                   40.00 USD
2026-06-11 * "Utility bills"
  Assets:Checking                          -100.00 USD
  Expenses:Utilities:Electric                60.00 USD
  Expenses:Utilities:Water                   40.00 USD

;; Rent: identical every month, except one doubled charge in April.
2026-01-03 * "Rent"
  Assets:Checking                         -1200.00 USD
  Expenses:Housing:Rent
2026-02-03 * "Rent"
  Assets:Checking                         -1200.00 USD
  Expenses:Housing:Rent
2026-03-03 * "Rent"
  Assets:Checking                         -1200.00 USD
  Expenses:Housing:Rent
2026-04-03 * "Rent charged twice"
  Assets:Checking                         -2400.00 USD
  Expenses:Housing:Rent
2026-05-03 * "Rent"
  Assets:Checking                         -1200.00 USD
  Expenses:Housing:Rent
2026-06-03 * "Rent"
  Assets:Checking                         -1200.00 USD
  Expenses:Housing:Rent

;; Coffee: steady, then a large refund.
2026-01-09 * "BLUE BOTTLE"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Coffee
2026-02-09 * "BLUE BOTTLE"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Coffee
2026-03-09 * "BLUE BOTTLE"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Coffee
2026-04-09 * "BLUE BOTTLE"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Coffee
2026-05-09 * "BLUE BOTTLE"
  Assets:Checking                           -20.00 USD
  Expenses:Food:Coffee
2026-06-09 * "Refund - annual subscription cancelled"
  Assets:Checking                            50.00 USD
  Expenses:Food:Coffee                     -50.00 USD

;; Gifts: three transactions, below the sample floor.
2026-01-15 * "Present"
  Assets:Checking                           -40.00 USD
  Expenses:Gifts
2026-03-15 * "Present"
  Assets:Checking                           -40.00 USD
  Expenses:Gifts
2026-05-15 * "Present"
  Assets:Checking                           -40.00 USD
  Expenses:Gifts

;; Travel: a trip and a larger refund, so the window nets negative.
2026-01-20 * "Flight"
  Assets:Checking                          -100.00 USD
  Expenses:Travel
2026-02-20 * "Flight refunded"
  Assets:Checking                           150.00 USD
  Expenses:Travel                          -150.00 USD

;; Spending in no envelope at all -- the ordinary case with auto-apply off.
2026-03-25 * "UNIDENTIFIED CHARGE"
  Assets:Checking                           -25.00 USD
  Expenses:Unknown
2026-05-25 * "UNIDENTIFIED CHARGE"
  Assets:Checking                           -15.00 USD
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


def report(ledger, from_date=None, to_date=None):
    entries, errors, options = ledger
    return trends_report(from_date, to_date, entries=entries, errors=errors, options=options)


@pytest.fixture(scope="module")
def half_year(ledger):
    return report(ledger, "2026-01-01", "2026-06-30")


def trend(result, name):
    return next(e for e in result.envelopes if e.name == name)


def assessment(result, name):
    return next(a for a in result.assessments if a.envelope == name)


# --- direction --------------------------------------------------------------


def test_a_steadily_rising_envelope_reads_up(half_year):
    groceries = trend(half_year, "Groceries")

    assert groceries.direction == "up"
    assert groceries.slope == Decimal("20.00")
    assert groceries.mean == Decimal("150.00")
    assert groceries.relative_slope == Decimal("0.1333")


def test_a_steadily_falling_envelope_reads_down(half_year):
    dining = trend(half_year, "Dining Out")

    assert dining.direction == "down"
    assert dining.slope == Decimal("-30.00")
    assert dining.relative_slope == Decimal("-0.2400")


def test_an_unchanging_envelope_is_flat_and_that_is_a_measurement(half_year):
    utilities = trend(half_year, "Utilities")

    assert utilities.direction == "flat"
    assert utilities.slope == Decimal("0.00")
    assert utilities.mean == Decimal("100.00")
    assert "flat band" in utilities.reason


def test_the_verdict_is_recomputable_from_the_numbers_returned(half_year):
    """The whole point of returning `slope` and `mean` as used: a reader can
    redo the comparison and disagree with the classification."""
    for e in half_year.envelopes:
        if e.relative_slope is None:
            continue
        expected = "flat" if abs(e.relative_slope) < FLAT_BAND else (
            "up" if e.relative_slope > 0 else "down"
        )
        assert e.direction == expected, e.name


def test_a_small_wobble_does_not_become_a_trend(half_year):
    """Rent moves once, by a lot, and still ends up flat: 34.29/month against
    a 1400 mean is 2.4%, well inside the flat band. A report that called that
    "rent is going up" would be wrong in the way that matters."""
    rent = trend(half_year, "Rent")

    assert rent.slope == Decimal("34.29")
    assert rent.direction == "flat"


def test_an_envelope_with_no_spending_at_all_is_flat_not_an_abstention(half_year):
    """Nothing happened in any period. That is a measurement, and a
    verifiable one."""
    books = trend(half_year, "Books")

    assert books.direction == "flat"
    assert books.total == Decimal(0)
    assert "no spending in any of the 6 periods" in books.reason


def test_every_direction_is_one_of_the_declared_set(half_year):
    assert {e.direction for e in half_year.envelopes} <= set(DIRECTIONS)


def test_the_declared_set_is_the_ratified_four(half_year):
    """`insufficient_data` is a fourth value, not a null direction and not a
    `flat` with a missing delta -- both of those make a client infer
    abstention from an absence, which flattens "we don't know yet" into
    "steady"."""
    assert DIRECTIONS == ("up", "down", "flat", "insufficient_data")


def test_every_line_carries_the_period_counts_that_justify_its_verdict(half_year):
    """So one envelope's card is self-contained: "3 of 3" and "1 of 3" are
    the difference between a verdict and an abstention."""
    for e in half_year.envelopes:
        assert e.periods_required == MIN_PERIODS, e.name
        assert e.periods_observed == 6, e.name
        assert (e.direction == "insufficient_data") >= (
            e.periods_observed < e.periods_required
        ), e.name


# --- abstention on direction -----------------------------------------------


def test_two_periods_is_not_a_trend(ledger):
    """A line through two points is a line through two points."""
    result = report(ledger, "2026-01-01", "2026-02-28")

    assert result.periods == ("2026-01", "2026-02")
    for e in result.envelopes:
        assert e.direction == "insufficient_data", e.name
        assert f"at least {MIN_PERIODS}" in e.reason
        # The counts that justify the abstention, on the line itself.
        assert (e.periods_observed, e.periods_required) == (2, 3), e.name


def test_abstention_is_distinct_from_flat(ledger):
    """The two must not be reachable as each other: `flat` is "we measured it
    and it is not moving", `insufficient_data` is "we declined to say"."""
    two_periods = report(ledger, "2026-01-01", "2026-02-28")
    six_periods = report(ledger, "2026-01-01", "2026-06-30")

    assert trend(two_periods, "Utilities").direction == "insufficient_data"
    assert trend(six_periods, "Utilities").direction == "flat"


def test_the_slope_is_still_measured_when_the_verdict_is_abstention(ledger):
    """A `slope` of 0.00 printed beside "insufficient_data" would read as "we
    measured it and it was flat" -- the one thing abstention exists to avoid
    saying."""
    result = report(ledger, "2026-01-01", "2026-02-28")

    assert trend(result, "Groceries").slope == Decimal("20.00")
    assert trend(result, "Groceries").direction == "insufficient_data"


def test_a_window_that_nets_negative_has_no_relative_trend(half_year):
    """Travel's refund exceeds its spending, so `slope / mean` compares
    against a negative baseline and means nothing."""
    travel = trend(half_year, "Travel")

    assert travel.direction == "insufficient_data"
    assert travel.relative_slope is None
    assert "not positive" in travel.reason


def test_an_abstaining_line_always_says_why(half_year):
    for e in half_year.envelopes:
        assert e.reason, e.name


# --- outliers ---------------------------------------------------------------


def test_a_doubled_recurring_charge_is_found(half_year):
    """The case the MAD-is-zero fallback exists for: five identical rents
    collapse the robust scale to zero, and declining there would decline
    exactly where a doubled charge matters most."""
    rent_outliers = [o for o in half_year.outliers if o.envelope == "Rent"]

    assert len(rent_outliers) == 1
    found = rent_outliers[0]
    assert found.posted_date == date(2026, 4, 3)
    assert found.amount == Decimal("2400.00")
    assert found.scale_method == "mean_absolute_deviation"
    assert found.score == Decimal("4.79")


def test_an_unusually_large_refund_is_found_with_a_negative_score(half_year):
    """A low outlier is as interesting as a high one, and the sign of the
    score is what says which it was."""
    coffee_outliers = [o for o in half_year.outliers if o.envelope == "Coffee"]

    assert len(coffee_outliers) == 1
    found = coffee_outliers[0]
    assert found.amount == Decimal("-50.00")
    assert found.score < 0
    assert abs(found.score) > OUTLIER_THRESHOLD


def test_every_flagged_transaction_can_be_recomputed_by_hand(half_year):
    """`score = (amount - median) / scale`, from three numbers on the same
    card. An outlier flag a reader cannot interrogate is worse than none."""
    for o in half_year.outliers:
        assert ((o.amount - o.median) / o.scale).quantize(Decimal("0.01")) == o.score
        assert abs(o.score) > o.threshold


def test_ordinary_variation_is_not_flagged(half_year):
    """Groceries doubles across the window and Dining Out quarters, and
    neither is unusual *for its own envelope* -- a detector that flagged
    those would flag everything."""
    flagged = {o.envelope for o in half_year.outliers}

    assert "Groceries" not in flagged
    assert "Dining Out" not in flagged
    assert assessment(half_year, "Groceries").judged is True
    assert assessment(half_year, "Groceries").outliers_found == 0


def test_outliers_are_ordered_worst_first(half_year):
    """If anything downstream shows only the top few, they should be the ones
    furthest from ordinary."""
    scores = [abs(o.score) for o in half_year.outliers]
    assert scores == sorted(scores, reverse=True)


def test_a_split_transaction_is_one_observation_at_its_net(half_year):
    """Utilities is one bill a month across two accounts. Counting the legs
    separately would systematically halve the observed charge size and so
    under-report large ones."""
    assert assessment(half_year, "Utilities").sample_size == 6


# --- abstention on outliers -------------------------------------------------


def test_a_small_sample_is_declined_rather_than_judged(half_year):
    """Calling the third-ever transaction unusual is a statement about the
    sample size, not about the transaction."""
    gifts = assessment(half_year, "Gifts")

    assert gifts.sample_size == 3
    assert gifts.judged is False
    assert f"at least {MIN_TRANSACTIONS}" in gifts.reason
    assert not any(o.envelope == "Gifts" for o in half_year.outliers)


def test_an_envelope_with_no_variation_at_all_is_declined(half_year):
    """Every Utilities bill is 100. There is no dispersion against which
    anything could be unusual, and inventing one would flag noise."""
    utilities = assessment(half_year, "Utilities")

    assert utilities.judged is False
    assert "no dispersion" in utilities.reason
    assert utilities.median == Decimal("100.00")


def test_not_judged_is_distinguishable_from_nothing_found(half_year):
    """"No outliers" and "we did not look" are different answers, and a chat
    summary that conflated them would be unfalsifiable."""
    assert assessment(half_year, "Books").judged is False
    assert assessment(half_year, "Groceries").judged is True
    assert assessment(half_year, "Groceries").outliers_found == 0


def test_every_envelope_gets_an_assessment_judged_or_not(half_year):
    assert {a.envelope for a in half_year.assessments} == {
        e.name for e in half_year.envelopes
    }


def test_a_judged_envelope_reports_the_basis_it_used(half_year):
    groceries = assessment(half_year, "Groceries")

    assert groceries.scale_method == "mad"
    assert groceries.median == Decimal("150.00")
    assert groceries.scale == Decimal("44.4774")


# --- coverage and self-description -----------------------------------------


def test_unmapped_spending_is_reported_so_the_reader_knows_the_coverage(half_year):
    """With auto-apply off nearly everything is unmapped, and a trends report
    that silently judged only the mapped remainder would look like a complete
    picture of the month."""
    assert half_year.unmapped_total == Decimal("40.00")
    assert half_year.unmapped_accounts == ("Expenses:Unknown",)
    assert half_year.unmapped_transactions == 2


def test_the_report_states_the_thresholds_it_applied(half_year):
    """So the response describes its own method instead of requiring the
    reader to have the source open. A summary that cannot be checked is not
    a finding."""
    assert half_year.min_periods == MIN_PERIODS
    assert half_year.flat_band == FLAT_BAND
    assert half_year.min_transactions == MIN_TRANSACTIONS
    assert half_year.outlier_threshold == OUTLIER_THRESHOLD


def test_the_default_window_is_the_ledgers_own_first_and_last_transaction(ledger):
    result = report(ledger)

    assert result.from_date == date(2026, 1, 1)
    assert result.to_date == date(2026, 6, 11)


# --- refusals ---------------------------------------------------------------


def test_a_backwards_window_is_a_failed_result_not_an_exception(ledger):
    result = report(ledger, "2026-06-01", "2026-01-01")

    assert result.ok is False
    assert "is after" in result.errors[0]
    assert result.envelopes == ()
    assert result.outliers == ()


def test_an_unusable_envelope_mapping_fails_the_report(ambiguous_ledger):
    result = report(ambiguous_ledger, "2026-01-01", "2026-01-31")

    assert result.ok is False
    assert "envelope mapping is unusable" in result.errors[0]


def test_an_unparseable_date_raises_for_the_caller_to_classify(ledger):
    with pytest.raises(ValueError):
        report(ledger, "last tuesday")


# --- serialization and rendering -------------------------------------------


def test_to_dict_keeps_every_statistic_as_a_string(half_year):
    """Statistics belong in `Decimal` in the sidecar. Nothing crosses to the
    browser as a float to be finished there."""
    payload = half_year.to_dict()

    groceries = next(e for e in payload["envelopes"] if e["name"] == "Groceries")
    assert groceries["slope"] == "20.00"
    assert groceries["mean"] == "150.00"
    assert groceries["relative_slope"] == "0.1333"
    assert payload["outlier_threshold"] == "3.5"
    assert payload["flat_band"] == "0.10"
    rent = next(o for o in payload["outliers"] if o["envelope"] == "Rent")
    assert rent["amount"] == "2400.00"
    assert rent["score"] == "4.79"
    assert rent["posted_date"] == "2026-04-03"


def test_to_dict_carries_an_undefined_relative_slope_as_null(half_year):
    payload = half_year.to_dict()
    travel = next(e for e in payload["envelopes"] if e["name"] == "Travel")

    assert travel["relative_slope"] is None
    assert travel["direction"] == "insufficient_data"


def test_render_shows_the_verdicts_the_flags_and_what_was_declined(half_year):
    text = half_year.render()

    assert "Spending trends, 2026-01-01 to 2026-06-30 (by month, USD)" in text
    assert "up" in text and "down" in text and "flat" in text
    assert "insufficient_data" in text
    assert "Rent charged twice" in text
    # The arithmetic behind the flag, on the page.
    assert "mean_absolute_deviation" in text
    assert "Not judged for outliers:" in text
    assert "Expenses:Unknown" in text


def test_render_says_so_plainly_when_nothing_was_flagged(ledger):
    text = report(ledger, "2026-01-01", "2026-02-28").render()
    assert "No transaction scored beyond the outlier threshold." in text


def test_render_reports_a_failure_with_its_reason(ledger):
    text = report(ledger, "2026-06-01", "2026-01-01").render()

    assert text.startswith("trends report failed:")
    assert "is after" in text
