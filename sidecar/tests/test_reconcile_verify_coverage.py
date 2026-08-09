"""`verify` check 4: is the cash-truth guard actually armed?

§5.2 item 3's promise is that "if we ever drop or duplicate a transaction,
`bean-check` fails at the next assertion date". `verify` already enforces the
assertions that exist. Nothing enforced that any exist — and
`render_balances_file` rewrites `balances.beancount` wholesale from whatever
SimpleFIN currently reports, so an account that stops being reported loses its
assertion and the guard stops covering it with a green `verify` throughout.

This is the *only* part of reconciliation that can run unattended, because it
is the only part that needs no statement. Comparing against a real statement
needs an input `verify` does not have on every sync, which is why
`bookkeeper reconcile` is a separate command.
"""

from __future__ import annotations

from beancount import loader

from bookkeeper.envelope.verify import verify_entries

# Funded and allocated, so the only thing `verify` can have to say about
# these ledgers is check 4. Without the opening balance and the allocation,
# every one of these fixtures also trips the over-allocation error and the
# overspend note, and the assertions below would be reading those.
HEADER = """\
option "operating_currency" "USD"

2026-01-01 open Assets:SimpleFIN:Checking   USD
2026-01-01 open Assets:Cash                 USD
2026-01-01 open Equity:Opening-Balances
2026-01-01 open Expenses:Food

2026-01-01 custom "envelope" "map" "Expenses:Food" "Groceries"

2026-01-01 * "Opening"
  Assets:SimpleFIN:Checking   1000.00 USD
  Assets:Cash                  100.00 USD
  Equity:Opening-Balances

2026-01-02 custom "envelope" "allocate" "Groceries" 500.00 USD
"""

SPEND = """
2026-01-05 * "TRADER JOES"
  Assets:SimpleFIN:Checking    -40.00 USD
  Expenses:Food
"""

#: Balance of Assets:SimpleFIN:Checking at the start of 2026-01-06, i.e. what
#: an assertion dated that day must say.
COVERED_BALANCE = "960.00"


def verify(text: str):
    entries, errors, _options = loader.load_string(text)
    return verify_entries(entries, errors)


def test_a_simplefin_account_with_no_assertion_is_reported():
    result = verify(HEADER + SPEND)
    assert any(
        "no balance assertion covers" in n and "Assets:SimpleFIN:Checking" in n
        for n in result.notes
    ), result.render()


def test_an_unasserted_account_does_not_fail_the_build():
    """A note, not an error, for the reason overspend is a note: it does not
    make the books wrong, it means one guard is not armed — and a bank can
    revoke a connection, so failing every sync on it would teach the user to
    ignore a red `verify`, which costs us the checks that matter."""
    result = verify(HEADER + SPEND)
    assert result.ok, result.render()
    assert result.errors == []


def test_a_covered_account_produces_no_note():
    """The assertion is dated the day *after* the last transaction, which is
    exactly what `NormalizedBalance` emits. Comparing the two dates without
    that offset would flag a correctly-covered account on every sync."""
    result = verify(
        HEADER + SPEND + f"\n2026-01-06 balance Assets:SimpleFIN:Checking   {COVERED_BALANCE} USD\n"
    )
    assert result.notes == [], result.render()


def test_transactions_after_the_last_assertion_are_counted_as_uncovered():
    """"What the report covers" and "where the data stops" are different
    facts, and the gap between them is where a dropped transaction hides."""
    result = verify(
        HEADER
        + SPEND
        + """
2026-01-06 balance Assets:SimpleFIN:Checking   960.00 USD

2026-01-10 * "BISTRO"
  Assets:SimpleFIN:Checking    -25.00 USD
  Expenses:Food

2026-01-11 * "BISTRO"
  Assets:SimpleFIN:Checking    -25.00 USD
  Expenses:Food
"""
    )
    assert any(
        "asserted only through 2026-01-06" in n and "2 later transaction(s)" in n
        for n in result.notes
    ), result.render()


def test_a_transaction_dated_on_the_assertion_date_is_not_covered_by_it():
    """A `balance` directive dated D asserts the balance at the *start* of D,
    so it covers transactions dated strictly before it. Treating D as covered
    would report a guard as armed one day further than it is."""
    result = verify(
        HEADER
        + SPEND
        + """
2026-01-06 balance Assets:SimpleFIN:Checking   960.00 USD

2026-01-06 * "SAME DAY"
  Assets:SimpleFIN:Checking    -25.00 USD
  Expenses:Food
"""
    )
    assert any("1 later transaction(s)" in n for n in result.notes), result.render()


def test_hand_written_asset_accounts_are_not_flagged():
    """Scoped to `Assets:SimpleFIN:` — the accounts `ingest` opens and is
    responsible for asserting. A hand-curated ledger's own accounts were never
    promised an assertion, and flagging them would fire on every fixture in
    this suite: noise that trains people to stop reading the notes."""
    result = verify(
        HEADER
        + """
2026-01-05 * "CASH LUNCH"
  Assets:Cash                  -12.00 USD
  Expenses:Food
"""
    )
    # `Assets:Cash` has postings and no assertion, exactly like the SimpleFIN
    # account this ledger also holds — and only the SimpleFIN one is named.
    assert not any("Assets:Cash" in n for n in result.notes), result.render()
    assert any("Assets:SimpleFIN:Checking" in n for n in result.notes), result.render()


def test_the_existing_fixtures_gain_no_notes_from_this_check(fixture_root):
    """A guard against the version of this check that was noisy enough to be
    ignored."""
    from bookkeeper.envelope.verify import run_verify

    fixture_root("basic")
    result = run_verify()
    assert result.ok
    assert result.notes == [], result.render()
