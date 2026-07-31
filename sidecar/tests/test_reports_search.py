"""Unit tests for `search_transactions`.

`q` reaches this module from a chat message -- typed by a user or emitted by
an 8B model -- so it is untrusted input, and there are **two** injection
surfaces that must both be closed:

- **BQL injection**, closed by binding `q` as a query parameter so nothing
  the caller types can add a clause;
- **regex injection**, which binding does *not* close. BQL's `~` compiles a
  bound parameter as a regular expression, so `(a+)+$` is a valid parameter
  and a catastrophic backtracker, and `[` is a valid parameter and an
  unhandled `re.error`. `literal_pattern` is what closes it.

The second is the one worth testing hardest, because closing the first looks
like it closed both. These tests therefore assert not just that hostile input
fails to crash, but that a pattern metacharacter matches *itself and nothing
else* -- `.*` must find nothing, and a transaction whose description really
does contain `(a+)+$` must be found by searching for that exact text.

Everything runs against a ledger built with `loader.load_string` and injected
via the `entries`/`errors`/`options` seam, so there is no filesystem involved
and the real `ledger/` is untouchable from here.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest
from beancount import loader

from bookkeeper.reports.search import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    literal_pattern,
    search_transactions,
)

LEDGER = """\
option "title" "search fixture"
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
2026-01-01 open Liabilities:CreditCard      USD
2026-01-01 open Expenses:Food:Groceries     USD
2026-01-01 open Expenses:Food:Dining        USD
2026-01-01 open Expenses:Transport:Gas      USD
2026-01-01 open Expenses:Unknown            USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"  "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Dining"     "Dining Out"
2026-01-01 custom "envelope" "map" "Expenses:Transport:Gas"   "Transport"

2026-01-01 * "Opening balance"
  Assets:Checking                          1000.00 USD
  Equity:Opening-Balances

2026-01-05 * "SAFEWAY #1842"
  simplefin-id: "txn-1"
  simplefin-payee: "Safeway"
  simplefin-memo: "weekly shop"
  Assets:Checking                           -80.00 USD
  Expenses:Food:Groceries

2026-01-07 * "BLUE BOTTLE COFFEE"
  simplefin-id: "txn-2"
  Assets:Checking                            -6.75 USD
  Expenses:Food:Dining

2026-01-09 * "Chevron" "PUMP PURCHASE 0093281"
  simplefin-id: "txn-3"
  Assets:Checking                           -52.40 USD
  Expenses:Transport:Gas

2026-01-11 * "COSTCO WHOLESALE"
  simplefin-id: "txn-4"
  Assets:Checking                          -150.00 USD
  Expenses:Food:Groceries                   100.00 USD
  Expenses:Food:Dining                       50.00 USD

2026-01-13 * "TRADER JOES #0188"
  simplefin-id: "txn-5"
  Liabilities:CreditCard                    -52.31 USD
  Expenses:Food:Groceries

2026-01-15 * "WIDGET (a+)+$ STORE"
  simplefin-id: "txn-6"
  Assets:Checking                           -10.00 USD
  Expenses:Unknown
"""

#: One account mapped to two envelopes. `build_account_map` refuses this.
AMBIGUOUS_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
2026-01-01 open Expenses:Food:Groceries     USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Food"

2026-01-05 * "SAFEWAY #1842"
  simplefin-id: "txn-1"
  Assets:Checking                           -80.00 USD
  Expenses:Food:Groceries
"""


@pytest.fixture(scope="module")
def ledger():
    entries, errors, options = loader.load_string(LEDGER)
    assert not errors, errors
    return entries, errors, options


@pytest.fixture(scope="module")
def ambiguous_ledger():
    entries, errors, options = loader.load_string(AMBIGUOUS_LEDGER)
    assert not errors, errors
    return entries, errors, options


def search(ledger, q, limit=None):
    entries, errors, options = ledger
    return search_transactions(q, limit=limit, entries=entries, errors=errors, options=options)


# --- the regex injection guard --------------------------------------------


def test_literal_pattern_escapes_everything_and_ignores_case():
    """Public because it is the guard, and a guard nobody can test directly
    is a guard nobody checks."""
    assert literal_pattern("a+b") == "(?i)" + re.escape("a+b")
    assert re.match(literal_pattern("(a+)+$"), "(a+)+$")
    assert re.match(literal_pattern("safeway"), "SAFEWAY")
    # The escaped form can only ever match itself.
    assert re.search(literal_pattern(".*"), "anything at all") is None


def test_a_regex_metacharacter_matches_itself_and_nothing_else(ledger):
    """The assertion that proves escaping, rather than merely surviving.

    `.*` as a regex matches all seven transactions. As a literal it matches
    none, because no description contains the two characters `.` and `*`.
    A test that only checked "no exception" would pass either way.
    """
    assert search(ledger, ".*").total == 0
    assert search(ledger, "^").total == 0
    assert search(ledger, "Expenses:.*").total == 0


def test_a_catastrophic_backtracker_is_matched_as_plain_text(ledger):
    """`(a+)+$` compiled as a pattern is an exponential-time bomb. Escaped,
    it is just six characters, and it finds the transaction whose description
    genuinely contains them."""
    result = search(ledger, "(a+)+$")

    assert result.ok is True
    assert result.total == 1
    assert result.matches[0].description == "WIDGET (a+)+$ STORE"


#: Metacharacters that would raise `re.error` if compiled as a pattern, and
#: that appear nowhere in `LEDGER` -- so escaped, they must find nothing.
#:
#: `(`, `)`, `+` and `$` are deliberately *absent* from this list: all four
#: occur in "WIDGET (a+)+$ STORE", so each correctly matches that row. That
#: is the literal-matching property, not a failure of this test -- see
#: `test_a_lone_metacharacter_still_matches_a_description_containing_it`.
UNCOMPILABLE = ["[", "*", "?", "\\", "[[[", "(?P<x>", "{2,}"]


@pytest.mark.parametrize("hostile", UNCOMPILABLE)
def test_input_that_would_not_compile_as_a_regex_is_an_ordinary_empty_result(ledger, hostile):
    """Unescaped, each of these raises `re.error` inside the query engine and
    surfaces as a 500 on a chat turn."""
    # Self-check: if a future edit to LEDGER introduces one of these
    # characters, this case would start matching a real row and quietly stop
    # testing what it claims to. Fail loudly instead.
    assert hostile not in LEDGER, f"{hostile!r} now occurs in the fixture; pick another"

    result = search(ledger, hostile)

    assert result.ok is True
    assert result.total == 0
    assert result.errors == []


def test_a_lone_metacharacter_still_matches_a_description_containing_it(ledger):
    """The other half of the same property: escaping must not mean "ignore".

    A bare `(` is not a valid pattern, but it is perfectly valid *text*, and
    a description that contains one should be found by searching for it.
    """
    result = search(ledger, "(")

    assert result.ok is True
    assert result.total == 1
    assert result.matches[0].description == "WIDGET (a+)+$ STORE"


def test_bql_clauses_in_the_query_are_matched_as_text_not_executed(ledger):
    """`q` is bound as a parameter, so the query text stays a constant this
    module owns."""
    for attempt in ("' OR 1=1 --", "%(funding)s", "'; SELECT * FROM #postings; --"):
        result = search(ledger, attempt)
        assert result.ok is True
        assert result.total == 0


# --- what it matches on ---------------------------------------------------


def test_matches_on_narration(ledger):
    result = search(ledger, "blue bottle")

    assert result.total == 1
    assert result.matches[0].description == "BLUE BOTTLE COFFEE"


def test_matches_on_payee(ledger):
    """Narration and payee are separate beancount fields; a bank puts the
    merchant in either one depending on the feed."""
    result = search(ledger, "chevron")

    assert result.total == 1
    assert result.matches[0].payee == "Chevron"
    assert result.matches[0].description == "PUMP PURCHASE 0093281"


def test_matches_on_the_account_a_transaction_was_filed_under(ledger):
    """So "groceries" finds both the merchant and the envelope account.

    None of these descriptions contain the word; every match here arrives
    through `has_account`.
    """
    result = search(ledger, "groceries")

    assert result.total == 3
    assert not any("GROCERIES" in m.description.upper() for m in result.matches)
    assert {m.simplefin_id for m in result.matches} == {"txn-1", "txn-4", "txn-5"}


def test_matching_is_case_insensitive(ledger):
    assert search(ledger, "SAFEWAY").total == search(ledger, "safeway").total == 1


# --- one row per transaction, not per posting -----------------------------


def test_a_two_posting_transaction_yields_one_row(ledger):
    """A spend has two postings; returning both would show every result
    twice. The funding leg is the useful one -- it carries the signed amount
    the bank reported."""
    result = search(ledger, "safeway")

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.account == "Assets:Checking"
    assert match.amount == Decimal("-80.00")
    assert match.categorized_account == "Expenses:Food:Groceries"


def test_a_split_transaction_reports_every_leg_it_was_filed_to(ledger):
    """Dropping legs would misrepresent where the money went, so the whole
    set is joined -- and sorted, so repeated searches are byte-identical."""
    result = search(ledger, "costco")

    assert len(result.matches) == 1
    match = result.matches[0]
    assert match.account == "Assets:Checking"
    assert match.categorized_account == "Expenses:Food:Dining, Expenses:Food:Groceries"
    # No single account, so no single envelope to label it with.
    assert match.envelope is None


def test_a_credit_card_charge_is_found_the_same_way_a_debit_is(ledger):
    """Liabilities are funding accounts too."""
    result = search(ledger, "trader joes")

    assert len(result.matches) == 1
    assert result.matches[0].account == "Liabilities:CreditCard"
    assert result.matches[0].amount == Decimal("-52.31")


def test_the_envelope_label_comes_from_the_ledgers_own_mapping(ledger):
    result = search(ledger, "blue bottle")
    assert result.matches[0].envelope == "Dining Out"

    unmapped = search(ledger, "widget")
    assert unmapped.matches[0].categorized_account == "Expenses:Unknown"
    assert unmapped.matches[0].envelope is None


def test_simplefin_metadata_is_surfaced(ledger):
    match = search(ledger, "safeway").matches[0]

    assert match.simplefin_id == "txn-1"
    assert match.payee == "Safeway"
    assert match.memo == "weekly shop"


def test_the_beancount_payee_wins_over_the_simplefin_one(ledger):
    """`payee or simplefin_payee`: the ledger's own field is the one a human
    may have corrected."""
    assert search(ledger, "chevron").matches[0].payee == "Chevron"
    assert search(ledger, "blue bottle").matches[0].payee is None


# --- limits ---------------------------------------------------------------


def test_results_are_capped_and_the_truncation_is_reported(ledger):
    result = search(ledger, "groceries", limit=2)

    assert len(result.matches) == 2
    assert result.total == 3
    assert result.truncated is True
    assert result.limit == 2


def test_an_untruncated_result_says_so(ledger):
    result = search(ledger, "groceries", limit=10)

    assert result.truncated is False
    assert result.limit == 10
    assert len(result.matches) == result.total == 3


def test_the_default_limit_applies_when_none_is_given(ledger):
    assert search(ledger, "groceries").limit == DEFAULT_LIMIT


def test_an_absurd_limit_is_capped(ledger):
    """A tool argument comes from an 8B model, and `limit=100000` is a
    plausible thing for one to emit."""
    assert search(ledger, "groceries", limit=100_000).limit == MAX_LIMIT


@pytest.mark.parametrize("limit", [0, -1, -100])
def test_a_non_positive_limit_becomes_one(ledger, limit):
    """`limit=0` returning nothing would look identical to "no matches"."""
    result = search(ledger, "groceries", limit=limit)

    assert result.limit == 1
    assert len(result.matches) == 1
    assert result.total == 3


# --- degenerate input -----------------------------------------------------


@pytest.mark.parametrize("empty", ["", "   ", "\t\n", None])
def test_an_empty_query_is_a_reason_not_a_crash(ledger, empty):
    """The caller is often a model; a structured "nothing to search for" is
    easier to recover from than an exception."""
    result = search(ledger, empty)

    assert result.ok is False
    assert result.errors == ["search text is empty"]
    assert result.matches == []


def test_surrounding_whitespace_is_trimmed(ledger):
    result = search(ledger, "  safeway  ")

    assert result.ok is True
    assert result.query == "safeway"
    assert result.total == 1


# --- determinism and ordering ---------------------------------------------


def test_results_are_newest_first(ledger):
    dates = [m.posted_date for m in search(ledger, "groceries").matches]
    assert dates == sorted(dates, reverse=True)


def test_repeated_searches_over_an_unchanged_ledger_are_identical(ledger):
    """A chat surface re-renders; a result set that reshuffled between two
    identical requests would look like the ledger had changed."""
    assert search(ledger, "groceries").to_dict() == search(ledger, "groceries").to_dict()


# --- a broken envelope mapping must not fail a search ---------------------


def test_an_unusable_envelope_mapping_costs_the_labels_not_the_results(ambiguous_ledger):
    """The envelope label is decoration on a result row, while the
    transactions themselves are exactly what someone debugging a broken
    mapping is trying to look at. `verify` is what judges the mapping."""
    result = search(ambiguous_ledger, "safeway")

    assert result.ok is True
    assert result.total == 1
    assert result.matches[0].envelope is None
    assert any("envelope labels omitted" in w for w in result.warnings), result.warnings


# --- serialization --------------------------------------------------------


def test_to_dict_keeps_money_as_strings_and_dates_as_iso(ledger):
    payload = search(ledger, "safeway").to_dict()

    assert payload["matches"][0]["amount"] == "-80.00"
    assert payload["matches"][0]["posted_date"] == "2026-01-05"
    assert payload["shown"] == 1
    assert payload["total"] == 1
    assert set(payload) == {
        "ok",
        "query",
        "total",
        "shown",
        "limit",
        "truncated",
        "matches",
        "warnings",
        "errors",
    }


def test_render_describes_a_hit_a_miss_and_a_truncation(ledger):
    hit = search(ledger, "safeway").render()
    assert "1 transaction(s) match 'safeway'" in hit
    assert "Assets:Checking -> Expenses:Food:Groceries  [Groceries]" in hit
    assert "id: txn-1" in hit

    assert search(ledger, "kayak") .render() == "no transactions match 'kayak'"

    truncated = search(ledger, "groceries", limit=1).render()
    assert "limited to 1" in truncated


def test_render_reports_a_failure_with_its_reason(ledger):
    assert search(ledger, "").render() == "search failed:\n  - search text is empty"
