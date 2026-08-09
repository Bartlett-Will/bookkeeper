"""Unit tests for `envelope_gaps`.

Two things here can mislead a user about their own money, and each account in
the fixture exists to pin one of them.

**Mapping the catch-all.** `Expenses:Unknown` is unmapped and `verify` errors
on it, correctly -- but the fix is to categorize the transactions, not to add
a `map` directive. A directive would silence §5.2's silent-drift guard
permanently while the spending underneath stayed uncategorized, turning the
one check that catches vanishing money into a check that cannot fail. So the
refusal is asserted here, and so is the fact that the refusal is *explained*:
a report that quietly omitted the catch-all would read as "this one is fine".

**Reading a measurement as advice.** `observed_monthly_mean` is the mean of
money already spent. The fixture makes every way of getting that wrong
visible:

- `Subscriptions` spends 100 a month Jan..May and 250 in a *partial* June.
  The right answer is 100.00; folding the part-month in gives 125.00 or
  150.00, so the distinction cannot be right by accident.
- `Sporadic` spends in January and May only. Feb, Mar and Apr are real zeros
  inside the window: 120.00, not the 300.00 an active-months-only divisor
  would report while calling it monthly.
- `Gifts` starts in April -- two complete months, below the floor, declined.
- `JuneOnly` lives entirely inside the partial month, so it has no complete
  month at all: a different abstention from `Gifts`, and reported as one.
- `Refunded` nets negative, so there is no monthly amount to describe.
- `Housing:Rent` is mapped and must never appear.
- `Euro` has postings in no operating-currency at all.

Abstention is a first-class outcome throughout, in the shape
`test_reports_trends.py` established: `None` must never be reachable by a
reader as zero, and "we declined" must never be reachable as "nothing here".
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader

from bookkeeper.categorize.models import UNKNOWN_ACCOUNTS
from bookkeeper.envelope.directives import find_ambiguous_accounts, parse_envelope_directives
from bookkeeper.envelope.verify import _find_unmapped_expense_accounts
from bookkeeper.tuning.gaps import (
    BASIS_INSUFFICIENT_HISTORY,
    BASIS_NOT_POSITIVE,
    BASIS_OBSERVED,
    MIN_MONTHS,
    NOT_A_RECOMMENDATION,
    envelope_gaps,
)

LEDGER = """\
option "title" "envelope gaps fixture"
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
2026-01-01 open Assets:EuroCash             EUR
2026-01-01 open Expenses:Housing:Rent       USD
2026-01-01 open Expenses:Subscriptions      USD
2026-01-01 open Expenses:Sporadic           USD
2026-01-01 open Expenses:Gifts              USD
2026-01-01 open Expenses:JuneOnly           USD
2026-01-01 open Expenses:Refunded           USD
2026-01-01 open Expenses:Euro               EUR
2026-01-01 open Expenses:Unknown            USD

2026-01-01 custom "envelope" "map" "Expenses:Housing:Rent" "Rent"

2026-01-01 * "Opening balance"
  Assets:Checking                        40000.00 USD
  Equity:Opening-Balances

2026-01-02 * "Opening balance EUR"
  Assets:EuroCash                         1000.00 EUR
  Equity:Opening-Balances

;; Mapped: never a gap, however much it spends.
2026-01-03 * "Rent"
  Assets:Checking                        -1200.00 USD
  Expenses:Housing:Rent
2026-05-03 * "Rent"
  Assets:Checking                        -1200.00 USD
  Expenses:Housing:Rent

;; 100 a month Jan..May, then 250 in the partial June.
2026-01-05 * "Streaming"
  Assets:Checking                         -100.00 USD
  Expenses:Subscriptions
2026-02-05 * "Streaming"
  Assets:Checking                         -100.00 USD
  Expenses:Subscriptions
2026-03-05 * "Streaming"
  Assets:Checking                         -100.00 USD
  Expenses:Subscriptions
2026-04-05 * "Streaming"
  Assets:Checking                         -100.00 USD
  Expenses:Subscriptions
2026-05-05 * "Streaming"
  Assets:Checking                         -100.00 USD
  Expenses:Subscriptions
2026-06-09 * "Streaming annual"
  Assets:Checking                         -250.00 USD
  Expenses:Subscriptions

;; January and May only. February, March and April are real zeros.
2026-01-20 * "One big thing"
  Assets:Checking                         -500.00 USD
  Expenses:Sporadic
2026-05-20 * "One small thing"
  Assets:Checking                         -100.00 USD
  Expenses:Sporadic

;; Starts in April: two complete months, below the floor.
2026-04-10 * "Present"
  Assets:Checking                          -40.00 USD
  Expenses:Gifts
2026-05-10 * "Present"
  Assets:Checking                          -60.00 USD
  Expenses:Gifts

;; Its whole history is inside the partial trailing month.
2026-06-05 * "New thing"
  Assets:Checking                          -45.00 USD
  Expenses:JuneOnly

;; The refund exceeds the spending.
2026-02-03 * "Bought"
  Assets:Checking                          -80.00 USD
  Expenses:Refunded
2026-03-03 * "Refunded, and then some"
  Assets:Checking                          100.00 USD
  Expenses:Refunded                       -100.00 USD

;; Postings in no operating currency at all.
2026-02-14 * "Cafe in Paris"
  Assets:EuroCash                          -30.00 EUR
  Expenses:Euro

;; The catch-all: a categorization gap, not a mapping gap.
2026-02-25 * "UNIDENTIFIED CHARGE"
  Assets:Checking                          -25.00 USD
  Expenses:Unknown
2026-03-25 * "UNIDENTIFIED CHARGE"
  Assets:Checking                          -15.00 USD
  Expenses:Unknown
"""

AMBIGUOUS_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking          USD
2026-01-01 open Expenses:Food:Groceries  USD
2026-01-01 open Expenses:Loose           USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Food"

2026-01-05 * "SAFEWAY"
  Assets:Checking                        -80.00 USD
  Expenses:Food:Groceries
2026-01-06 * "Something else"
  Assets:Checking                        -20.00 USD
  Expenses:Loose
"""

MALFORMED_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking      USD
2026-01-01 open Expenses:Loose       USD

2026-01-01 custom "envelope" "map" "Expenses:Loose"

2026-01-05 * "Something"
  Assets:Checking                    -20.00 USD
  Expenses:Loose
"""

#: Everything mapped, and the ledger ends on the last day of a month -- so
#: neither the "no gaps" line nor the partial-month warning can be produced
#: by accident elsewhere.
COMPLETE_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking          USD
2026-01-01 open Expenses:Food:Groceries  USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"

2026-01-05 * "SAFEWAY"
  Assets:Checking                        -80.00 USD
  Expenses:Food:Groceries
2026-01-31 * "SAFEWAY"
  Assets:Checking                        -20.00 USD
  Expenses:Food:Groceries
"""


def _load(text):
    entries, errors, options = loader.load_string(text)
    assert not errors, errors
    return entries, options


@pytest.fixture(scope="module")
def ledger():
    return _load(LEDGER)


@pytest.fixture(scope="module")
def ambiguous_ledger():
    return _load(AMBIGUOUS_LEDGER)


@pytest.fixture(scope="module")
def malformed_ledger():
    return _load(MALFORMED_LEDGER)


@pytest.fixture(scope="module")
def complete_ledger():
    return _load(COMPLETE_LEDGER)


def gaps(loaded):
    entries, options = loaded
    return envelope_gaps(entries=entries, options=options)


@pytest.fixture(scope="module")
def result(ledger):
    return gaps(ledger)


def line(result, account):
    return next(a for a in result.accounts if a.account == account)


# --- the catch-all must never be offered a mapping --------------------------
#
# The one move that turns §5.2's silent-drift guard off rather than closing
# it. Everything else in this file is about being wrong; this is about being
# unable to be caught.


def test_the_catch_all_is_never_listed_as_a_mapping_gap(result):
    """`Expenses:Unknown` is unmapped and is a `verify` error, and it is still
    not a *mapping* gap: the fix is to categorize the transactions."""
    assert "Expenses:Unknown" not in {a.account for a in result.accounts}
    assert [a.account for a in result.uncategorized] == ["Expenses:Unknown"]


def test_the_catch_all_is_kept_out_of_the_totals(result):
    """40.00 across 2 transactions sits in `Expenses:Unknown`. Folding it into
    the mapping-gap totals would inflate them with money that needs a
    different fix, and after a backfill it is the largest line by far."""
    assert result.total_unmapped_spend == Decimal("1475.00")
    assert result.total_unmapped_transactions == 13

    unknown = result.uncategorized[0]
    assert unknown.total_spend == Decimal("40.00")
    assert unknown.transactions == 2
    assert unknown.total_spend + result.total_unmapped_spend == Decimal("1515.00")


def test_the_refusal_is_explained_and_not_merely_performed(result):
    """The reason has to travel with the refusal. A reader who is only told
    "not listed" will reasonably conclude the tool missed it and add the
    directive by hand -- which is the exact outcome the refusal exists to
    prevent."""
    text = result.render()

    assert "Expenses:Unknown also has no mapping, and must not be given one" in text
    assert (
        "Mapping the catch-all to an envelope would silence the unmapped-account "
        "check instead of closing it" in text
    )
    assert "are simply not categorized yet" in text


def test_the_refusal_points_at_the_fix_that_does_close_the_gap(result):
    """Categorize, do not map. Naming the commands is what makes the refusal
    actionable rather than a dead end."""
    text = result.render()

    assert "bookkeeper review" in text
    assert "bookkeeper suggest-rules" in text


def test_no_paste_ready_map_directive_is_ever_emitted_for_the_catch_all(result):
    """Every mapping-gap line ends with a directive to paste. The catch-all
    must not get one -- a line a user can copy is a line a user will copy."""
    text = result.render()

    assert 'custom "envelope" "map" "Expenses:Subscriptions"' in text
    assert 'custom "envelope" "map" "Expenses:Unknown"' not in text


def test_the_catch_all_set_is_the_categorizers_own(result):
    """Imported rather than restated, so "catch-all" means here what it means
    to the cascade. A second list would drift."""
    assert "Expenses:Unknown" in UNKNOWN_ACCOUNTS
    assert {a.account for a in result.uncategorized} <= UNKNOWN_ACCOUNTS
    assert not {a.account for a in result.accounts} & UNKNOWN_ACCOUNTS


# --- the set is verify's set ------------------------------------------------


def test_the_unmapped_set_is_verifys_own_set(result, ledger):
    """Two answers to "which accounts are unmapped" is precisely the §5.2
    drift this module was written to avoid, so it calls `verify`'s finder.
    Asserted over the union of everything this module reports, including the
    account it can only warn about -- an account dropped from all three
    buckets would be one `verify` fails on and this report never mentions."""
    entries, _options = ledger
    parsed = parse_envelope_directives(entries)
    known = {m.account for m in parsed.maps} | set(find_ambiguous_accounts(parsed.maps))
    expected = _find_unmapped_expense_accounts(entries, known)

    reported = (
        {a.account for a in result.accounts}
        | {a.account for a in result.uncategorized}
        | {"Expenses:Euro"}
    )
    assert reported == expected


def test_a_mapped_account_is_never_a_gap(result):
    """Rent is the largest spend in the fixture and is mapped, so it is the
    line most likely to show up if the mapping check were inverted."""
    assert "Expenses:Housing:Rent" not in {a.account for a in result.accounts}
    assert "Expenses:Housing:Rent" not in result.render()


# --- the monthly figure describes the past ----------------------------------


def test_the_wording_says_what_happened_not_what_to_do(result):
    """A user reading "$100" as advice when it means "you averaged $100" is
    the confident-wrong claim this project keeps designing against. The
    per-account lines are checked with the disclaimer removed, since the
    disclaimer is the one place the word "recommended" belongs."""
    body = result.render().replace(NOT_A_RECOMMENDATION, "")

    assert "you averaged 100.00 USD/month over 5 complete month(s)" in body
    for advice in ("we suggest", "you should", "set aside", "budget of", "allocate "):
        assert advice not in body.casefold(), advice


def test_the_field_is_named_for_what_it_measured(result):
    subscriptions = line(result, "Expenses:Subscriptions")

    assert subscriptions.basis == BASIS_OBSERVED == "observed_mean"
    assert subscriptions.observed_monthly_mean == Decimal("100.00")
    assert "a description of the past, not a recommendation" in subscriptions.reason


def test_the_disclaimer_travels_in_the_payload_not_only_in_the_render(result):
    """A client rendering the number on a card gets the caveat without having
    to write one, so it cannot show the figure without it."""
    payload = result.to_dict()

    assert payload["not_a_recommendation"] == NOT_A_RECOMMENDATION
    assert "They are not recommended allocations" in payload["not_a_recommendation"]
    assert NOT_A_RECOMMENDATION in result.render()


def test_the_figure_is_recomputable_from_the_numbers_beside_it(result):
    """`spend_in_window / months_observed`, from two fields on the same line.
    A figure a reader cannot redo is a figure they have to trust."""
    for a in result.accounts + result.uncategorized:
        if a.observed_monthly_mean is None:
            continue
        recomputed = (a.spend_in_window / a.months_observed).quantize(Decimal("0.01"))
        assert recomputed == a.observed_monthly_mean, a.account


# --- it declines rather than extrapolating ----------------------------------


def test_too_little_history_declines_rather_than_extrapolating(result):
    """Two months cannot distinguish a recurring cost from a single annual
    premium, and turning a premium into "you spend $2,000 a month" is worse
    than saying nothing."""
    gifts = line(result, "Expenses:Gifts")

    assert gifts.basis == BASIS_INSUFFICIENT_HISTORY
    assert gifts.observed_monthly_mean is None
    assert gifts.months_observed == 2
    assert f"at least {MIN_MONTHS} are needed" in gifts.reason
    assert "tell a recurring cost from a one-off" in gifts.reason


def test_declining_is_distinct_from_reporting_a_zero(result):
    """`None` and `0.00` are different claims, and a client that inferred
    abstention from a falsy number would render "you averaged $0" for an
    account it has no rate for."""
    gifts = line(result, "Expenses:Gifts")

    assert gifts.observed_monthly_mean is None
    assert gifts.to_dict()["observed_monthly_mean"] is None
    assert gifts.spend_in_window == Decimal("100.00")


def test_an_account_with_no_complete_month_at_all_is_a_different_abstention(result):
    """`JuneOnly` lives entirely inside the partial trailing month, so it has
    no window rather than too short a one. Reported as its own reason, since
    "wait a month" and "the ledger has not covered a month of this yet" are
    different things to tell a user."""
    june = line(result, "Expenses:JuneOnly")

    assert june.basis == BASIS_INSUFFICIENT_HISTORY
    assert june.months_observed == 0
    assert (june.window_from, june.window_to) == (None, None)
    assert "no month of this account's history is complete in the ledger yet" in june.reason
    assert line(result, "Expenses:Gifts").reason != june.reason


def test_a_net_negative_account_declines_rather_than_reporting_a_negative_rate(result):
    """A negative monthly average is not a description of anything a budget
    could hold. This one has enough months, so it is the abstention that is
    about the money rather than about the history."""
    refunded = line(result, "Expenses:Refunded")

    assert refunded.basis == BASIS_NOT_POSITIVE
    assert refunded.months_observed == 4
    assert refunded.months_observed >= MIN_MONTHS
    assert refunded.observed_monthly_mean is None
    assert refunded.total_spend == Decimal("-20.00")
    assert "refunds meet or exceed spending" in refunded.reason


def test_the_same_ledger_both_judges_and_declines(result):
    """The discrimination the tests above depend on, asserted directly:
    5 complete months gets a figure and 2 does not, on one ledger, so neither
    outcome is what this module always does."""
    assert line(result, "Expenses:Sporadic").observed_monthly_mean == Decimal("120.00")
    assert line(result, "Expenses:Gifts").observed_monthly_mean is None


def test_every_line_carries_the_month_counts_that_justify_its_verdict(result):
    """So one account's card is self-contained: "5 of 3" and "2 of 3" are the
    difference between a measurement and an abstention."""
    for a in result.accounts + result.uncategorized:
        assert a.months_required == MIN_MONTHS == result.min_months, a.account
        assert a.reason, a.account
        if a.months_observed < a.months_required:
            assert a.basis == BASIS_INSUFFICIENT_HISTORY, a.account
            assert a.observed_monthly_mean is None, a.account


# --- complete months only ---------------------------------------------------


def test_the_partial_trailing_month_is_excluded_from_the_rate(result):
    """`Subscriptions` spends 100 a month Jan..May and 250 on 9 June. The
    answer is 100.00. Dividing 750 by five months gives 150.00 and by six
    gives 125.00, so this cannot pass unless the part-month really is out."""
    subscriptions = line(result, "Expenses:Subscriptions")

    assert subscriptions.observed_monthly_mean == Decimal("100.00")
    assert subscriptions.months_observed == 5
    assert subscriptions.window_to.isoformat() == "2026-05-31"


def test_total_spend_and_spend_in_window_are_never_merged(result):
    """The difference between them is exactly the trailing partial month --
    the figure that would silently corrupt the rate if it were folded in, so
    they stay separate fields."""
    subscriptions = line(result, "Expenses:Subscriptions")

    assert subscriptions.total_spend == Decimal("750.00")
    assert subscriptions.spend_in_window == Decimal("500.00")
    assert subscriptions.last_posted.isoformat() == "2026-06-09"


def test_the_exclusion_is_reported_rather_than_done_quietly(result):
    """Answering a slightly different question than the one asked is its own
    failure, even when the substituted question is the better one."""
    assert any("2026-06 is only partly recorded" in w for w in result.warnings)
    assert any("would under-state the rate" in w for w in result.warnings)


def test_a_ledger_ending_on_a_month_end_has_no_partial_month(complete_ledger):
    """The warning must not fire on an ordinary ledger, or it becomes noise
    readers learn to skip."""
    outcome = gaps(complete_ledger)

    assert not any("partly recorded" in w for w in outcome.warnings)


def test_zero_months_inside_the_window_are_counted_as_zeros(result):
    """`Sporadic` spends 500 in January and 100 in May. Those three empty
    months are real evidence -- you genuinely spent nothing -- and averaging
    over active months only would report a per-active-month rate of 300.00
    while calling it monthly. This is the rule `reports/trends.py` applies to
    its own window, and the two are meant to agree."""
    sporadic = line(result, "Expenses:Sporadic")

    assert sporadic.months_observed == 5
    assert sporadic.transactions == 2
    assert sporadic.observed_monthly_mean == Decimal("120.00")


# --- ordering and coverage --------------------------------------------------


def test_the_biggest_gap_comes_first_because_the_question_is_money(result):
    """"How much of my budget is missing" is a money question, unlike the rule
    suggestions' "how much work is this saving", which is a count question."""
    assert [a.account for a in result.accounts] == [
        "Expenses:Subscriptions",
        "Expenses:Sporadic",
        "Expenses:Gifts",
        "Expenses:JuneOnly",
        "Expenses:Refunded",
    ]
    keys = [(-a.total_spend, a.account) for a in result.accounts]
    assert keys == sorted(keys)


def test_a_fully_mapped_ledger_says_so_plainly(complete_ledger):
    outcome = gaps(complete_ledger)

    assert outcome.ok is True
    assert outcome.accounts == ()
    assert outcome.total_unmapped_spend == Decimal("0.00")
    assert (
        "No envelope gaps: every expense account with postings maps to an envelope."
        in outcome.render()
    )


# --- warnings and refusals --------------------------------------------------


def test_an_ambiguously_mapped_account_is_a_warning_not_a_gap(ambiguous_ledger):
    """An account mapped to two envelopes is known-but-not-usable. Reporting
    it as both double-mapped and unmapped would be self-contradictory."""
    outcome = gaps(ambiguous_ledger)

    assert "Expenses:Food:Groceries" not in {a.account for a in outcome.accounts}
    assert [a.account for a in outcome.accounts] == ["Expenses:Loose"]
    assert any("map to more than one envelope" in w for w in outcome.warnings)
    assert any("not a gap" in w for w in outcome.warnings)


def test_unusable_directives_fail_rather_than_guessing_what_is_unmapped(malformed_ledger):
    """Nothing can be called unmapped with confidence against a mapping that
    does not parse, and a list computed anyway would be an authoritative-
    looking guess."""
    outcome = gaps(malformed_ledger)

    assert outcome.ok is False
    assert outcome.accounts == ()
    assert outcome.uncategorized == ()
    assert "envelope directives are unusable" in outcome.errors[0]
    assert outcome.render().startswith("envelope gaps failed:")


def test_an_account_with_no_postings_in_the_operating_currency_is_declared(result):
    """Silently dropping it would read as "this one is fine" for an account
    `verify` still fails on."""
    assert "Expenses:Euro" not in {a.account for a in result.accounts}
    warning = next(w for w in result.warnings if "Expenses:Euro" in w)

    assert "no postings in USD" in warning
    assert "still a `verify` error" in warning


# --- money crosses as a string ----------------------------------------------


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


def test_every_amount_crosses_as_a_quantized_string(result):
    """Money is `Decimal` in the sidecar and a string at the edge. A float
    finished in a browser is how a cent goes missing."""
    payload = result.to_dict()

    for entry in payload["accounts"] + payload["uncategorized"]:
        for field in ("total_spend", "spend_in_window"):
            assert isinstance(entry[field], str), (entry["account"], field)
            assert re.fullmatch(r"-?\d+\.\d{2}", entry[field]), (entry["account"], field)
        mean = entry["observed_monthly_mean"]
        assert mean is None or re.fullmatch(r"-?\d+\.\d{2}", mean), entry["account"]
    assert re.fullmatch(r"-?\d+\.\d{2}", payload["total_unmapped_spend"])


def test_no_figure_leaves_this_module_in_exponent_notation(
    ledger, ambiguous_ledger, complete_ledger, malformed_ledger
):
    """`Decimal` division takes the dividend's exponent less the divisor's, so
    `Decimal(0) / Decimal("150.00")` is `Decimal("0E+2")` and serializes as
    the string `"0E+2"`. That parses as a number and compares as garbage, and
    it reached a browser once in Phase 5.

    Every string in the payload is checked rather than the fields that carry
    money today, because the next one will be a different field.
    """
    for loaded in (ledger, ambiguous_ledger, complete_ledger, malformed_ledger):
        payload = gaps(loaded).to_dict()
        offenders = [
            (path, text) for path, text in _string_values(payload) if EXPONENT_FORM.match(text)
        ]
        assert offenders == []


# --- nothing is written -----------------------------------------------------


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every path under `root`, with file contents. Directories map to None."""
    return {
        path.relative_to(root).as_posix(): (None if path.is_dir() else path.read_bytes())
        for path in root.rglob("*")
    }


def test_envelope_gaps_writes_nothing_at_all(tmp_path, monkeypatch):
    """It fixes nothing, and adding a `map` directive is a decision about what
    an account *is* -- the user's to make. Asserted over the whole tree by
    path and by content, so a created file fails even when nothing existing
    changed.
    """
    root = tmp_path / "root"
    (root / "ledger").mkdir(parents=True)
    (root / "data").mkdir(parents=True)
    (root / "ledger" / "main.beancount").write_text(LEDGER, encoding="utf-8")
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
    before = _snapshot(root)

    outcome = envelope_gaps()

    assert outcome.ok is True
    assert outcome.accounts, "the fixture must actually produce gaps here"
    assert _snapshot(root) == before


# --- rendering --------------------------------------------------------------


def test_render_gives_the_directive_a_user_would_paste(result):
    text = result.render()

    assert 'to budget it: custom "envelope" "map" "Expenses:Gifts" "<envelope>"' in text


def test_render_shows_the_abstentions_beside_the_measurements(result):
    """A line with no monthly figure must say why, or a reader scanning the
    column will read the blank as zero."""
    text = result.render()

    assert "you averaged 120.00 USD/month" in text
    assert "no monthly figure: 2 complete month(s) of history" in text
    assert "no monthly figure: spending over the complete months is not positive" in text


def test_render_states_the_coverage_it_is_reporting_on(result):
    text = result.render()

    assert (
        "5 expense account(s) have spending but no envelope mapping: 13 transaction(s) "
        "totalling 1475.00 USD sit outside every budget figure." in text
    )
