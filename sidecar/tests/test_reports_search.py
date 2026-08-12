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
from beancount.core.data import Transaction

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

#: Spending, a refund against it, a transfer between the user's own accounts,
#: and the same merchant in a second currency. Everything `amount_totals` has
#: to keep apart, in one place, all matching the single query "wholefoods".
TOTALS_LEDGER = """\
option "operating_currency" "USD"

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:Checking             USD
; No currency constraint: this account carries the GBP leg below, which is
; the whole point of the fixture.
2026-01-01 open Liabilities:CreditCard
2026-01-01 open Expenses:Food:Groceries
2026-01-01 open Expenses:Travel

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"

2026-01-01 * "Opening balance"
  Assets:Checking                          1000.00 USD
  Equity:Opening-Balances

2026-01-04 * "WHOLEFOODS MKT 101"
  Assets:Checking                          -100.00 USD
  Expenses:Food:Groceries

2026-01-06 * "WHOLEFOODS MKT 101"
  Liabilities:CreditCard                    -40.00 USD
  Expenses:Food:Groceries

2026-01-08 * "WHOLEFOODS refund"
  Assets:Checking                            25.00 USD
  Expenses:Food:Groceries                   -25.00 USD

2026-01-10 * "WHOLEFOODS card payment"
  Assets:Checking                           -40.00 USD
  Liabilities:CreditCard                     40.00 USD

2026-01-12 * "WHOLEFOODS LONDON"
  Liabilities:CreditCard                    -30.00 GBP
  Expenses:Travel                            30.00 GBP
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
def searchable_text(ledger):
    """Every field the query actually matches on, as one blob.

    Narration, payee and account -- the three things `_SEARCH_QUERY` looks
    at. Deliberately not the ledger source, which also carries syntax the
    query never sees.
    """
    entries, _errors, _options = ledger
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        parts.append(entry.narration or "")
        parts.append(entry.payee or "")
        parts.extend(posting.account for posting in entry.postings)
    return "\n".join(parts)


@pytest.fixture(scope="module")
def totals_ledger():
    entries, errors, options = loader.load_string(TOTALS_LEDGER)
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
def test_input_that_would_not_compile_as_a_regex_is_an_ordinary_empty_result(
    ledger, searchable_text, hostile
):
    """Unescaped, each of these raises `re.error` inside the query engine and
    surfaces as a 500 on a chat turn."""
    # Self-check: if a future edit to the fixture introduces one of these
    # characters into a searchable field, this case would start matching a
    # real row and quietly stop testing what it claims to. Checked against
    # narration/payee/account rather than the raw source, because the source
    # also contains beancount syntax the query never sees -- `*` is the
    # transaction flag on every entry, and is not searchable text.
    assert hostile not in searchable_text, f"{hostile!r} now occurs in the fixture; pick another"

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


# --- totals over the matches ----------------------------------------------
#
# The measured Phase 4 gap: "how much did I spend at Whole Foods" had no
# correct tool -- search matched the text without adding it up, and the
# spending report groups by envelope rather than by merchant. What makes this
# hard is not the addition, it is saying honestly *what* was added: the
# matches span accounts, currencies, and both directions, and a single figure
# labelled "total" over that mixture is a number a user would act on and that
# would be wrong.


def totals_by_currency(result):
    return {t.currency: t for t in result.amount_totals}


def test_spending_is_totalled_over_the_matches(totals_ledger):
    """The question the gap was about, answered directly."""
    usd = totals_by_currency(search(totals_ledger, "wholefoods"))["USD"]

    assert usd.spent == Decimal("140.00")
    assert usd.spend_count == 2


def test_every_figure_is_a_decimal_never_a_float(totals_ledger):
    """A float here would put a rounding error into the one number the user
    reads as the answer."""
    usd = totals_by_currency(search(totals_ledger, "wholefoods"))["USD"]

    for value in (usd.spent, usd.received, usd.net, usd.transferred):
        assert isinstance(value, Decimal)


def test_a_refund_does_not_quietly_reduce_what_was_spent(totals_ledger):
    """Netting silently would answer "how much did I spend" with a smaller
    number than was spent. Both directions are reported, and the difference is
    labelled `net` rather than being the only figure offered."""
    usd = totals_by_currency(search(totals_ledger, "wholefoods"))["USD"]

    assert usd.spent == Decimal("140.00")
    assert usd.received == Decimal("25.00")
    assert usd.net == Decimal("115.00")
    assert usd.receipt_count == 1
    assert usd.net == usd.spent - usd.received


def test_the_render_labels_the_net_figure_as_net(totals_ledger):
    """`115.00` printed beside `spent` would be a false answer; the label is
    what makes the number safe to read."""
    rendered = search(totals_ledger, "wholefoods").render()

    assert "spent            140.00" in rendered
    assert "received          25.00" in rendered
    assert "net spend        115.00" in rendered


def test_a_transfer_between_your_own_accounts_is_excluded_and_named(totals_ledger):
    """A card payment matches on both of its funding legs. Counted, it would
    report the same 40.00 as both spent and received -- inflating spending and
    then netting it back off, so "you spent nothing at Chase" on a real
    payment."""
    usd = totals_by_currency(search(totals_ledger, "wholefoods"))["USD"]

    assert usd.transferred == Decimal("40.00")
    assert usd.transfer_count == 1
    # Counted once, on the outgoing leg: the incoming leg is the same money
    # and is in this same result set.
    assert usd.spent == Decimal("140.00")
    assert usd.received == Decimal("25.00")
    assert "moved between your own accounts" in search(totals_ledger, "wholefoods").render()


def test_a_transfer_is_still_a_match_even_though_it_is_in_no_total(totals_ledger):
    """Excluded from the figures, not hidden from the results: the user
    searched for it and it happened.

    Counted **once**. A transfer has two funding legs, so `#postings` yields a
    row for each, and this asserted 2 for a query matching a single
    transaction — the name says "a transfer" and the number said two. The card
    was rendering the payment twice, at -500 and +500, directly above a totals
    block that correctly reported it once as excluded.
    """
    result = search(totals_ledger, "card payment")

    assert result.total == 1
    assert len(result.matches) == 1
    assert totals_by_currency(result)["USD"].spent == Decimal(0)
    # The money side was always right and must stay right: the totals need to
    # see *both* legs to recognise a transfer at all, so deduping the rows the
    # user sees must not dedupe what `_amount_totals` is given.
    assert totals_by_currency(result)["USD"].transferred > Decimal(0)


def test_currencies_are_totalled_separately_and_never_added(totals_ledger):
    """There is no exchange rate in the ledger. Inventing one to produce a
    single headline figure would be worse than declining to."""
    result = search(totals_ledger, "wholefoods")
    totals = totals_by_currency(result)

    assert set(totals) == {"GBP", "USD"}
    assert totals["GBP"].spent == Decimal("30.00")
    assert totals["USD"].spent == Decimal("140.00")
    assert result.mixed_currency is True
    # No combined figure exists anywhere in the response to be misread.
    assert not any(t.spent == Decimal("170.00") for t in result.amount_totals)


def test_a_mixed_currency_result_says_so_rather_than_picking_one(totals_ledger):
    result = search(totals_ledger, "wholefoods")

    assert any("no exchange rates" in w for w in result.warnings), result.warnings
    rendered = result.render()
    assert "GBP" in rendered
    assert "USD" in rendered


def test_a_single_currency_result_is_not_flagged_as_mixed(ledger):
    result = search(ledger, "groceries")

    assert result.mixed_currency is False
    assert [t.currency for t in result.amount_totals] == ["USD"]
    assert result.warnings == []


def test_the_totals_name_the_accounts_they_span(totals_ledger):
    """A figure combining a checking account and a credit card reads as one
    account's activity unless it says otherwise."""
    usd = totals_by_currency(search(totals_ledger, "wholefoods"))["USD"]

    assert usd.accounts == ("Assets:Checking", "Liabilities:CreditCard")


def test_totals_cover_every_match_not_just_the_page_shown(ledger):
    """A total over the visible rows would understate the answer exactly when
    the result was large enough for someone to need a total."""
    full = search(ledger, "groceries")
    limited = search(ledger, "groceries", limit=1)

    assert limited.truncated is True
    assert len(limited.matches) == 1
    assert limited.amount_totals == full.amount_totals
    assert "not only the 1 shown" in limited.render()


def test_a_result_with_no_matches_has_no_totals(ledger):
    """Rather than a zero, which is a claim about money that was never
    examined."""
    result = search(ledger, "kayak")

    assert result.amount_totals == []
    assert result.mixed_currency is False


def test_a_failed_search_reports_no_totals(ledger):
    assert search(ledger, "").amount_totals == []


def test_totals_are_currency_ordered_so_repeated_searches_are_identical(totals_ledger):
    assert [t.currency for t in search(totals_ledger, "wholefoods").amount_totals] == [
        "GBP",
        "USD",
    ]


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
        "amount_totals",
        "mixed_currency",
        "warnings",
        "errors",
    }


def test_to_dict_keeps_the_totals_as_strings_too(totals_ledger):
    """Money is a string end to end; a JSON number is a double."""
    payload = search(totals_ledger, "wholefoods").to_dict()
    usd = next(t for t in payload["amount_totals"] if t["currency"] == "USD")

    assert usd["spent"] == "140.00"
    assert usd["received"] == "25.00"
    assert usd["net"] == "115.00"
    assert usd["transferred"] == "40.00"
    assert usd["accounts"] == ["Assets:Checking", "Liabilities:CreditCard"]
    assert payload["mixed_currency"] is True


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
