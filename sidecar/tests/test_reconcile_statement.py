"""Reading a bank's CSV export.

The subject here is the parser's *refusals* as much as its successes. A
reconciler that misreads a statement does not fail — it produces a complete,
authoritative-looking report about transactions that were never wrong, and
sends someone hunting through their own books for an artifact of column
guessing. So every ambiguity a real export can carry is asserted to be either
resolved from evidence or refused by name.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from bookkeeper.reconcile.statement import (
    _DATE_ALIASES,
    Statement,
    StatementError,
    _normalize_header,
    _pick_column,
    load_statement_csv,
    parse_money,
    parse_statement_csv,
)


def parse(text: str, **kwargs):
    kwargs.setdefault("account", "Assets:SimpleFIN:Checking")
    return parse_statement_csv(text, **kwargs)


# --- amounts --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.34", "12.34"),
        ("-12.34", "-12.34"),
        ("12.34-", "-12.34"),
        ("(12.34)", "-12.34"),
        ("$1,234.56", "1234.56"),
        ("  1,234.56 USD ", "1234.56"),
        ("+45.00", "45.00"),
        ("0.00", "0.00"),
    ],
)
def test_money_notations_banks_actually_emit(raw, expected):
    assert parse_money(raw) == Decimal(expected)


def test_money_keeps_trailing_zeros_rather_than_normalising_them():
    """`Decimal("12.30")` and `Decimal("12.3")` compare equal but render
    differently, and this string is printed in a report about someone's
    money."""
    assert str(parse_money("12.30")) == "12.30"


def test_money_never_goes_through_float():
    # 0.1 + 0.2 territory: a float round-trip turns this into 8.4499999...
    assert parse_money("8.45") * 100 == Decimal("845.00")


def test_unreadable_amount_names_the_text_it_could_not_read():
    with pytest.raises(StatementError, match="cannot read 'abc' as an amount"):
        parse_money("abc")


# --- columns --------------------------------------------------------------


def test_signed_amount_column():
    statement = parse(
        "Date,Description,Amount\n2026-01-05,TRADER JOES,-80.00\n2026-01-06,PAYROLL,2500.00\n"
    )
    assert [line.amount for line in statement.lines] == [Decimal("-80.00"), Decimal("2500.00")]


def test_debit_credit_pair_is_converted_to_the_ledgers_sign_convention():
    """The column the bank chose *is* the direction, so this needs no guess —
    which is why a debit/credit export is the one shape that never trips the
    sign-inversion warning."""
    statement = parse(
        "Posting Date,Description,Debit,Credit\n"
        "2026-01-05,TRADER JOES,80.00,\n"
        "2026-01-06,PAYROLL,,2500.00\n"
    )
    assert [line.amount for line in statement.lines] == [Decimal("-80.00"), Decimal("2500.00")]


def test_a_debit_column_with_a_negative_number_still_means_money_out():
    """Some exports write the debit column negative *and* label it Debit.
    Taking the absolute value keeps that from double-negating into a deposit."""
    statement = parse("Date,Description,Debit\n2026-01-05,TRADER JOES,-80.00\n")
    assert statement.lines[0].amount == Decimal("-80.00")


def test_a_row_with_both_a_debit_and_a_credit_is_refused_not_netted():
    with pytest.raises(StatementError, match="row 2: both a debit"):
        parse("Date,Description,Debit,Credit\n2026-01-05,X,80.00,20.00\n")


def test_posting_date_wins_over_transaction_date():
    """A statement's balance moves on the date the bank *posted*, so when a
    file carries both, the posting date is the one that reconciles."""
    statement = parse(
        "Transaction Date,Posting Date,Description,Amount\n"
        "2026-01-03,2026-01-05,TRADER JOES,-80.00\n"
    )
    assert statement.lines[0].posted_date == date(2026, 1, 5)


def test_missing_date_column_is_refused_and_lists_what_it_found():
    with pytest.raises(StatementError) as exc:
        parse("When,Description,Amount\n2026-01-05,X,-1.00\n")
    assert "no date column" in str(exc.value)
    assert "'When'" in str(exc.value)


def test_missing_amount_column_is_refused_and_names_the_recognised_ones():
    with pytest.raises(StatementError) as exc:
        parse("Date,Description,Sum\n2026-01-05,X,-1.00\n")
    assert "no amount column" in str(exc.value)
    assert "debit" in str(exc.value)


def test_a_headerless_file_is_refused_rather_than_read_positionally():
    """Column order is not a contract any bank offers, and guessing at it is
    how a date column gets compared against amounts."""
    with pytest.raises(StatementError):
        parse("2026-01-05,TRADER JOES,-80.00\n2026-01-06,PAYROLL,2500.00\n")


def test_an_empty_file_and_a_header_only_file_are_told_apart():
    with pytest.raises(StatementError, match="empty"):
        parse("   \n")
    with pytest.raises(StatementError, match="no transactions"):
        parse("Date,Description,Amount\n")


def test_a_missing_description_column_is_a_warning_not_a_failure():
    statement = parse("Date,Amount\n2026-01-05,-80.00\n")
    assert statement.lines[0].description == "(no description)"
    assert any("no description column" in w for w in statement.warnings)


def test_semicolon_delimited_exports_are_read():
    statement = parse("Date;Description;Amount\n2026-01-05;TRADER JOES;-80.00\n")
    assert statement.lines[0].amount == Decimal("-80.00")


# --- dates ----------------------------------------------------------------


def test_iso_dates_are_always_accepted_and_raise_no_ambiguity_warning():
    statement = parse("Date,Description,Amount\n2026-01-05,X,-1.00\n")
    assert statement.lines[0].posted_date == date(2026, 1, 5)
    assert not any("ambiguous" in w for w in statement.warnings)


def test_one_unambiguous_row_settles_the_whole_file_as_day_first():
    """`13/04/2026` cannot be month-first, and that single row decides how
    `03/04/2026` in the same file is read."""
    statement = parse(
        "Date,Description,Amount\n03/04/2026,A,-1.00\n13/04/2026,B,-2.00\n"
    )
    assert [line.posted_date for line in statement.lines] == [
        date(2026, 4, 3),
        date(2026, 4, 13),
    ]


def test_one_unambiguous_row_settles_the_whole_file_as_month_first():
    statement = parse(
        "Date,Description,Amount\n03/04/2026,A,-1.00\n04/13/2026,B,-2.00\n"
    )
    assert [line.posted_date for line in statement.lines] == [
        date(2026, 3, 4),
        date(2026, 4, 13),
    ]


def test_a_file_with_both_orderings_is_refused_outright():
    """No single reading of it is correct, and picking one would silently
    transpose half the dates in a report whose whole subject is dates."""
    with pytest.raises(StatementError, match="both day-first and month-first"):
        parse("Date,Description,Amount\n13/04/2026,A,-1.00\n04/13/2026,B,-2.00\n")


def test_a_wholly_ambiguous_file_is_read_month_first_and_says_so():
    statement = parse("Date,Description,Amount\n03/04/2026,A,-1.00\n05/06/2026,B,-2.00\n")
    assert statement.lines[0].posted_date == date(2026, 3, 4)
    assert any("ambiguous" in w and "month-first" in w for w in statement.warnings)


def test_year_first_slashes_are_recognised():
    statement = parse("Date,Description,Amount\n2026/01/05,A,-1.00\n")
    assert statement.lines[0].posted_date == date(2026, 1, 5)


def test_an_unreadable_date_names_the_spreadsheet_row():
    with pytest.raises(StatementError, match="row 3"):
        parse("Date,Description,Amount\n2026-01-05,A,-1.00\n5 Jan 26,B,-2.00\n")


# --- the whole statement --------------------------------------------------


def test_rows_carry_the_row_number_a_spreadsheet_shows():
    """Row 1 is the header, so the first transaction is row 2. Every finding
    downstream points at this number."""
    statement = parse("Date,Description,Amount\n2026-01-05,A,-1.00\n2026-01-06,B,-2.00\n")
    assert [line.row for line in statement.lines] == [2, 3]


def test_covers_from_prefers_the_stated_period_over_the_first_line():
    """A period starting the 1st whose first transaction is the 4th covers
    those three quiet days; without that, a ledger entry on the 2nd would be
    reported as one the statement omitted."""
    statement = parse(
        "Date,Description,Amount\n2026-01-04,A,-1.00\n", from_date=date(2026, 1, 1)
    )
    assert statement.covers_from == date(2026, 1, 1)


def test_covers_from_falls_back_to_the_earliest_line():
    statement = parse("Date,Description,Amount\n2026-01-04,A,-1.00\n2026-01-02,B,-2.00\n")
    assert statement.covers_from == date(2026, 1, 2)


def test_a_balance_only_statement_is_a_complete_statement():
    """The case every user can produce, with no file at all."""
    statement = Statement(
        account="Assets:SimpleFIN:Checking",
        closing_date=date(2026, 1, 31),
        closing_balance=Decimal("4212.55"),
    )
    assert statement.lines == ()
    assert statement.total == Decimal(0)
    assert statement.covers_to == date(2026, 1, 31)


def test_a_bom_is_neutralised_by_the_header_normalizer_not_only_by_the_codec():
    """Bank exports routinely carry a BOM, and glued to the first header name
    it would fail with an error blaming the wrong thing.

    Asserted against `_normalize_header` directly, because the obvious test —
    write a `utf-8-sig` file and read it back — passes just as well when
    `load_statement_csv` is changed to plain `utf-8`. The BOM survives into
    the header name in that case and the lookup *still* works, because
    `_NON_ALNUM` treats U+FEFF as punctuation and collapses it away. Reading
    with `utf-8-sig` is a second line of defence, not the one doing the work,
    and a test that cannot tell them apart pins neither.
    """
    assert _normalize_header("﻿Date") == "date"
    assert _pick_column(["﻿Date", "Amount"], _DATE_ALIASES) == "﻿Date"


def test_a_bom_file_reads_end_to_end(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text("Date,Description,Amount\n2026-01-05,A,-1.00\n", encoding="utf-8-sig")
    statement = load_statement_csv(path, account="Assets:SimpleFIN:Checking")
    assert statement.lines[0].amount == Decimal("-1.00")
    assert statement.source == "export.csv"


def test_an_unreadable_file_is_an_error_not_a_traceback(tmp_path):
    with pytest.raises(StatementError, match="cannot read"):
        load_statement_csv(tmp_path / "nope.csv", account="Assets:SimpleFIN:Checking")
