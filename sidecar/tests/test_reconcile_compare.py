"""Unit tests for `bookkeeper.reconcile.compare`.

Reconciliation is the guard PLAN.md §5.2 item 3 keeps: the one check that
survives the envelope model, because it compares the books against an
*independent* source rather than against the feed that built them. A user
opens this command when they already suspect their books are wrong, which
makes a bug here worse than a bug almost anywhere else in the sidecar — it
does not merely fail, it hands someone a confident, wrong answer and sends
them hunting for a transaction that is fine.

So the subject of these tests is not "does it notice a difference". It is:

- that the answer is a set of *named* transactions, each pointing at a file
  and line a user can open, rather than a scalar;
- that candidates really are **alternatives**, since the render says so in
  those words and a user who added them up would be off by a multiple;
- that the three failures which produce a difference of the *same magnitude*
  — a missing transaction, a duplicate, and one the bank dated differently —
  are told apart by evidence rather than by the size of the gap;
- that a one-day boundary mismatch is never called a missing transaction.

Traps this file fell into, recorded because they each made a test pass while
proving nothing:

1. **`loader.load_string` reports no error for text it does not recognise.**
   The same trap `test_envelope_allocate.py` documents. Every fixture here
   goes through `_load`, which asserts `not errors` — without that, a typo in
   a fixture's account name yields an empty entry list, every finding list
   comes back empty, and assertions like "no MISSING finding" pass for the
   wrong reason.
2. **An opening-balance transaction dated inside the statement period is
   reported as `EXTRA`,** correctly — a bank statement does not list the day
   you opened your books. The first draft of these fixtures opened the
   account on 2026-01-01, which put a spurious 1000.00 finding in every
   result and made `explained`/`residual` meaningless everywhere. The shared
   preamble now dates it 2025-12-01, outside every period used below.
3. **`_hypotheses` only runs when there are no statement lines.** A test
   meaning to exercise the balance-only candidate path while passing a CSV
   silently exercises the line-diff path instead, which produces *confirmed*
   findings — so `result.candidates` is empty and any assertion about
   candidates passes vacuously by being an assertion about nothing.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from beancount import loader

from bookkeeper.reconcile.compare import (
    KIND_AMOUNT_MATCH,
    KIND_COUNT,
    KIND_DATE_BOUNDARY,
    KIND_DATE_SHIFT,
    KIND_DUPLICATE,
    KIND_EXTRA,
    KIND_FEED_DISAGREES,
    KIND_MISSING,
    KIND_SIGN_CONVENTION,
    MAX_CANDIDATES,
    ReconcileError,
    _flip,
    balance_asof,
    flatten_account_postings,
    reconcile_entries,
    resolve_account,
    run_reconcile,
)
from bookkeeper.reconcile.statement import Statement, parse_statement_csv

ACCOUNT = "Assets:Checking"

#: Opened in 2025 on purpose. See trap 2 in the module docstring: an opening
#: balance dated inside the reconciled period is a legitimate `EXTRA` finding
#: and would contaminate every assertion about findings below.
PREAMBLE = """
option "operating_currency" "USD"
2025-01-01 open Assets:Checking USD
2025-01-01 open Assets:Savings USD
2025-01-01 open Expenses:Food USD
2025-01-01 open Equity:Opening-Balances

2025-12-01 * "Opening balance"
  Assets:Checking   1000.00 USD
  Equity:Opening-Balances
"""

#: Balance of `Assets:Checking` before any test's own transactions.
OPENING = Decimal("1000.00")


def _load(body: str):
    """`(entries, options)` for a fixture ledger, or a failed assertion.

    The `assert not errors` is load-bearing rather than tidy: beancount
    silently ignores text it cannot parse as a directive, so a fixture with a
    misspelled account name would load to *nothing* and every "no such
    finding" assertion below would pass against an empty ledger.
    """
    entries, errors, options = loader.load_string(PREAMBLE + body)
    assert not errors, errors
    return entries, options


def txn(when: str, narration: str, amount: str, *, sfid: str | None = None) -> str:
    meta = (
        f'  simplefin-id: "{sfid}"\n  simplefin-account: "{ACCOUNT}"\n' if sfid else ""
    )
    return (
        f'{when} * "{narration}"\n{meta}'
        f"  {ACCOUNT}   {amount} USD\n"
        f"  Expenses:Food\n\n"
    )


def csv_statement(rows: str, balance: str | None, **kwargs) -> Statement:
    """A parsed CSV statement over the January 2026 period used throughout."""
    kwargs.setdefault("closing_date", date(2026, 1, 31))
    kwargs.setdefault("from_date", date(2026, 1, 1))
    return parse_statement_csv(
        "Date,Description,Amount\n" + rows,
        account=ACCOUNT,
        closing_balance=None if balance is None else Decimal(balance),
        **kwargs,
    )


def balance_only(balance: str, on: date = date(2026, 1, 31), **kwargs) -> Statement:
    return Statement(
        account=ACCOUNT,
        closing_date=on,
        closing_balance=Decimal(balance),
        source="balance only",
        **kwargs,
    )


def kinds(result) -> list[str]:
    return [f.kind for f in result.findings]


def only(result, kind: str):
    """The single finding of `kind`, asserting there is exactly one."""
    found = [f for f in result.findings if f.kind == kind]
    assert len(found) == 1, f"expected one {kind}, got {kinds(result)}"
    return found[0]


def only_confirmed(result, kind: str):
    """The single *observed* finding of `kind`.

    Separate from `only` because confirmed findings and hypotheses are
    different claims about the same ledger, and a test about one of them
    should not fail because the other path also had something to say.
    """
    found = [f for f in result.findings if f.kind == kind and f.confirmed]
    assert len(found) == 1, f"expected one confirmed {kind}, got {kinds(result)}"
    return found[0]


@pytest.fixture
def ledger_root(tmp_path, monkeypatch):
    """A throwaway single-file ledger under `BOOKKEEPER_ROOT`.

    Only the `run_reconcile` tests need this; everything else drives
    `reconcile_entries`, which is pure. Nothing here writes to the ledger —
    reconciliation is read-only — but pointing `BOOKKEEPER_ROOT` at a temp
    tree keeps the real one unreachable regardless.
    """

    def _use(body: str) -> Path:
        ledger = tmp_path / "ledger"
        ledger.mkdir(parents=True, exist_ok=True)
        (ledger / "main.beancount").write_text(PREAMBLE + body, encoding="utf-8")
        monkeypatch.setenv("BOOKKEEPER_ROOT", str(tmp_path))
        return tmp_path

    return _use


# =========================================================================
# The three failures that produce a difference of the same magnitude.
#
# This is the reason the module exists. A report that says "you disagree by
# 47.13" cannot distinguish these, and the remedy for each is different: run
# a sync, delete a line, or do nothing at all because the money is already
# right. Every scenario below is built to differ by exactly 47.13.
# =========================================================================

GAP = Decimal("47.13")

#: Two transactions both sides agree about, so the fixtures differ only in
#: the one transaction under test.
AGREED_LEDGER = txn("2026-01-05", "TRADER JOES", "-80.00") + txn(
    "2026-01-12", "SHELL OIL", "-30.00"
)
AGREED_ROWS = "2026-01-05,TRADER JOES,-80.00\n2026-01-12,SHELL OIL,-30.00\n"
#: 1000.00 - 80.00 - 30.00
AGREED_BALANCE = Decimal("890.00")


def _missing_case():
    """The bank has a transaction the ledger never imported."""
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(
        AGREED_ROWS + "2026-01-20,COSTCO,-47.13\n", str(AGREED_BALANCE - GAP)
    )
    return reconcile_entries(entries, statement, options=options)


def _duplicate_case():
    """The ledger imported one transaction twice."""
    entries, options = _load(
        AGREED_LEDGER
        + txn("2026-01-20", "COSTCO", "-47.13")
        + txn("2026-01-21", "COSTCO", "-47.13")
    )
    statement = csv_statement(
        AGREED_ROWS + "2026-01-20,COSTCO,-47.13\n", str(AGREED_BALANCE - GAP)
    )
    return reconcile_entries(entries, statement, options=options)


def _redated_case():
    """The bank dated it 31 January; the ledger dates it 1 February."""
    entries, options = _load(AGREED_LEDGER + txn("2026-02-01", "COSTCO", "-47.13"))
    statement = csv_statement(
        AGREED_ROWS + "2026-01-31,COSTCO,-47.13\n", str(AGREED_BALANCE - GAP)
    )
    return reconcile_entries(entries, statement, options=options)


def test_the_three_failures_are_indistinguishable_by_the_size_of_the_gap():
    """The premise of everything below, asserted rather than assumed.

    If these three scenarios ever stopped agreeing on `abs(delta)`, the tests
    that follow would be distinguishing them by magnitude without anybody
    noticing, and would pass even if the classification logic were deleted.
    """
    deltas = {abs(case().delta) for case in (_missing_case, _duplicate_case, _redated_case)}
    assert deltas == {GAP}


def test_a_transaction_the_ledger_never_imported_is_reported_as_missing():
    result = _missing_case()

    finding = only(result, KIND_MISSING)
    assert finding.confirmed is True
    assert finding.delta == -GAP
    # Named, not merely counted: the statement row the user can go and look at.
    assert [line.row for line in finding.statement_lines] == [4]
    assert finding.statement_lines[0].description == "COSTCO"
    assert "never imported" in finding.explanation
    assert "sync" in finding.explanation
    # Nothing in the ledger resembles it, so nothing is called a duplicate.
    assert KIND_DUPLICATE not in kinds(result)
    assert result.explained == -GAP
    assert result.residual == 0
    assert result.ok is False


def test_a_transaction_imported_twice_is_reported_as_a_duplicate():
    """Opposite sign to the missing case above, same magnitude. The sign
    cannot tell them apart either — a missing debit and a duplicated credit
    both leave the ledger reading high — so the classification has to come
    from the evidence, and the evidence is the twin entry."""
    result = _duplicate_case()

    finding = only(result, KIND_DUPLICATE)
    assert finding.confirmed is True
    assert finding.delta == GAP
    # Both copies are named, because the user has to choose which to delete.
    assert [e.date for e in finding.ledger_entries] == [date(2026, 1, 21), date(2026, 1, 20)]
    assert all(e.location.endswith(tuple("0123456789")) for e in finding.ledger_entries)
    assert KIND_MISSING not in kinds(result)
    assert result.explained == GAP
    assert result.residual == 0
    assert result.ok is False


def test_a_transaction_the_bank_dated_a_day_earlier_is_not_called_missing():
    """The most common real failure, and the one with the worst wrong answer.

    `NormalizedBalance.assertion_date` is already `balance-date + 1` and banks
    post at odd hours, so a transaction the bank dated the 31st reaching the
    ledger dated the 1st is ordinary. Reported as MISSING it would send
    someone to re-sync a transaction that is sitting in their ledger,
    correct, one line below where they looked.
    """
    result = _redated_case()

    finding = only(result, KIND_DATE_BOUNDARY)
    assert finding.confirmed is True
    assert finding.delta == -GAP
    assert KIND_MISSING not in kinds(result)
    assert KIND_DUPLICATE not in kinds(result)
    # Both sides of the pair are named, which is what makes it checkable.
    assert finding.ledger_entries[0].date == date(2026, 2, 1)
    assert finding.statement_lines[0].posted_date == date(2026, 1, 31)
    assert "only the date differs" in finding.explanation
    assert "not a missing transaction" in finding.explanation
    assert result.residual == 0


def test_the_one_day_pair_is_matched_rather_than_left_on_both_sides():
    """The failure mode this replaces is subtler than a mislabel: an unmatched
    pair would be counted as a missing transaction *and* an extra one, so the
    match count would under-report and the finding list would carry two rows
    for one transaction."""
    result = _redated_case()

    assert result.matched == 3
    assert result.statement_lines == 3
    assert KIND_EXTRA not in kinds(result)


def test_a_shift_that_stays_inside_the_statement_moves_no_money():
    """Both dates inside the period: the closing balance is unaffected, so the
    finding is reported with a zero delta and does not fail the run. Treating
    it as a discrepancy would give a red exit code to a ledger that is right."""
    entries, options = _load(AGREED_LEDGER + txn("2026-01-22", "COSTCO", "-47.13"))
    statement = csv_statement(
        AGREED_ROWS + "2026-01-20,COSTCO,-47.13\n", str(AGREED_BALANCE - GAP)
    )
    result = reconcile_entries(entries, statement, options=options)

    finding = only(result, KIND_DATE_SHIFT)
    assert finding.delta == 0
    assert "2 day(s) later" in finding.explanation
    assert "no money is affected" in finding.explanation
    assert result.delta == 0
    assert result.ok is True


def test_an_entry_with_no_counterpart_and_no_twin_is_extra_not_duplicate():
    """`EXTRA` and `DUPLICATE` differ only in whether a twin was found, and
    the remedy differs completely — "delete one of these two" against "the
    bank has not posted this yet". Calling a lone entry a duplicate invites
    someone to delete a real transaction."""
    entries, options = _load(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE + GAP))
    result = reconcile_entries(entries, statement, options=options)

    finding = only(result, KIND_EXTRA)
    assert finding.confirmed is True
    assert finding.ledger_entries[0].description == "COSTCO"
    assert "not a duplicate" in finding.explanation
    assert KIND_DUPLICATE not in kinds(result)


def test_two_entries_days_apart_with_unlike_descriptions_are_not_a_duplicate():
    """A fortnightly bill and a coincidence of amount both live here. The cost
    of a false "you imported this twice" is someone deleting a transaction
    that really happened, so the description test is what keeps the finding
    honest."""
    entries, options = _load(
        AGREED_LEDGER
        + txn("2026-01-20", "COSTCO WHOLESALE", "-47.13")
        + txn("2026-01-21", "PACIFIC GAS AND ELECTRIC", "-47.13")
    )
    statement = csv_statement(
        AGREED_ROWS + "2026-01-20,COSTCO WHOLESALE,-47.13\n", str(AGREED_BALANCE - GAP)
    )
    result = reconcile_entries(entries, statement, options=options)

    assert KIND_DUPLICATE not in kinds(result)
    assert only(result, KIND_EXTRA).ledger_entries[0].description == (
        "PACIFIC GAS AND ELECTRIC"
    )


# =========================================================================
# Candidates: the balance-only path, where nothing can be observed.
# =========================================================================


def test_a_bare_balance_still_narrows_the_gap_to_named_ledger_entries():
    """"They disagree by 47.13" is not an answer. With no file at all — the
    only case every user can produce — the module still has to come back with
    transactions the user can open."""
    entries, options = _load(
        AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13")
    )
    result = reconcile_entries(entries, balance_only(str(AGREED_BALANCE)), options=options)

    assert result.delta == GAP
    candidate = only(result, KIND_AMOUNT_MATCH)
    assert candidate.confirmed is False
    assert candidate.ledger_entries[0].description == "COSTCO"
    assert candidate.ledger_entries[0].location != "(unknown)"
    assert re.search(r":\d+$", candidate.ledger_entries[0].location), (
        "a candidate must point at a line a user can open"
    )


def test_candidates_are_alternatives_rather_than_a_list_to_add_up():
    """The render says so in those words, and the claim has to be true.

    Four entries of the same amount each account for the *whole* gap on their
    own. A reader who summed the column would be off by a factor of four, and
    the arithmetic in the report (`explained` / `residual`) must not make that
    mistake either: unconfirmed findings contribute nothing to `explained`.
    """
    entries, options = _load(
        txn("2026-01-04", "ALPHA MARKET", "-47.13")
        + txn("2026-01-09", "BRAVO HARDWARE", "-47.13")
        + txn("2026-01-14", "CHARLIE BOOKS", "-47.13")
        + txn("2026-01-19", "DELTA PHARMACY", "-47.13")
    )
    ledger_balance = OPENING - 4 * GAP
    result = reconcile_entries(
        entries, balance_only(str(ledger_balance + GAP)), options=options
    )

    assert result.delta == GAP
    assert len(result.candidates) == 4
    # Each one alone closes the gap...
    assert {c.delta for c in result.candidates} == {GAP}
    # ...so their sum is emphatically not the difference.
    assert sum(c.delta for c in result.candidates) == 4 * GAP
    # And the report's own arithmetic does not add them up.
    assert result.explained == 0
    assert result.residual == GAP
    assert result.confirmed_findings == ()


def test_the_candidate_heading_tells_the_reader_not_to_add_them_up():
    entries, options = _load(
        txn("2026-01-04", "ALPHA MARKET", "-47.13")
        + txn("2026-01-09", "BRAVO HARDWARE", "-47.13")
    )
    result = reconcile_entries(
        entries, balance_only(str(OPENING - 2 * GAP + GAP)), options=options
    )
    text = result.render()

    assert "They are alternatives, not a list to add up" in text
    assert "ALPHA MARKET" in text and "BRAVO HARDWARE" in text
    # Candidates are never printed under the heading that *is* a sum.
    assert "What accounts for the difference" not in text


def test_a_boundary_candidate_is_offered_before_the_bare_amount_match():
    """Ordering is the module's advice, not a detail. An entry days from the
    cutoff whose amount is the gap is very likely a posting-date difference —
    where the money is already right and there is nothing to fix — and it is
    the cheapest thing to check. Offering "this might not belong in your
    ledger" first invites a deletion that would create the error.
    """
    entries, options = _load(txn("2026-01-30", "COSTCO", "-47.13"))
    result = reconcile_entries(
        entries, balance_only(str(OPENING - GAP + GAP)), options=options
    )

    assert kinds(result) == [KIND_DATE_BOUNDARY]
    finding = result.findings[0]
    assert finding.confirmed is False
    assert "not a duplicate" in finding.explanation
    # The entry is claimed by the boundary hypothesis, so it is not offered a
    # second time as a bare amount match. Two candidates naming one entry read
    # as two things to check.
    assert KIND_AMOUNT_MATCH not in kinds(result)


def test_a_transaction_dated_a_day_the_wrong_side_is_a_re_dating_candidate():
    """Balance-only counterpart to the CSV boundary case: the ledger counts a
    31 January purchase, the bank has not posted it. Same magnitude as a
    genuinely missing transaction and a completely different remedy."""
    entries, options = _load(AGREED_LEDGER + txn("2026-01-31", "COSTCO", "-47.13"))
    result = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE)), options=options
    )

    assert result.delta == GAP
    finding = only(result, KIND_DATE_BOUNDARY)
    assert finding.ledger_entries[0].date == date(2026, 1, 31)
    assert KIND_MISSING not in kinds(result)


def test_nothing_near_the_cutoff_is_not_dressed_up_as_a_re_dating():
    """The contrast that gives the test above its meaning. Same gap, same
    balance-only statement — but with no entry to re-date and none to remove,
    the honest answer is "a transaction is absent", and it says how to name
    it."""
    entries, options = _load(AGREED_LEDGER)
    result = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE - GAP)), options=options
    )

    assert kinds(result) == [KIND_MISSING]
    finding = result.findings[0]
    assert finding.confirmed is False
    assert finding.delta == -GAP
    assert finding.ledger_entries == ()
    assert "nothing in the ledger accounts for this difference" in finding.explanation
    assert "--statement" in finding.explanation


def test_a_boundary_candidate_is_offered_only_within_the_match_window():
    """Seven days out is a different transaction that happens to cost the
    same, and calling it a posting-date difference would be a guess dressed
    as a diagnosis."""
    entries, options = _load(txn("2026-01-20", "COSTCO", "-47.13"))
    result = reconcile_entries(
        entries, balance_only(str(OPENING - GAP + GAP)), options=options
    )

    assert kinds(result) == [KIND_AMOUNT_MATCH]


def test_more_candidates_than_fit_are_capped_and_the_remainder_is_counted():
    """A dump is where a real finding hides — but silently dropping evidence
    from a report about someone's money is worse, so the count survives."""
    names = ["ALPHA", "BRAVO", "CHARLIE", "DELTA", "ECHO", "FOXTROT", "GOLF", "HOTEL"]
    days = [1, 2, 6, 10, 14, 18, 22, 26]
    body = "".join(
        txn(f"2026-01-{day:02d}", name, "-47.13") for day, name in zip(days, names)
    )
    entries, options = _load(body)
    result = reconcile_entries(
        entries, balance_only(str(OPENING - 8 * GAP + GAP)), options=options
    )

    assert len(result.findings) == MAX_CANDIDATES
    assert any(
        f"2 further {KIND_AMOUNT_MATCH} finding(s) not shown" in note
        for note in result.notes
    ), result.notes


# =========================================================================
# The dedup key: the one duplicate that needs no statement and no hypothesis.
# =========================================================================


def test_a_repeated_simplefin_id_is_a_confirmed_duplicate_without_a_statement():
    """`simplefin-id` + `simplefin-account` is the import dedup key
    (`ingest/render.py`), so a repeat inside one account is not a resemblance
    to be judged — it is a broken invariant, and the only duplicate finding
    that is *observed* rather than inferred from the size of a gap.

    Asserted over the *confirmed* finding only. The balance-only path also
    runs `_hypotheses` over the same gap, and what it offers alongside this
    is a separate question from whether the observation itself is right.
    """
    entries, options = _load(
        txn("2026-01-10", "COSTCO", "-47.13", sfid="tx-2")
        + txn("2026-01-11", "COSTCO", "-47.13", sfid="tx-2")
    )
    result = reconcile_entries(
        entries, balance_only(str(OPENING - GAP)), options=options
    )

    finding = only_confirmed(result, KIND_DUPLICATE)
    assert finding.confirmed is True, "a repeated dedup key is observed, not hypothesised"
    assert finding.delta == GAP
    assert len(finding.ledger_entries) == 2
    assert "share the SimpleFIN id 'tx-2'" in finding.explanation
    assert result.ok is False


def test_a_repeated_dedup_key_is_reported_even_when_the_balances_agree():
    """A defect in the import invariant is worth knowing about whether or not
    it happens to explain this statement's arithmetic. Reporting it only when
    it moves the balance would hide every duplicate that cancels."""
    entries, options = _load(
        txn("2026-01-10", "COSTCO", "-47.13", sfid="tx-2")
        + txn("2026-01-11", "COSTCO", "-47.13", sfid="tx-2")
    )
    result = reconcile_entries(
        entries, balance_only(str(OPENING - 2 * GAP)), options=options
    )

    assert result.delta == 0
    assert result.reconciled is True
    assert only_confirmed(result, KIND_DUPLICATE)
    assert result.ok is False, "a broken dedup key is not an OK reconciliation"


def test_distinct_simplefin_ids_are_not_a_duplicate():
    """Two genuinely different transactions of the same amount, days apart,
    with the same merchant name — the exact shape `_looks_like_duplicate_of`
    would flag. The dedup-key path must not add to that on its own evidence."""
    entries, options = _load(
        txn("2026-01-10", "COSTCO", "-47.13", sfid="tx-2")
        + txn("2026-01-11", "COSTCO", "-47.13", sfid="tx-3")
    )
    result = reconcile_entries(
        entries, balance_only(str(OPENING - 2 * GAP)), options=options
    )

    assert kinds(result) == []


#: A charge imported twice under one dedup key — the shape every genuine
#: double-import takes, since every imported transaction carries a
#: `simplefin-id`.
DOUBLE_IMPORT = txn("2026-01-20", "COSTCO", "-47.13", sfid="tx-2") + txn(
    "2026-01-20", "COSTCO", "-47.13", sfid="tx-2"
)


def test_a_double_import_is_counted_once_when_a_statement_lists_the_charge():
    """Regression. Two confirmed findings used to describe the same pair — the
    dedup-key check reporting the broken invariant, and the line diff
    rediscovering the leftover copy as a resemblance — and *both* deltas were
    summed into `explained`. The arithmetic then double-counted the fix and
    charged the difference to a phantom discrepancy before the period.

    That is the worst failure available to this module. It found the entire
    cause and then sent the user somewhere else to look for it, with an
    `Accounted for` figure that cannot be right on its face. Someone
    reconciling already suspects their books are wrong; a confident wrong
    destination is worse than no answer.
    """
    entries, options = _load(DOUBLE_IMPORT)
    # The bank has the charge once, so its closing balance is one GAP below
    # the opening — and the ledger, holding it twice, is a further GAP below.
    statement = csv_statement("2026-01-20,COSTCO,-47.13\n", str(OPENING - GAP))
    result = reconcile_entries(entries, statement, options=options)

    assert kinds(result).count(KIND_DUPLICATE) == 1, kinds(result)
    finding = only_confirmed(result, KIND_DUPLICATE)
    assert "share the SimpleFIN id 'tx-2'" in finding.explanation

    # The arithmetic closes exactly: the duplicate is the whole difference.
    assert result.delta == GAP
    assert result.explained == GAP
    assert result.residual == 0
    assert not any("not accounted for by anything" in n for n in result.notes)


def test_only_the_spurious_copies_of_a_double_import_are_absorbed():
    """The other half of the fix, and the reason it counts copies rather than
    suppressing the whole dedup group: one member of the group is a real
    transaction. When the bank does not list it either, that member is a
    genuine `EXTRA` and still has to be reported — over-correcting here would
    trade a double-count for a silent omission.
    """
    entries, options = _load(txn("2026-01-05", "TRADER JOES", "-80.00") + DOUBLE_IMPORT)
    # The statement knows about the groceries and nothing else.
    statement = csv_statement("2026-01-05,TRADER JOES,-80.00\n", str(OPENING - 80))
    result = reconcile_entries(entries, statement, options=options)

    assert kinds(result).count(KIND_DUPLICATE) == 1, kinds(result)
    assert kinds(result).count(KIND_EXTRA) == 1, kinds(result)
    assert result.delta == 2 * GAP
    assert result.explained == 2 * GAP
    assert result.residual == 0


def test_a_confirmed_duplicate_is_not_followed_by_a_candidate_contradicting_it():
    """Regression. `_hypotheses` used to be handed the whole gap even when a
    confirmed finding had already explained all of it. Finding nothing left to
    claim, it fell through to its last-resort branch and printed "nothing in
    the ledger accounts for this difference" directly beneath a finding that
    accounted for every cent of it.

    "Unexplained 0.00" and "nothing accounts for this" cannot both be true,
    and a report that contradicts itself on the same screen is one a user
    stops believing — including the half of it that was right.
    """
    entries, options = _load(DOUBLE_IMPORT)
    result = reconcile_entries(
        entries, balance_only(str(OPENING - GAP)), options=options
    )

    assert only_confirmed(result, KIND_DUPLICATE).delta == GAP
    assert result.explained == GAP
    assert result.residual == 0
    assert result.candidates == (), kinds(result)
    assert KIND_MISSING not in kinds(result)

    text = result.render()
    assert "Unexplained            0.00" in text
    assert "nothing in the ledger accounts for this difference" not in text


def test_a_partly_explained_gap_still_hypothesises_about_the_remainder():
    """The complement of the test above: suppressing the fallback must not
    suppress the search. With a confirmed duplicate covering part of the gap,
    the candidates offered are for what is *left*, not for the whole
    difference — hunting the full delta would look for an explanation of
    something already explained."""
    entries, options = _load(
        DOUBLE_IMPORT + txn("2026-01-22", "HARDWARE", "-12.00")
    )
    # Duplicate accounts for GAP; the bank also never posted the 12.00.
    result = reconcile_entries(
        entries, balance_only(str(OPENING - GAP)), options=options
    )

    assert result.explained == GAP
    assert result.residual == Decimal("12.00")
    candidate = only(result, KIND_AMOUNT_MATCH)
    assert candidate.confirmed is False
    assert candidate.ledger_entries[0].description == "HARDWARE"
    assert candidate.delta == Decimal("12.00")


# =========================================================================
# `ok` vs `reconciled`: a right total is not a right ledger.
# =========================================================================


def test_a_missing_and_a_duplicate_that_cancel_out_still_fail():
    """The single most valuable thing this module does that a balance
    assertion cannot. Two errors of equal and opposite size leave the closing
    balance exactly right; `bean-check` passes, the delta is zero, and the
    books are wrong in two places.
    """
    entries, options = _load(
        txn("2026-01-05", "TRADER JOES", "-80.00")
        + txn("2026-01-10", "SHELL OIL", "-47.13")
        + txn("2026-01-11", "SHELL OIL", "-47.13")
    )
    statement = csv_statement(
        "2026-01-05,TRADER JOES,-80.00\n"
        "2026-01-10,SHELL OIL,-47.13\n"
        "2026-01-20,COSTCO,-47.13\n",
        "825.74",
    )
    result = reconcile_entries(entries, statement, options=options)

    assert result.delta == 0
    assert result.reconciled is True, "the totals do agree — that is the trap"
    assert result.ok is False, "but the ledger does not agree with the bank"
    assert set(kinds(result)) == {KIND_MISSING, KIND_DUPLICATE}
    assert "DOES NOT MATCH" in result.render()


def test_a_clean_reconciliation_is_ok_and_says_so():
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE))
    result = reconcile_entries(entries, statement, options=options)

    assert result.delta == 0
    assert result.ok is True
    assert result.reconciled is True
    assert result.findings == ()
    assert result.matched == 2
    text = result.render()
    assert "agrees with the statement to the cent" in text
    assert "reconcile: OK" in text


def test_a_transaction_count_catches_what_a_balance_cannot():
    """Two errors that cancel leave the balance right; the bank's own printed
    transaction count does not. This is the check a user can run from a paper
    statement with no export at all."""
    entries, options = _load(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    statement = balance_only(
        str(AGREED_BALANCE - GAP), from_date=date(2026, 1, 1), count=2
    )
    result = reconcile_entries(entries, statement, options=options)

    assert result.delta == 0
    finding = only(result, KIND_COUNT)
    assert finding.confirmed is True
    # A count says nothing about amounts, so it explains none of the gap.
    assert finding.delta == 0
    assert "says 2 transaction(s)" in finding.explanation
    assert "the ledger has 3" in finding.explanation
    assert result.ok is False


def test_a_matching_transaction_count_raises_nothing():
    entries, options = _load(AGREED_LEDGER)
    statement = balance_only(str(AGREED_BALANCE), from_date=date(2026, 1, 1), count=2)
    result = reconcile_entries(entries, statement, options=options)

    assert kinds(result) == []
    assert result.ok is True


def test_the_feed_disagreeing_with_the_bank_is_reported_as_upstream():
    """The most valuable line in the report when it appears: the ledger is a
    faithful record of a feed that is itself wrong, and no amount of hunting
    through transactions will find it."""
    entries, options = _load(
        AGREED_LEDGER + "2026-02-01 balance Assets:Checking 890.00 USD\n"
    )
    result = reconcile_entries(
        entries, balance_only("885.00"), options=options
    )

    finding = only(result, KIND_FEED_DISAGREES)
    assert finding.confirmed is True
    # It locates a discrepancy rather than explaining one, so it moves nothing.
    assert finding.delta == 0
    assert "the discrepancy is upstream of the ledger" in finding.explanation
    assert "SimpleFIN" in finding.explanation


def test_a_feed_assertion_that_agrees_is_not_reported():
    entries, options = _load(
        AGREED_LEDGER + "2026-02-01 balance Assets:Checking 890.00 USD\n"
    )
    result = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE)), options=options
    )

    assert KIND_FEED_DISAGREES not in kinds(result)


# =========================================================================
# Dates: the off-by-one that runs through the whole system.
# =========================================================================


def test_the_beancount_assertion_date_is_the_day_after_the_closing_date():
    """A `balance` directive asserts the balance at the *start* of its date,
    so "my balance at the end of the 31st" is a directive dated the 1st —
    exactly what `NormalizedBalance.assertion_date` already does. Both dates
    are carried so a user comparing this report against `balances.beancount`
    is not left to rediscover the offset."""
    entries, options = _load(AGREED_LEDGER)
    result = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE)), options=options
    )

    assert result.closing_date == date(2026, 1, 31)
    assert result.assertion_date == date(2026, 2, 1)
    text = result.render()
    assert "2026-02-01 balance Assets:Checking" in text
    assert "dated the day *after* the closing date" in text


def test_the_ledger_balance_is_read_at_the_end_of_the_closing_date():
    """Off by one here and every reconciliation in the system is wrong by
    whatever posted on the closing day."""
    entries, _options = _load(txn("2026-01-31", "COSTCO", "-47.13"))
    ledger_entries = flatten_account_postings(entries, ACCOUNT)

    assert balance_asof(ledger_entries, date(2026, 1, 31)) == OPENING - GAP
    assert balance_asof(ledger_entries, date(2026, 1, 30)) == OPENING
    assert balance_asof(ledger_entries, None) == OPENING - GAP


def test_the_period_report_counts_what_it_covers_not_what_it_matched_against():
    """The distinction that bit Phase 5 three times: the window asked for, the
    window made available, and the range the data spans are three different
    facts. Entries are widened by the tolerance so a one-day shift can pair
    up — but the report claims the period, so a February transaction must not
    inflate "ledger entries in the period"."""
    entries, options = _load(AGREED_LEDGER + txn("2026-02-02", "RENT", "-1200.00"))
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE))
    result = reconcile_entries(entries, statement, options=options)

    assert result.from_date == date(2026, 1, 1)
    assert result.to_date == date(2026, 1, 31)
    assert result.ledger_entries == 2, "the February entry is not in the period"


def test_an_unmatched_entry_in_the_tolerance_skirt_is_dropped_not_reported():
    """`window` reaches three days past the period so a shifted transaction
    can still pair up. Entries in that skirt that pair with nothing are
    ordinary transactions from the following week, and reporting them as "in
    the ledger, not on the statement" would file a page of false findings
    under the heading a user reads first."""
    entries, options = _load(
        AGREED_LEDGER
        + txn("2026-02-02", "RENT", "-1200.00")
        + txn("2026-01-20", "COSTCO", "-47.13")
    )
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE + GAP))
    result = reconcile_entries(entries, statement, options=options)

    extras = [f for f in result.findings if f.kind == KIND_EXTRA]
    assert [e.description for f in extras for e in f.ledger_entries] == ["COSTCO"], (
        "only the in-period entry is a finding; the February one is not"
    )


def test_a_stated_period_start_covers_the_days_before_the_first_transaction():
    """A statement period starting the 1st whose first line is the 5th still
    speaks for those four quiet days. Without that, a ledger entry on the 2nd
    reads as one the statement omitted, when in truth nothing happened."""
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE))

    assert statement.covers_from == date(2026, 1, 1)
    assert reconcile_entries(entries, statement, options=options).from_date == (
        date(2026, 1, 1)
    )


# =========================================================================
# Sign conventions, and `--flip-signs`.
# =========================================================================

#: The same two transactions, exported with withdrawals as positive numbers.
INVERTED_ROWS = "2026-01-05,TRADER JOES,80.00\n2026-01-12,SHELL OIL,30.00\n"


def test_an_inverted_export_is_detected_and_nothing_is_flipped_automatically():
    """A debit-positive CSV is the one parsing mistake that produces a
    fully-populated, entirely wrong report. Negating a user's money on a
    heuristic is the silent wrongness this module exists to catch, so the
    detection is a sentence and a flag, never an action."""
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(INVERTED_ROWS, str(AGREED_BALANCE))
    result = reconcile_entries(entries, statement, options=options)

    finding = only(result, KIND_SIGN_CONVENTION)
    assert finding.delta == 0
    assert "2 of 2 lines match the ledger with their signs flipped" in finding.explanation
    assert "Nothing was flipped automatically" in finding.explanation
    assert "--flip-signs" in finding.explanation
    # Nothing was flipped, so the lines really did fail to match. Without
    # this the test would pass against a module that detected the inversion
    # and then quietly applied it — the one outcome the sentence rules out.
    assert result.matched == 0
    assert kinds(result).count(KIND_MISSING) == 2


def test_the_sign_warning_is_printed_above_the_findings_it_invalidates():
    """When this fires, every finding below it is an artifact of reading the
    file backwards. Printed in its ordinary place it sits under a list a
    reader has already started acting on."""
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(INVERTED_ROWS, str(AGREED_BALANCE))
    text = reconcile_entries(entries, statement, options=options).render()

    assert "signs look inverted" in text
    assert text.index("signs look inverted") < text.index("MISSING")


def test_a_correctly_signed_export_raises_no_sign_warning():
    """The other orientation. Without this, a detector that fired on every
    file would pass the test above."""
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE))
    result = reconcile_entries(entries, statement, options=options)

    assert KIND_SIGN_CONVENTION not in kinds(result)
    assert "signs look inverted" not in result.render()


def test_flip_reverses_every_line_and_says_it_did():
    statement = csv_statement(INVERTED_ROWS, str(AGREED_BALANCE))
    flipped = _flip(statement)

    assert [line.amount for line in flipped.lines] == [Decimal("-80.00"), Decimal("-30.00")]
    assert any("sign-flipped" in w for w in flipped.warnings)


def test_flip_leaves_the_closing_balance_alone():
    """A debit-positive export is a per-line column convention, not a claim
    that the account's balance is negated. Flipping the balance too would turn
    a cosmetic import quirk into a report about someone owing money they
    have."""
    statement = csv_statement(INVERTED_ROWS, "890.00")

    assert _flip(statement).closing_balance == Decimal("890.00")


def test_flipping_an_inverted_export_reconciles_it(ledger_root, tmp_path):
    """End to end through `run_reconcile`, because the flag is only useful if
    the wiring from the CLI reaches `_flip`."""
    ledger_root(AGREED_LEDGER)
    export = tmp_path / "export.csv"
    export.write_text("Date,Description,Amount\n" + INVERTED_ROWS, encoding="utf-8")

    as_written = run_reconcile(
        ACCOUNT, balance="890.00", on="2026-01-31", statement=str(export)
    )
    flipped = run_reconcile(
        ACCOUNT,
        balance="890.00",
        on="2026-01-31",
        statement=str(export),
        flip_signs=True,
    )

    assert as_written.ok is False
    assert flipped.ok is True, flipped.render()
    assert flipped.matched == 2
    assert flipped.delta == 0
    assert flipped.findings == ()


# =========================================================================
# Money is a string, and never a float.
# =========================================================================


def test_every_money_field_crosses_as_a_string():
    """These reach a browser. JSON numbers are doubles, and a cent lost to a
    double in a reconciliation report is a cent someone then goes looking
    for."""
    entries, options = _load(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    statement = csv_statement(
        AGREED_ROWS + "2026-01-21,COSTCO,-47.13\n", str(AGREED_BALANCE - GAP)
    )
    payload = reconcile_entries(entries, statement, options=options).to_dict()

    for key in (
        "statement_balance",
        "ledger_balance",
        "delta",
        "explained",
        "residual",
    ):
        assert isinstance(payload[key], str), f"{key} is {type(payload[key])}"
    for finding in payload["findings"]:
        assert isinstance(finding["delta"], str)
        for entry in finding["ledger_entries"]:
            assert isinstance(entry["amount"], str)
        for line in finding["statement_lines"]:
            assert isinstance(line["amount"], str)


def test_a_missing_closing_balance_serializes_as_null_not_zero():
    """`None` and `0` are different answers: "the balances were not compared"
    against "they agree exactly"."""
    entries, options = _load(AGREED_LEDGER)
    statement = csv_statement(AGREED_ROWS, None)
    payload = reconcile_entries(entries, statement, options=options).to_dict()

    assert payload["delta"] is None
    assert payload["statement_balance"] is None
    assert payload["reconciled"] is False


#: A number written in exponent form, e.g. `0E+2` or `-1.5e-3`. Matched as a
#: whole string so prose containing an "e" is not a false positive. Same shape
#: as `test_reports_monthend.py`, which is where this reached a browser.
EXPONENT_FORM = re.compile(r"^[+-]?\d+(\.\d+)?[eE][+-]?\d+$")


def _string_values(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _string_values(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _string_values(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, value


def test_no_figure_leaves_this_module_in_exponent_notation():
    """`Decimal(0) / Decimal("150.00")` is `Decimal("0E+2")`, which serializes
    as the string `"0E+2"` — it parses as a number and compares as garbage,
    and it reached a browser once already in Phase 5.

    Nothing in `compare.py` divides today, so this is a standing guard rather
    than a live catch: the first ratio anyone adds here (a match rate, a
    percentage of the gap explained) fails it without quantizing. Every string
    in the payload is scanned rather than the fields that hold money, because
    the next one will be a different field.
    """
    entries, options = _load(
        AGREED_LEDGER
        + txn("2026-01-20", "COSTCO", "-47.13")
        + txn("2026-01-21", "COSTCO", "-47.13")
    )
    for statement in (
        csv_statement(AGREED_ROWS, str(AGREED_BALANCE)),
        csv_statement(AGREED_ROWS + "2026-01-20,COSTCO,-47.13\n", str(AGREED_BALANCE)),
        csv_statement(AGREED_ROWS, None),
        balance_only(str(AGREED_BALANCE)),
        balance_only("0.00"),
    ):
        payload = reconcile_entries(entries, statement, options=options).to_dict()
        offenders = [
            (path, text)
            for path, text in _string_values(payload)
            if EXPONENT_FORM.match(text)
        ]
        assert offenders == [], f"{statement.source}: {offenders}"


def test_cents_survive_a_statement_amount_that_a_float_would_round():
    """0.1 + 0.2 territory. If any of this went through `float`, the delta
    would come back as a long tail of nines instead of zero."""
    entries, options = _load(
        txn("2026-01-05", "A", "-0.10") + txn("2026-01-06", "B", "-0.20")
    )
    statement = csv_statement(
        "2026-01-05,A,-0.10\n2026-01-06,B,-0.20\n", "999.70"
    )
    result = reconcile_entries(entries, statement, options=options)

    assert result.ledger_balance == Decimal("999.70")
    assert result.delta == Decimal("0.00")
    assert str(result.to_dict()["delta"]) == "0.00"


# =========================================================================
# Naming the account, and refusing to guess.
# =========================================================================


def test_a_leaf_name_resolves_to_the_full_account():
    """A user reconciling their checking account types "Checking"."""
    entries, _options = _load(AGREED_LEDGER)

    assert resolve_account(entries, "Checking") == ACCOUNT
    assert resolve_account(entries, "checking") == ACCOUNT
    assert resolve_account(entries, ACCOUNT) == ACCOUNT


def test_an_ambiguous_name_is_refused_and_lists_the_candidates():
    """Guessing which of two accounts a statement belongs to produces a report
    that is entirely internally consistent and about the wrong money."""
    entries, _options = _load(
        AGREED_LEDGER
        + '2026-01-06 * "Transfer"\n'
        "  Assets:Savings   -5.00 USD\n"
        "  Assets:Checking\n\n"
    )
    with pytest.raises(ReconcileError, match="matches more than one account"):
        resolve_account(entries, "Assets")


def test_an_unknown_account_lists_what_the_ledger_actually_has():
    entries, _options = _load(AGREED_LEDGER)

    with pytest.raises(ReconcileError) as exc:
        resolve_account(entries, "Brokerage")
    assert "no account matches 'Brokerage'" in str(exc.value)
    assert ACCOUNT in str(exc.value)


def test_a_liability_account_is_reconcilable():
    """A credit card statement is the statement most people actually hold in
    their hands, and it has the worst date-boundary behaviour of any account
    type — excluding it would make the tool unusable where it is most
    needed."""
    entries, options = _load(
        "2025-01-01 open Liabilities:CreditCard USD\n"
        '2026-01-08 * "TRADER JOES"\n'
        "  Liabilities:CreditCard   -80.00 USD\n"
        "  Expenses:Food\n\n"
    )
    statement = parse_statement_csv(
        "Date,Description,Amount\n2026-01-08,TRADER JOES,-80.00\n",
        account="CreditCard",
        closing_date=date(2026, 1, 31),
        closing_balance=Decimal("-80.00"),
    )
    result = reconcile_entries(entries, statement, options=options)

    assert result.account == "Liabilities:CreditCard"
    assert result.ok is True


def test_a_transaction_touching_the_account_twice_yields_two_comparable_entries():
    """A statement line is a single movement of money. Comparing it against a
    transaction that touches the account twice would compare an amount
    against a sum, and the two postings would be invisible individually."""
    entries, _options = _load(
        '2026-01-05 * "Split"\n'
        "  Assets:Checking   -80.00 USD\n"
        "  Assets:Checking   -20.00 USD\n"
        "  Expenses:Food\n\n"
    )
    flattened = flatten_account_postings(entries, ACCOUNT)

    assert [e.amount for e in flattened] == [
        OPENING,
        Decimal("-80.00"),
        Decimal("-20.00"),
    ]


def test_a_statement_currency_that_contradicts_the_ledger_is_refused():
    """Netting two currencies into one comparison produces a number with no
    meaning."""
    entries, options = _load(AGREED_LEDGER)
    statement = Statement(
        account=ACCOUNT,
        closing_date=date(2026, 1, 31),
        closing_balance=Decimal("890.00"),
        currency="EUR",
    )
    with pytest.raises(ReconcileError, match="reconcile one currency at a time"):
        reconcile_entries(entries, statement, options=options)


def test_an_account_holding_two_currencies_is_refused():
    # The EUR leg goes to `Equity:Opening-Balances`, which the preamble opens
    # without a currency constraint. Posting it to `Expenses:Food` — opened
    # `USD` — is a bean-check violation, so the fixture would fail to load and
    # this would assert against an empty ledger rather than a two-currency one.
    entries, options = _load(
        "2025-01-01 open Assets:Multi\n"
        '2026-01-05 * "A"\n'
        "  Assets:Multi   -80.00 USD\n"
        "  Expenses:Food\n\n"
        '2026-01-06 * "B"\n'
        "  Assets:Multi   -70.00 EUR\n"
        "  Equity:Opening-Balances   70.00 EUR\n\n"
    )
    statement = Statement(account="Assets:Multi", closing_date=date(2026, 1, 31))
    with pytest.raises(ReconcileError, match="more than one currency"):
        reconcile_entries(entries, statement, options=options)


# =========================================================================
# `run_reconcile`: a caller's mistake is a result, never a traceback.
#
# `cli.py` prints `.render()` and returns `.ok`, so an exception escaping here
# is a stack trace where a sentence belongs.
# =========================================================================


def test_a_balance_without_a_date_is_refused_with_a_reason(ledger_root):
    ledger_root(AGREED_LEDGER)
    result = run_reconcile(ACCOUNT, balance="890.00")

    assert result.ok is False
    assert "needs the date it was the balance on" in result.errors[0]
    assert "reconcile failed:" in result.render()


def test_reconciling_against_nothing_at_all_is_refused(ledger_root):
    ledger_root(AGREED_LEDGER)
    result = run_reconcile(ACCOUNT)

    assert result.ok is False
    assert "nothing to reconcile against" in result.errors[0]


def test_an_unreadable_statement_file_is_a_sentence_not_a_traceback(ledger_root, tmp_path):
    ledger_root(AGREED_LEDGER)
    result = run_reconcile(
        ACCOUNT, balance="890.00", on="2026-01-31", statement=str(tmp_path / "nope.csv")
    )

    assert result.ok is False
    assert "cannot read" in result.errors[0]


def test_an_unknown_account_comes_back_as_a_result(ledger_root):
    ledger_root(AGREED_LEDGER)
    result = run_reconcile("Brokerage", balance="890.00", on="2026-01-31")

    assert result.ok is False
    assert "no account matches" in result.errors[0]


def test_an_unparseable_balance_is_refused_by_name(ledger_root):
    ledger_root(AGREED_LEDGER)
    result = run_reconcile(ACCOUNT, balance="about nine hundred", on="2026-01-31")

    assert result.ok is False
    assert "as an amount" in result.errors[0]


def test_a_balance_only_run_reaches_the_candidate_path(ledger_root):
    """The call every user can make: an account, a number, and the date it was
    the balance on."""
    ledger_root(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    result = run_reconcile(ACCOUNT, balance="890.00", on="2026-01-31")

    assert result.errors == []
    assert result.ok is False
    assert result.source == "balance only"
    assert only(result, KIND_AMOUNT_MATCH).ledger_entries[0].description == "COSTCO"


def test_a_statement_with_no_closing_balance_diffs_lines_and_says_what_it_did_not_check(
    ledger_root, tmp_path
):
    """Proving the transactions match is not proving the money does, and a
    report that let someone believe otherwise would be worse than no report."""
    ledger_root(AGREED_LEDGER)
    export = tmp_path / "export.csv"
    export.write_text("Date,Description,Amount\n" + AGREED_ROWS, encoding="utf-8")

    result = run_reconcile(ACCOUNT, statement=str(export))

    assert result.delta is None
    assert result.matched == 2
    assert result.source == "export.csv"
    assert any("not that the account totals do" in note for note in result.notes)
    assert "the balances were not compared" in result.render()


def test_a_ledger_that_does_not_load_cleanly_is_a_note_not_a_refusal(ledger_root):
    """The likeliest reason a ledger fails to load is a *failing balance
    assertion* — which is the exact moment a user reaches for this command.
    Refusing then would withhold the tool precisely when it is wanted."""
    ledger_root(AGREED_LEDGER + "2026-02-01 balance Assets:Checking 1.00 USD\n")
    result = run_reconcile(ACCOUNT, balance="890.00", on="2026-01-31")

    assert result.errors == []
    assert result.ledger_balance == AGREED_BALANCE
    assert any("run `bookkeeper verify`" in note for note in result.notes)


# =========================================================================
# The rendered report, which is the only part most users will ever read.
# =========================================================================


def test_the_direction_of_the_difference_is_spelled_out_in_words():
    """"-47.13" is a direction nobody reads correctly at eleven at night, and
    reading it backwards sends someone hunting the opposite of the problem."""
    entries, options = _load(AGREED_LEDGER)

    less = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE - GAP)), options=options
    ).render()
    more = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE + GAP)), options=options
    ).render()

    assert "you have LESS than the ledger does" in less
    assert "you have MORE than the ledger does" in more


def test_findings_that_move_money_are_printed_apart_from_ones_that_do_not():
    """A `0.00` row inside a column of amounts that are supposed to add up to
    the difference makes the reader work out which rows are part of the sum —
    in the section whose whole job is to be readable at a glance."""
    # The assertion states the ledger's *own* balance (1000 - 80 - 30 - 47.13),
    # so bean-check passes and the fixture loads. What it disagrees with is the
    # statement, which does not know about the COSTCO charge at all — that is
    # precisely the FEED-DISAGREES case, and an assertion that contradicted the
    # ledger would be an ordinary bean-check failure instead.
    entries, options = _load(
        AGREED_LEDGER
        + txn("2026-01-20", "COSTCO", "-47.13")
        + "2026-02-01 balance Assets:Checking 842.87 USD\n"
    )
    statement = csv_statement(AGREED_ROWS, str(AGREED_BALANCE))
    text = reconcile_entries(entries, statement, options=options).render()

    money_at = text.index("What accounts for the difference")
    context_at = text.index("Also worth knowing (these move no money)")
    assert money_at < context_at
    assert text.index("EXTRA") < context_at
    assert text.index("FEED-DISAGREES") > context_at


def test_a_finding_points_at_a_file_and_a_line_the_user_can_open(ledger_root):
    """"A duplicate of 47.13" is a puzzle; a path and a line number is a fix."""
    ledger_root(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    text = run_reconcile(ACCOUNT, balance="890.00", on="2026-01-31").render()

    assert re.search(r"ledger/main\.beancount:\d+", text), text


def test_the_location_is_shortened_to_what_to_open_not_where_it_lives(ledger_root):
    """The absolute path is forty characters of the user's home directory
    before the part that identifies the entry, read in a terminal beside three
    others while someone looks for one of them."""
    root = ledger_root(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    result = run_reconcile(ACCOUNT, balance="890.00", on="2026-01-31")

    location = only(result, KIND_AMOUNT_MATCH).ledger_entries[0].location
    assert location.startswith("ledger/main.beancount:")
    assert str(root) not in location


def test_an_unexplained_remainder_is_stated_only_when_something_was_explained():
    """On a run that explained nothing, "the rest is unexplained" is the whole
    report restated, and a report that repeats itself is one people skim."""
    entries, options = _load(AGREED_LEDGER)

    explained_nothing = reconcile_entries(
        entries, balance_only(str(AGREED_BALANCE - GAP)), options=options
    )
    assert explained_nothing.explained == 0
    assert not any("not accounted for by anything" in n for n in explained_nothing.notes)

    # The bank knows nothing of the COSTCO charge, so its closing balance is
    # the agreed one — plus 5.00 of discrepancy that starts before the period.
    # Adding GAP here as well would double-count the charge: the ledger is
    # already 47.13 lower than the bank because of it.
    entries, options = _load(AGREED_LEDGER + txn("2026-01-20", "COSTCO", "-47.13"))
    partly = reconcile_entries(
        entries,
        csv_statement(AGREED_ROWS, str(AGREED_BALANCE + Decimal("5.00"))),
        options=options,
    )
    assert partly.explained == GAP
    assert partly.residual == Decimal("5.00")
    assert any("not accounted for by anything" in n for n in partly.notes)


def test_a_statement_warning_survives_into_the_report():
    """A file whose dates were read month-first on a guess must say so in the
    report, not only at parse time — date-boundary misplacement is the failure
    this tool exists to diagnose, and every finding below rests on it."""
    entries, options = _load(AGREED_LEDGER)
    statement = parse_statement_csv(
        "Date,Description,Amount\n05/01/2026,TRADER JOES,-80.00\n",
        account=ACCOUNT,
        closing_date=date(2026, 1, 31),
        closing_balance=Decimal("890.00"),
    )
    result = reconcile_entries(entries, statement, options=options)

    assert any("ambiguous" in note for note in result.notes)
    assert "note: every date in this file is ambiguous" in result.render()
