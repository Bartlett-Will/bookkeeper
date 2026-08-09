"""What a bank statement is, and how to read one off a CSV export.

**Why CSV, and why the balance alone is the primary case.**

A user reconciling at their kitchen table has one of three things: a PDF, a
CSV export from their bank's website, or a number they are reading off a
piece of paper. Only two of those can be fed to a program without adding a
dependency, and only one of them can be fed to a program *correctly*:

- **A number, with no file at all.** This is the case that must always work,
  because it is the only one every user has. `Statement(closing_balance=...,
  closing_date=...)` with no lines is a complete, reconcilable statement, and
  `compare.py` still narrows a discrepancy to candidate transactions from the
  ledger alone. Everything else here is an enrichment of this case, never a
  precondition for it.
- **CSV**, parsed below. Every retail bank exports it, it is the format users
  already download, and `csv` is in the standard library. The column names
  vary wildly, so this parser matches on a table of aliases and *fails loudly*
  when it cannot find a column rather than guessing at position.

Rejected, each for a reason:

- **PDF.** Requires a dependency, which Phase 6 forbids, but the real
  objection survives the rule: PDF statement layouts are per-bank and
  position-based, so extraction fails silently and partially. A statement
  parsed 90% correctly is worse than no statement at all -- it produces a
  confident discrepancy that is an artifact of the parser, and the user
  spends their evening hunting a transaction that was never wrong.
- **OFX/QFX.** Parseable with the stdlib, but every bank that offers OFX also
  offers CSV, so it buys no coverage; and a half-correct SGML parser fails the
  same silent way a PDF does.
- **Pasted free text.** Would need heuristics or a model to segment. This
  whole module exists to be a *deterministic* check on a model-assisted
  pipeline; making the check itself probabilistic defeats it.
- **A second SimpleFIN fetch.** Tempting and worthless: it checks the feed
  against itself. Reconciliation means an independent source, or it means
  nothing.

**Signs.** The ledger convention is signed: money out is negative. Banks are
inconsistent -- some export a signed `Amount`, some a positive `Debit` /
`Credit` pair. The pair is unambiguous and is converted here. A single signed
column is taken exactly as written; this module never flips it on a hunch,
because silently negating a user's money is the worst available failure.
`compare.py` detects a whole-file sign inversion and says so instead.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

#: Used only when neither the ledger nor the caller names a currency.
DEFAULT_CURRENCY = "USD"


class StatementError(ValueError):
    """A statement that cannot be read. Always names the row, when there is one.

    A `ValueError` subclass so the CLI and API can catch it the same way they
    already catch an unparseable date -- a caller who handed us a file we
    cannot read has no reconciliation to be given, and gets a message rather
    than a traceback.
    """


@dataclass(frozen=True)
class StatementLine:
    """One transaction as the *bank* recorded it.

    `row` is the 1-based row number in the source file, header included, so
    that every finding downstream can point at a line the user can actually
    open and look at. A discrepancy report that says "some transaction" is the
    thing this whole package exists not to produce.
    """

    posted_date: date
    #: Ledger sign convention: negative is money leaving the account.
    amount: Decimal
    description: str
    row: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "posted_date": self.posted_date.isoformat(),
            "amount": str(self.amount),
            "description": self.description,
            "row": self.row,
        }


@dataclass(frozen=True)
class Statement:
    """A bank statement, in whatever completeness the user actually has.

    Every field except `account` is optional, and the combinations are all
    meaningful rather than degenerate:

    - `closing_balance` + `closing_date` alone -- "on this date my balance was
      X". The minimum viable statement, and the one every user can produce.
    - `lines` alone -- a CSV export with no closing figure. The transaction
      set can still be diffed, which finds duplicates and omissions; the
      balance simply is not asserted, and the result says so rather than
      inventing one.
    - Both -- the full check, and the only form where a discrepancy in the
      balance can be *decomposed* into named transactions rather than
      guessed at.

    `from_date` is the first day the statement's lines are claimed to cover,
    which is not the same as the first day a line appears on. A statement
    period starting the 1st whose first transaction is the 4th covers those
    three quiet days; without `from_date`, a ledger transaction on the 2nd
    would be reported as an entry the statement omitted, when in truth the
    statement covers it and it simply did not happen at the bank.
    """

    account: str
    closing_date: date | None = None
    closing_balance: Decimal | None = None
    #: Empty means "unspecified": take the account's own currency from the
    #: ledger. Defaulting to "USD" here would look like a claim, and a claim
    #: that happened to be wrong gets checked against the ledger and refused —
    #: declining to reconcile a Canadian account because nobody typed a
    #: currency.
    currency: str = ""
    lines: tuple[StatementLine, ...] = ()
    from_date: date | None = None
    #: The bank's own stated transaction count for the period, if the user has
    #: one. A count is often printed on a statement even when no machine-
    #: readable export exists, and it catches the exact failure a balance
    #: cannot: two errors of equal and opposite size.
    count: int | None = None
    #: Where this came from, for the render. A user re-reading a report needs
    #: to know which file produced it.
    source: str = "inline"
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def covers_from(self) -> date | None:
        """First day the lines speak for: `from_date`, else the earliest line."""
        if self.from_date is not None:
            return self.from_date
        return min((line.posted_date for line in self.lines), default=None)

    @property
    def covers_to(self) -> date | None:
        """Last day the lines speak for: `closing_date`, else the latest line."""
        if self.closing_date is not None:
            return self.closing_date
        return max((line.posted_date for line in self.lines), default=None)

    @property
    def total(self) -> Decimal:
        """Net of every line. Zero for a balance-only statement, which is
        correct: it makes no claim about transactions at all."""
        return sum((line.amount for line in self.lines), Decimal(0))


# -- CSV reading -----------------------------------------------------------

#: Header aliases, normalized (lowercased, punctuation collapsed to spaces).
#: Ordered by how specific they are: `posting date` must win over `date` on a
#: file that has both, because a file with both usually also has a
#: `transaction date` that the bank does *not* post under.
_DATE_ALIASES = (
    "posting date",
    "posted date",
    "post date",
    "date posted",
    "transaction date",
    "effective date",
    "posted",
    "date",
)
_AMOUNT_ALIASES = ("transaction amount", "amount", "value")
_DEBIT_ALIASES = ("debit", "withdrawal", "withdrawals", "money out", "paid out", "charge")
_CREDIT_ALIASES = ("credit", "deposit", "deposits", "money in", "paid in")
_DESCRIPTION_ALIASES = (
    "description",
    "transaction description",
    "narration",
    "details",
    "particulars",
    "payee",
    "merchant",
    "name",
    "memo",
    "reference",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
#: Everything a bank puts around a number that is not part of it.
_MONEY_NOISE = re.compile(r"[,\s $£€¥]|(?<=\d)\s*(?:USD|EUR|GBP|CAD|AUD)\b", re.IGNORECASE)


def _normalize_header(name: str) -> str:
    return _NON_ALNUM.sub(" ", (name or "").strip().lower()).strip()


def _pick_column(headers: list[str], aliases: tuple[str, ...]) -> str | None:
    """First header matching an alias, aliases tried most-specific first."""
    normalized = {_normalize_header(h): h for h in headers}
    for alias in aliases:
        if alias in normalized:
            return normalized[alias]
    return None


def parse_money(text: str) -> Decimal:
    """A bank's rendering of an amount, as an exact `Decimal`.

    Handles the three notations that actually appear in exports: a leading or
    trailing minus, accounting parentheses (`(45.10)` is negative), and
    thousands separators or a currency symbol wrapped around either. Never
    goes through `float` -- a statement's cents are the entire point.
    """
    raw = (text or "").strip()
    if not raw:
        raise StatementError("empty amount")
    negative = False
    if raw.startswith("(") and raw.endswith(")"):
        negative = True
        raw = raw[1:-1]
    if raw.endswith("-"):
        negative = True
        raw = raw[:-1]
    cleaned = _MONEY_NOISE.sub("", raw).removeprefix("+")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as exc:
        raise StatementError(f"cannot read {text.strip()!r} as an amount") from exc
    return -value if negative else value


#: Slash/dot forms, tried after ISO. The ordering ambiguity between the first
#: two is resolved across the whole file (see `_infer_date_order`), never
#: per-row -- a file read half as US and half as European would be silently,
#: unfixably wrong.
_MONTH_FIRST = "%m/%d/%Y"
_DAY_FIRST = "%d/%m/%Y"
_YEAR_FIRST = "%Y/%m/%d"


def _slash_parts(raw: str) -> list[str] | None:
    """`raw` as three numeric components, or `None` if it is not that shape."""
    parts = re.split(r"[/.\-]", raw.strip())
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return parts


def _infer_date_order(values: list[str]) -> tuple[str, str | None]:
    """`(strftime format, warning)` for a whole file's date column.

    Decided once, over every row, because the evidence is file-wide: a single
    row reading `03/04/2026` is undecidable, but one row somewhere reading
    `13/04/2026` settles the entire file. Only when *no* row disambiguates is
    a default applied, and then it is stated out loud -- a reconciliation
    whose dates might be transposed must say so, since date-boundary
    misplacement is the failure this tool exists to diagnose.
    """
    day_first = month_first = False
    year_first = False
    for value in values:
        parts = _slash_parts(value)
        if parts is None:
            continue
        if len(parts[0]) == 4:
            year_first = True
            continue
        first, second = int(parts[0]), int(parts[1])
        if first > 12:
            day_first = True
        if second > 12:
            month_first = True
    if year_first and not (day_first or month_first):
        return _YEAR_FIRST, None
    if day_first and month_first:
        raise StatementError(
            "the date column contains both day-first and month-first dates "
            "(some rows have a first component over 12, others a second one) -- "
            "no single reading of this file is correct; re-export it with ISO "
            "(YYYY-MM-DD) dates"
        )
    if day_first:
        return _DAY_FIRST, None
    if month_first:
        return _MONTH_FIRST, None
    return _MONTH_FIRST, (
        "every date in this file is ambiguous (no day above 12), so it was read "
        "month-first (US style, e.g. 03/04/2026 = 4 March). If your bank writes "
        "dates day-first, re-export with ISO (YYYY-MM-DD) dates before trusting "
        "any date-related finding below."
    )


def _parse_date(raw: str, slash_format: str, row: int) -> date:
    value = (raw or "").strip()
    if not value:
        raise StatementError(f"row {row}: empty date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    parts = _slash_parts(value)
    if parts is not None:
        try:
            return datetime.strptime("/".join(parts), slash_format).date()  # noqa: DTZ007
        except ValueError as exc:
            raise StatementError(f"row {row}: cannot read {value!r} as a date") from exc
    raise StatementError(
        f"row {row}: cannot read {value!r} as a date -- ISO (YYYY-MM-DD) is "
        "always accepted"
    )


def _row_amount(
    row: dict[str, str], amount_col: str | None, debit_col: str | None, credit_col: str | None,
    number: int,
) -> Decimal:
    """This row's amount in ledger sign convention.

    A debit/credit pair is unambiguous -- the column the bank chose *is* the
    direction -- so it is converted here without a guess. A single signed
    column is taken as written.
    """
    def read(raw: str) -> Decimal:
        try:
            return parse_money(raw)
        except StatementError as exc:
            raise StatementError(f"row {number}: {exc}") from exc

    if amount_col is not None:
        raw = (row.get(amount_col) or "").strip()
        if raw:
            return read(raw)
    debit = (row.get(debit_col) or "").strip() if debit_col else ""
    credit = (row.get(credit_col) or "").strip() if credit_col else ""
    if debit and credit:
        # Both filled is not a transaction, it is a misread file. Netting them
        # would silently produce a plausible-looking wrong number.
        raise StatementError(
            f"row {number}: both a debit ({debit}) and a credit ({credit}) -- "
            "this file's columns do not mean what they appear to"
        )
    if debit:
        return -abs(read(debit))
    if credit:
        return abs(read(credit))
    raise StatementError(f"row {number}: no amount in any recognised column")


def parse_statement_csv(
    text: str,
    *,
    account: str,
    closing_date: date | None = None,
    closing_balance: Decimal | None = None,
    currency: str = "",
    from_date: date | None = None,
    source: str = "csv",
) -> Statement:
    """A bank CSV export as a `Statement`.

    A header row is **required**, and its absence is an error rather than a
    fallback to column order. Positional guessing is exactly how a
    reconciliation tool ends up confidently comparing dates against amounts;
    the failure is silent and the output still looks like a report.
    """
    if not text.strip():
        raise StatementError("the statement file is empty")
    # `Sniffer` rather than a fixed comma: European exports are semicolon-
    # delimited and tab exports are common enough to be worth not rejecting.
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    if not headers:
        raise StatementError("the statement file has no header row")

    date_col = _pick_column(headers, _DATE_ALIASES)
    amount_col = _pick_column(headers, _AMOUNT_ALIASES)
    debit_col = _pick_column(headers, _DEBIT_ALIASES)
    credit_col = _pick_column(headers, _CREDIT_ALIASES)
    description_col = _pick_column(headers, _DESCRIPTION_ALIASES)

    if date_col is None:
        raise StatementError(
            "no date column found. Columns are: "
            + ", ".join(repr(h) for h in headers)
            + ". A recognised name is one of: "
            + ", ".join(_DATE_ALIASES)
        )
    if amount_col is None and debit_col is None and credit_col is None:
        raise StatementError(
            "no amount column found. Columns are: "
            + ", ".join(repr(h) for h in headers)
            + ". Either an amount column ("
            + ", ".join(_AMOUNT_ALIASES)
            + ") or a debit/credit pair ("
            + ", ".join(_DEBIT_ALIASES[:3])
            + " / "
            + ", ".join(_CREDIT_ALIASES[:3])
            + ")"
        )

    rows = list(reader)
    if not rows:
        raise StatementError("the statement file has a header row but no transactions")
    slash_format, ambiguity = _infer_date_order([(r.get(date_col) or "") for r in rows])

    lines: list[StatementLine] = []
    for offset, row in enumerate(rows):
        # +2: row 1 is the header, and `enumerate` starts at 0. The number
        # printed here has to be the one the user's spreadsheet shows.
        number = offset + 2
        posted = _parse_date(row.get(date_col) or "", slash_format, number)
        amount = _row_amount(row, amount_col, debit_col, credit_col, number)
        description = (row.get(description_col) or "").strip() if description_col else ""
        lines.append(
            StatementLine(
                posted_date=posted,
                amount=amount,
                description=description or "(no description)",
                row=number,
            )
        )

    warnings: list[str] = []
    if ambiguity:
        warnings.append(ambiguity)
    if description_col is None:
        # Not fatal -- amounts and dates alone still reconcile -- but it
        # changes what the findings can say, so it is stated rather than
        # discovered from a report full of "(no description)".
        warnings.append(
            "no description column found, so findings can only identify a "
            "transaction by its date and amount"
        )

    return Statement(
        account=account,
        closing_date=closing_date,
        closing_balance=closing_balance,
        currency=currency,
        lines=tuple(lines),
        from_date=from_date,
        source=source,
        warnings=tuple(warnings),
    )


def load_statement_csv(path: str | Path, **kwargs) -> Statement:
    """`parse_statement_csv` over a file, defaulting `source` to its name."""
    file_path = Path(path)
    try:
        # `utf-8-sig`: bank exports routinely carry a BOM, which would
        # otherwise be glued to the first header name and defeat the alias
        # lookup with an error blaming the wrong thing.
        text = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise StatementError(f"cannot read {file_path}: {exc}") from exc
    kwargs.setdefault("source", file_path.name)
    return parse_statement_csv(text, **kwargs)
