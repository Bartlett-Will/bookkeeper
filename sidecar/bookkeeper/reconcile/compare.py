"""Comparing the ledger against a statement, and saying *where* they differ.

PLAN.md §5.2 item 3 is the reason this exists: cash reconciliation is the
guard that survives the envelope model, because "if we ever drop or duplicate
a transaction, `bean-check` fails at the next assertion date". That guard is
armed by `ingest`, from figures SimpleFIN reports about itself. This module
arms it from an *independent* source -- the statement the bank sent -- which
is the only thing that can catch a feed that is itself wrong or incomplete.

**"They disagree by $47.13" is not an answer.** The output of this module is
a set of *named transactions*, each with the share of the difference it
accounts for, each pointing at a file and line (in the ledger) or a row (in
the statement) the user can open. A scalar tells someone there is a problem
and gives them nothing to do about it.

Three failures dominate real reconciliation, and they are kept distinct
because the remedy for each is different:

1. **Date-boundary mismatch.** The single most common one, and structurally
   likely here: `NormalizedBalance.assertion_date` is already `balance-date +
   1 day`, banks post at odd hours, and a transaction the bank dated the 31st
   can reach the ledger dated the 1st. It must never be reported as a missing
   transaction -- the money is present and correct, only the date differs, and
   sending someone to hunt for a transaction that is right there is the worst
   outcome short of being silently wrong. Any transaction near the cutoff
   whose amount matches the gap is checked for this *before* anything is
   called missing.
2. **A missing transaction** -- on the statement, never imported.
3. **A duplicate** -- imported twice.

A duplicate and a missing transaction produce a same-signed gap in opposite
directions: a missing debit and a duplicated credit both leave the ledger
reading high. The sign therefore cannot tell them apart, and this module never
tries. It distinguishes them by *evidence*: a duplicate is two ledger entries
that look like one transaction (identical amount, near-identical description,
days apart -- or, decisively, the same `simplefin-id` on the same account,
which is a violation of the dedup key and is reported whether or not it
explains anything). A missing transaction is a statement line with no ledger
counterpart. Where there is no statement to diff against, both remain
*hypotheses* and are labelled as such.

That labelling is the other load-bearing distinction here. A finding is
`confirmed` when it was observed -- a line with no counterpart, two entries
sharing a dedup key. It is unconfirmed when it was inferred from the size of
the gap alone. Only confirmed findings are summed into `explained`, so
`residual = delta - explained` means something exact: the part of the
difference that the evidence does not account for. Hypotheses are alternatives
to each other and summing them would be nonsense.

Every amount is `Decimal` throughout and leaves as a string (§ standing
constraint). Nothing here divides, so no quantization is needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

from beancount.core.data import Balance, Transaction

from bookkeeper.envelope.compute import load_ledger
from bookkeeper.reconcile.statement import (
    DEFAULT_CURRENCY,
    Statement,
    StatementError,
    StatementLine,
    load_statement_csv,
    parse_money,
)

#: Accounts a bank statement can be about. Liabilities are included on
#: purpose: a credit card statement is the statement most people actually
#: hold in their hands, and excluding it would make this tool unusable for
#: the account type with the worst date-boundary behaviour.
RECONCILABLE_PREFIXES = ("Assets:", "Liabilities:")

#: How many days apart two records of the same transaction may be dated and
#: still be treated as the same transaction. Three, not one: a weekend swallows
#: two days at most banks, so a Friday purchase posting on Monday is a
#: three-day spread and is entirely ordinary. Widening it further starts
#: matching genuinely different transactions of the same amount -- a coffee
#: bought twice in a week -- which would hide a real duplicate.
MATCH_WINDOW_DAYS = 3

#: How alike two descriptions must read before two ledger entries of the same
#: amount are called a duplicate rather than a coincidence. High, because the
#: cost of a false "you imported this twice" is someone deleting a real
#: transaction from their books.
DUPLICATE_SIMILARITY = 0.85

#: Longest list of one kind of candidate the render will print. A user hunting
#: a discrepancy at their kitchen table needs the plausible few; forty
#: same-amount coffees is a dump, and the count of what was withheld is more
#: use than the rows themselves.
MAX_CANDIDATES = 6

# Finding kinds. Strings rather than an enum to match the rest of the codebase
# (`monthend.COVERAGE_*`, `trends` directions), which the API serializes
# directly.
KIND_MISSING = "missing"
KIND_EXTRA = "extra"
KIND_DUPLICATE = "duplicate"
KIND_DATE_BOUNDARY = "date-boundary"
KIND_DATE_SHIFT = "date-shift"
KIND_AMOUNT_MATCH = "amount-match"
KIND_SIGN_CONVENTION = "sign-convention"
KIND_FEED_DISAGREES = "feed-disagrees"
KIND_COUNT = "count"

#: Kinds that mean the books and the bank actually disagree about what
#: happened, as opposed to disagreeing about a date or offering a hypothesis.
BREAKING_KINDS = frozenset({KIND_MISSING, KIND_EXTRA, KIND_DUPLICATE, KIND_COUNT})


class ReconcileError(ValueError):
    """A request that cannot be reconciled at all -- unknown or ambiguous
    account, mixed currencies. Distinct from *finding a discrepancy*, which is
    a successful reconciliation with a non-zero answer."""


@dataclass(frozen=True)
class LedgerEntry:
    """One posting to the reconciled account, flattened for comparison.

    Postings rather than transactions, because a statement line is a single
    movement of money and matching it against a transaction that touches the
    account twice would compare an amount against a sum.

    `filename`/`lineno` come from beancount's own metadata and exist for one
    reason: every finding must point somewhere a user can open. "A duplicate
    of 47.13" is a puzzle; "ledger/transactions/2026-07.beancount:184" is a
    fix.
    """

    date: date
    amount: Decimal
    description: str
    simplefin_id: str | None = None
    filename: str | None = None
    lineno: int | None = None

    @property
    def location(self) -> str:
        """`transactions/2026-07.beancount:184` — what to open, not where it lives.

        Shortened to the last two path components deliberately. The absolute
        path is forty characters of the user's home directory before the part
        that identifies the entry, and this string is read in a terminal
        beside three others while someone looks for one of them.
        """
        if not self.filename:
            return "(unknown)"
        parts = self.filename.replace("\\", "/").split("/")
        short = "/".join(parts[-2:]) if len(parts) > 1 else self.filename
        return f"{short}:{self.lineno}" if self.lineno else short

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "amount": str(self.amount),
            "description": self.description,
            "simplefin_id": self.simplefin_id,
            "location": self.location,
        }


@dataclass(frozen=True)
class Finding:
    """One thing that is, or might be, wrong.

    `delta` is this finding's share of `statement - ledger`: correct it, and
    the ledger's closing balance moves by exactly this much toward the
    statement. Findings that cannot move the closing balance carry zero --
    a date shift entirely on one side of the cutoff, a disagreeing feed
    assertion -- and are still reported, because they are still things the
    user needs to know.

    `confirmed` separates what was *observed* from what was *inferred*. Only
    confirmed findings are summed into `ReconcileResult.explained`; see the
    module docstring.
    """

    kind: str
    delta: Decimal
    explanation: str
    confirmed: bool = False
    ledger_entries: tuple[LedgerEntry, ...] = ()
    statement_lines: tuple[StatementLine, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "delta": str(self.delta),
            "explanation": self.explanation,
            "confirmed": self.confirmed,
            "ledger_entries": [e.to_dict() for e in self.ledger_entries],
            "statement_lines": [line.to_dict() for line in self.statement_lines],
        }


@dataclass
class ReconcileResult:
    """The answer, and everything needed to check it.

    `.ok` / `.render()` per `cli.py`'s dispatch contract. `.ok` is
    "the ledger agrees with the bank", so it is false for a non-zero
    difference, for any confirmed missing/extra/duplicate even when those
    happen to cancel out to zero, and for a transaction count that disagrees.
    A date shift that moves no money is not a disagreement about money and
    does not fail it -- it is reported and the exit code stays zero.
    """

    ok: bool
    account: str
    currency: str = DEFAULT_CURRENCY
    source: str = "inline"
    #: The statement's closing date, and the beancount `balance` directive
    #: date that asserts the same instant. These differ by one day and that is
    #: not a detail: a directive dated D asserts the balance at the *start* of
    #: D, so "my balance at the end of the 31st" is a directive dated the 1st
    #: (`normalize.NormalizedBalance`). Both are carried so a user comparing
    #: this report against `balances.beancount` is not left to rediscover it.
    closing_date: date | None = None
    assertion_date: date | None = None
    statement_balance: Decimal | None = None
    ledger_balance: Decimal | None = None
    #: `statement - ledger`. Positive: the bank says there is more money than
    #: the ledger does. `None` when no closing balance was supplied, which is
    #: a legitimate statement (a CSV with no closing figure still diffs).
    delta: Decimal | None = None
    #: Σ of the confirmed findings' deltas, and what is left over. A non-zero
    #: `residual` means the evidence in this period does not account for the
    #: whole difference -- so it starts before the period, or the closing
    #: balance itself is wrong.
    explained: Decimal = Decimal(0)
    residual: Decimal = Decimal(0)
    from_date: date | None = None
    to_date: date | None = None
    statement_lines: int = 0
    ledger_entries: int = 0
    matched: int = 0
    findings: tuple[Finding, ...] = ()
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def reconciled(self) -> bool:
        """The balances agree exactly. Weaker than `ok`: a ledger can land on
        the right total via a missing transaction and a duplicate of the same
        size, which `ok` catches and this does not."""
        return self.delta is not None and self.delta == 0

    @property
    def confirmed_findings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.confirmed)

    @property
    def candidates(self) -> tuple[Finding, ...]:
        """Hypotheses -- alternatives to one another, not components of a sum."""
        return tuple(f for f in self.findings if not f.confirmed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "account": self.account,
            "currency": self.currency,
            "source": self.source,
            "closing_date": None if self.closing_date is None else self.closing_date.isoformat(),
            "assertion_date": (
                None if self.assertion_date is None else self.assertion_date.isoformat()
            ),
            "statement_balance": (
                None if self.statement_balance is None else str(self.statement_balance)
            ),
            "ledger_balance": None if self.ledger_balance is None else str(self.ledger_balance),
            "delta": None if self.delta is None else str(self.delta),
            "explained": str(self.explained),
            "residual": str(self.residual),
            "reconciled": self.reconciled,
            "from_date": None if self.from_date is None else self.from_date.isoformat(),
            "to_date": None if self.to_date is None else self.to_date.isoformat(),
            "statement_lines": self.statement_lines,
            "ledger_entries": self.ledger_entries,
            "matched": self.matched,
            "findings": [f.to_dict() for f in self.findings],
            "notes": list(self.notes),
            "errors": list(self.errors),
        }

    # -- rendering ---------------------------------------------------------

    def _headline_lines(self) -> list[str]:
        """The three figures, then one sentence saying which way they point.

        The sentence is there because "-47.13" is a direction nobody reads
        correctly at eleven at night, and reading it backwards sends someone
        hunting the opposite of the problem.
        """
        lines: list[str] = []
        if self.statement_balance is None:
            lines.append(
                "No closing balance was given, so the balances were not compared -- "
                "only the transactions below."
            )
            return lines
        when = self.closing_date.isoformat() if self.closing_date else "the statement date"
        for label, amount in (
            (f"Statement closing balance, {when}", self.statement_balance),
            ("Ledger balance, same date", self.ledger_balance),
            ("Difference (statement - ledger)", self.delta),
        ):
            lines.append(f"  {label:<44}{amount:>12.2f} {self.currency}")
        if self.delta == 0:
            lines.append("  The ledger agrees with the statement to the cent.")
        elif self.delta > 0:
            lines.append("  The bank says you have MORE than the ledger does.")
        else:
            lines.append("  The bank says you have LESS than the ledger does.")
        if self.assertion_date is not None:
            lines.append("")
            lines.append(
                f"  Same claim as a beancount assertion:  {self.assertion_date.isoformat()} "
                f"balance {self.account}   {self.statement_balance} {self.currency}"
            )
            lines.append(
                "  (a `balance` directive is dated the day *after* the closing date it "
                "asserts)"
            )
        return lines

    def _finding_lines(self, findings: tuple[Finding, ...]) -> list[str]:
        lines: list[str] = []
        for f in findings:
            lines.append(f"  {f.delta:>10.2f}  {f.kind.upper()}")
            lines.append(f"              {f.explanation}")
            for entry in f.ledger_entries:
                lines.append(
                    f"              ledger  {entry.date.isoformat()}  {entry.amount:>10.2f}  "
                    f"{entry.description}"
                )
                lines.append(f"                      {entry.location}")
            for line in f.statement_lines:
                lines.append(
                    f"              bank    {line.posted_date.isoformat()}  "
                    f"{line.amount:>10.2f}  {line.description}  [row {line.row}]"
                )
        return lines

    def render(self) -> str:
        if self.errors:
            return "\n".join(["reconcile failed:", *(f"  - {e}" for e in self.errors)])

        lines = [f"Reconciliation — {self.account}"]
        window = ""
        if self.from_date and self.to_date:
            window = f", {self.from_date.isoformat()} to {self.to_date.isoformat()}"
        lines.append(f"Statement: {self.source}{window} ({self.statement_lines} line(s))")
        lines.append("")
        lines.extend(self._headline_lines())

        # Hoisted above everything else, because when it fires the whole rest
        # of the page is an artifact of reading the file backwards. Left in
        # its ordinary place it sits below a list of findings that a reader
        # has already started acting on.
        for finding in self.findings:
            if finding.kind == KIND_SIGN_CONVENTION:
                lines.append("")
                lines.append(f"  READ THIS FIRST: {finding.explanation}")

        if self.statement_lines:
            lines.append("")
            lines.append(
                f"Matched {self.matched} of {self.statement_lines} statement line(s) against "
                f"{self.ledger_entries} ledger entry/entries in the period."
            )

        # Findings that move money and findings that merely inform are printed
        # apart. Mixed together, a `0.00` row sits in a column of amounts that
        # are supposed to add up to the difference, and the reader has to work
        # out which rows are part of the sum — in a section whose whole job is
        # to be readable at a glance while hunting one transaction.
        confirmed = self.confirmed_findings
        moving = tuple(f for f in confirmed if f.delta != 0)
        # The sign-convention warning is excluded here because it was already
        # printed above, in full. It is the one finding that is hoisted, and
        # printing it a second time under a milder heading would undercut the
        # hoist rather than reinforce it.
        context = tuple(
            f for f in confirmed if f.delta == 0 and f.kind != KIND_SIGN_CONVENTION
        )
        if moving:
            lines.append("")
            lines.append("What accounts for the difference")
            lines.extend(self._finding_lines(moving))
            if self.delta is not None:
                lines.append("")
                lines.append(f"  Accounted for  {self.explained:>12.2f} {self.currency}")
                lines.append(f"  Unexplained    {self.residual:>12.2f} {self.currency}")

        candidates = self.candidates
        if candidates:
            lines.append("")
            lines.append(
                "Candidates — any ONE of these would account for the difference. "
                "They are alternatives, not a list to add up:"
            )
            lines.extend(self._finding_lines(candidates))

        if context:
            lines.append("")
            lines.append("Also worth knowing (these move no money)")
            lines.extend(self._finding_lines(context))

        if self.ok:
            lines.append("")
            lines.append("reconcile: OK — the ledger matches this statement.")
        else:
            lines.append("")
            lines.append("reconcile: DOES NOT MATCH")

        if self.notes:
            lines.append("")
            for note in self.notes:
                lines.append(f"  note: {note}")
        return "\n".join(lines)


# -- reading the ledger ----------------------------------------------------


def account_names(entries: list[Any]) -> set[str]:
    """Every reconcilable account the ledger mentions, opened or posted to."""
    names: set[str] = set()
    for entry in entries:
        accounts: tuple[str, ...] = ()
        if isinstance(entry, Transaction):
            accounts = tuple(p.account for p in entry.postings)
        else:
            account = getattr(entry, "account", None)
            if isinstance(account, str):
                accounts = (account,)
        names.update(a for a in accounts if a.startswith(RECONCILABLE_PREFIXES))
    return names


def resolve_account(entries: list[Any], name: str) -> str:
    """A full account name from whatever the user typed.

    A user reconciling their checking account types "Checking", not
    "Assets:SimpleFIN:Checking". A leaf or suffix that names exactly one
    account resolves to it; one that names several is an error listing them,
    never a pick. Guessing which of two accounts a statement belongs to would
    produce a confident reconciliation of the wrong account -- a report that
    is entirely internally consistent and about the wrong money.
    """
    known = account_names(entries)
    if name in known:
        return name
    lowered = name.lower()
    exact_ci = sorted(a for a in known if a.lower() == lowered)
    if len(exact_ci) == 1:
        return exact_ci[0]
    suffix = sorted(a for a in known if a.lower().endswith(":" + lowered))
    if len(suffix) == 1:
        return suffix[0]
    substring = sorted(a for a in known if lowered in a.lower())
    matches = exact_ci or suffix or substring
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ReconcileError(
            f"{name!r} matches more than one account: {', '.join(matches)} -- "
            "name the one you mean in full"
        )
    if not known:
        raise ReconcileError(
            "the ledger has no asset or liability accounts to reconcile against"
        )
    raise ReconcileError(
        f"no account matches {name!r}. The ledger has: {', '.join(sorted(known))}"
    )


def _entry_currency(posting) -> str | None:
    units = posting.units
    return None if units is None else units.currency


def flatten_account_postings(entries: list[Any], account: str) -> tuple[LedgerEntry, ...]:
    """Every posting to `account`, in date order, as comparable records.

    Sorted by `(date, filename, lineno)` rather than by beancount's own entry
    order so the output is stable across loads -- a discrepancy report whose
    candidate list reorders between two runs of the same command is a report
    nobody trusts.
    """
    out: list[LedgerEntry] = []
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        meta = entry.meta or {}
        for posting in entry.postings:
            if posting.account != account or posting.units is None:
                continue
            out.append(
                LedgerEntry(
                    date=entry.date,
                    amount=posting.units.number,
                    description=entry.narration or entry.payee or "",
                    simplefin_id=meta.get("simplefin-id"),
                    filename=meta.get("filename"),
                    lineno=meta.get("lineno"),
                )
            )
    return tuple(sorted(out, key=lambda e: (e.date, e.filename or "", e.lineno or 0)))


def account_currency(entries: list[Any], account: str) -> str | None:
    """The single currency `account` is denominated in, or `None` if unused.

    Raises if the ledger posts more than one currency to it: a statement is in
    one currency, and netting two of them into one comparison would produce a
    number with no meaning.
    """
    found: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        for posting in entry.postings:
            if posting.account == account:
                currency = _entry_currency(posting)
                if currency:
                    found.add(currency)
    if len(found) > 1:
        raise ReconcileError(
            f"{account} holds more than one currency ({', '.join(sorted(found))}); "
            "reconcile one currency at a time"
        )
    return next(iter(found), None)


def balance_asof(ledger_entries: tuple[LedgerEntry, ...], asof: date | None) -> Decimal:
    """Closing balance at the *end* of `asof` -- every posting dated on or
    before it. `None` means "everything", i.e. the account's current balance.

    This is deliberately the end-of-day reading, matching what a statement
    means by "closing balance on the 31st", and it is why
    `ReconcileResult.assertion_date` is a day later: the equivalent beancount
    directive asserts the balance at the *start* of its date.
    """
    return sum(
        (e.amount for e in ledger_entries if asof is None or e.date <= asof), Decimal(0)
    )


# -- matching --------------------------------------------------------------


def _normalize_text(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def _similarity(left: str, right: str) -> float:
    """0..1 likeness of two descriptions. Only ever used to *rank* candidates
    that already agree on amount, never to decide a match on its own."""
    a, b = _normalize_text(left), _normalize_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass(frozen=True)
class _Match:
    line: StatementLine
    entry: LedgerEntry


def _match_lines(
    lines: tuple[StatementLine, ...], entries: tuple[LedgerEntry, ...], tolerance: int
) -> tuple[list[_Match], list[StatementLine], list[LedgerEntry]]:
    """Pair statement lines to ledger entries; return the leftovers on both sides.

    Amount equality is required, never approximated -- two amounts that differ
    are two transactions, and a tolerance on money is how a reconciler starts
    quietly forgiving the errors it exists to find. Date proximity and
    description likeness only decide *which* equal-amount entry a line pairs
    with, which matters when a week holds three identical coffees.

    Greedy over (date distance, then description likeness): the closest,
    most alike pair is taken first. Optimal assignment would be marginally
    better and considerably harder to explain to someone reading a report
    about their own money.
    """
    candidates: list[tuple[int, float, int, int]] = []
    for li, line in enumerate(lines):
        for ei, entry in enumerate(entries):
            if entry.amount != line.amount:
                continue
            distance = abs((entry.date - line.posted_date).days)
            if distance > tolerance:
                continue
            candidates.append((distance, -_similarity(line.description, entry.description), li, ei))
    candidates.sort()

    used_lines: set[int] = set()
    used_entries: set[int] = set()
    matches: list[_Match] = []
    for _distance, _score, li, ei in candidates:
        if li in used_lines or ei in used_entries:
            continue
        used_lines.add(li)
        used_entries.add(ei)
        matches.append(_Match(line=lines[li], entry=entries[ei]))
    return (
        matches,
        [line for i, line in enumerate(lines) if i not in used_lines],
        [entry for i, entry in enumerate(entries) if i not in used_entries],
    )


def _inside(when: date, cutoff: date | None) -> bool:
    """Whether a transaction dated `when` is inside the closing balance."""
    return cutoff is None or when <= cutoff


def _looks_like_duplicate_of(
    entry: LedgerEntry, others: tuple[LedgerEntry, ...], tolerance: int
) -> LedgerEntry | None:
    """Another ledger entry that looks like the same transaction, or `None`.

    Same amount, dated within `tolerance` days, and near-identical
    description. The description test is what keeps a fortnightly identical
    bill from being called a duplicate of itself.
    """
    best: tuple[float, LedgerEntry] | None = None
    for other in others:
        if other is entry or other.amount != entry.amount:
            continue
        if abs((other.date - entry.date).days) > tolerance:
            continue
        score = _similarity(entry.description, other.description)
        if score >= DUPLICATE_SIMILARITY and (best is None or score > best[0]):
            best = (score, other)
    return best[1] if best else None


def _duplicate_dedup_keys(entries: tuple[LedgerEntry, ...]) -> list[tuple[LedgerEntry, ...]]:
    """Groups of entries sharing a `simplefin-id` on this account.

    `render_transaction` documents `simplefin-id` + `simplefin-account` as
    *the* dedup key, so within one account a repeated id is not a resemblance
    to be judged -- it is the same bank transaction recorded twice. This is
    the one duplicate finding that needs no statement and no hypothesis, which
    is why it is reported whether or not it explains the difference.
    """
    by_id: dict[str, list[LedgerEntry]] = {}
    for entry in entries:
        if entry.simplefin_id:
            by_id.setdefault(entry.simplefin_id, []).append(entry)
    return [tuple(group) for _key, group in sorted(by_id.items()) if len(group) > 1]


# -- the findings ----------------------------------------------------------


def _boundary_explanation(entry: LedgerEntry, cutoff: date, ledger_late: bool) -> str:
    if ledger_late:
        return (
            f"date boundary: the ledger dates this {entry.date.isoformat()}, after your "
            f"closing date {cutoff.isoformat()}, so the bank counted it and the ledger "
            "did not. The transaction is present and correct — only the date differs. "
            "This is not a missing transaction."
        )
    return (
        f"date boundary: the ledger dates this {entry.date.isoformat()}, on or before your "
        f"closing date {cutoff.isoformat()}, but the bank had not posted it yet. The "
        "transaction is present and correct — only the date differs. This is not a "
        "duplicate."
    )


def _line_diff_findings(
    lines: tuple[StatementLine, ...],
    entries: tuple[LedgerEntry, ...],
    all_entries: tuple[LedgerEntry, ...],
    cutoff: date | None,
    tolerance: int,
    period: tuple[date, date] | None = None,
    absorbed: dict[str, int] | None = None,
) -> tuple[list[Finding], int]:
    """Confirmed findings from a line-by-line diff, and the match count.

    `absorbed` maps a `simplefin-id` to how many unmatched ledger entries
    carrying it have *already* been accounted for by a dedup-key duplicate
    finding, and is consumed as those entries are met. Without it the same
    double-import is reported twice — once as the broken dedup-key invariant
    it is, once again as a resemblance this pass rediscovered — and, far
    worse, both deltas are summed into `explained`, so the arithmetic
    double-counts the fix and blames the leftover on a discrepancy before the
    statement period. Finding the real cause and then pointing somewhere else
    is the worst thing this module can do.

    Order matters and is the point: matched-but-shifted pairs are classified
    first, so a transaction the bank dated a day earlier can never fall
    through to be reported as missing.

    `entries` is deliberately *wider* than `period` — widened by `tolerance`,
    so a transaction the bank dated inside the statement and the ledger dated
    a day outside it is still available to pair up. Entries in that skirt that
    pair with nothing are then dropped rather than reported: they are ordinary
    transactions from the following week, and calling them "in the ledger, not
    on the statement" would file a page of false findings under the heading a
    user reads first.
    """
    matches, unmatched_lines, unmatched_entries = _match_lines(lines, entries, tolerance)
    findings: list[Finding] = []

    for match in matches:
        if match.line.posted_date == match.entry.date:
            continue
        statement_in = _inside(match.line.posted_date, cutoff)
        ledger_in = _inside(match.entry.date, cutoff)
        if statement_in and not ledger_in:
            findings.append(
                Finding(
                    kind=KIND_DATE_BOUNDARY,
                    delta=match.entry.amount,
                    explanation=_boundary_explanation(match.entry, cutoff, ledger_late=True),
                    confirmed=True,
                    ledger_entries=(match.entry,),
                    statement_lines=(match.line,),
                )
            )
        elif ledger_in and not statement_in:
            findings.append(
                Finding(
                    kind=KIND_DATE_BOUNDARY,
                    delta=-match.entry.amount,
                    explanation=_boundary_explanation(match.entry, cutoff, ledger_late=False),
                    confirmed=True,
                    ledger_entries=(match.entry,),
                    statement_lines=(match.line,),
                )
            )
        else:
            days = (match.entry.date - match.line.posted_date).days
            findings.append(
                Finding(
                    kind=KIND_DATE_SHIFT,
                    delta=Decimal(0),
                    explanation=(
                        f"the ledger dates this {abs(days)} day(s) "
                        f"{'later' if days > 0 else 'earlier'} than the bank does. Both "
                        "dates fall inside this statement, so no money is affected."
                    ),
                    confirmed=True,
                    ledger_entries=(match.entry,),
                    statement_lines=(match.line,),
                )
            )

    for line in unmatched_lines:
        findings.append(
            Finding(
                kind=KIND_MISSING,
                delta=line.amount if _inside(line.posted_date, cutoff) else Decimal(0),
                explanation=(
                    "on the statement, with no matching entry in the ledger — it was "
                    "never imported. Run `bookkeeper sync` covering this date, then "
                    "reconcile again."
                ),
                confirmed=True,
                statement_lines=(line,),
            )
        )

    # Absorption runs as its own pass, before anything is classified, because
    # the entries it takes out must also stop being *evidence*. A spurious
    # copy the dedup-key check already reported is not available to prove that
    # the copy beside it is a duplicate too — with the pool left whole, a
    # charge imported twice and absent from the statement comes back as one
    # dedup duplicate plus a resemblance-duplicate whose text says "the bank
    # recorded the transaction once" when the bank recorded it zero times.
    #
    # Only the extra copies are absorbed, never the whole group: one member is
    # a legitimate transaction, and if the bank does not list that one either
    # it is a genuine `EXTRA` that still has to be reported. Which member the
    # matcher happened to pair up does not matter — they are interchangeable —
    # so the budget is a count. Out-of-period entries are skipped first, so
    # they cannot spend budget that an in-period copy needs.
    budget = dict(absorbed or {})
    absorbed_ids: set[int] = set()
    unexplained: list[LedgerEntry] = []
    for entry in unmatched_entries:
        if period is not None and not period[0] <= entry.date <= period[1]:
            continue
        if entry.simplefin_id and budget.get(entry.simplefin_id, 0) > 0:
            budget[entry.simplefin_id] -= 1
            absorbed_ids.add(id(entry))
            continue
        unexplained.append(entry)

    twin_pool = tuple(e for e in all_entries if id(e) not in absorbed_ids)
    for entry in unexplained:
        twin = _looks_like_duplicate_of(entry, twin_pool, tolerance)
        delta = -entry.amount if _inside(entry.date, cutoff) else Decimal(0)
        if twin is not None:
            findings.append(
                Finding(
                    kind=KIND_DUPLICATE,
                    delta=delta,
                    explanation=(
                        "in the ledger twice: this entry has no statement line of its "
                        "own, and the ledger holds another entry for the same amount "
                        "and description within days of it. The bank recorded the "
                        "transaction once."
                    ),
                    confirmed=True,
                    ledger_entries=(entry, twin),
                )
            )
        else:
            findings.append(
                Finding(
                    kind=KIND_EXTRA,
                    delta=delta,
                    explanation=(
                        "in the ledger, with no matching line on the statement, and "
                        "nothing in the ledger resembling it — so it is not a "
                        "duplicate. Either the bank has not posted it yet or it does "
                        "not belong to this account."
                    ),
                    confirmed=True,
                    ledger_entries=(entry,),
                )
            )
    return findings, len(matches)


def _hypotheses(
    delta: Decimal,
    all_entries: tuple[LedgerEntry, ...],
    cutoff: date | None,
    tolerance: int,
    already_named: frozenset[int] = frozenset(),
) -> list[Finding]:
    """Candidate explanations for a gap, from the ledger alone.

    This is the balance-only path -- "on this date my balance was X", no file.
    Nothing here is observed, so every finding comes back `confirmed=False`
    and the render presents them as alternatives.

    Ordered by how likely and how cheap to check they are: date boundary
    first, because it is both the most common cause and the one where the
    money is already right; then duplicates, which are real errors with
    evidence behind them; then bare amount matches; and only then, if nothing
    in the ledger can account for the gap, the conclusion that a transaction
    is simply absent.
    """
    findings: list[Finding] = []
    # Entries an observed finding already accounts for start out claimed, so a
    # pair reported as a *confirmed* duplicate is not then offered again as a
    # hypothesis about itself. Two findings naming the same two entries reads
    # as two problems, and the second one contradicts the first by calling a
    # measurement a guess.
    claimed: set[int] = set(already_named)

    def take(entry: LedgerEntry) -> bool:
        if id(entry) in claimed:
            return False
        claimed.add(id(entry))
        return True

    if cutoff is not None:
        # The ledger dates it after the cutoff; the bank counted it before.
        for entry in all_entries:
            in_window = cutoff < entry.date <= cutoff + timedelta(days=tolerance)
            if in_window and entry.amount == delta and take(entry):
                findings.append(
                    Finding(
                        kind=KIND_DATE_BOUNDARY,
                        delta=entry.amount,
                        explanation=_boundary_explanation(entry, cutoff, ledger_late=True),
                        ledger_entries=(entry,),
                    )
                )
        # The ledger counts it before the cutoff; the bank had not posted it.
        for entry in all_entries:
            in_window = cutoff - timedelta(days=tolerance) <= entry.date <= cutoff
            if in_window and entry.amount == -delta and take(entry):
                findings.append(
                    Finding(
                        kind=KIND_DATE_BOUNDARY,
                        delta=-entry.amount,
                        explanation=_boundary_explanation(entry, cutoff, ledger_late=False),
                        ledger_entries=(entry,),
                    )
                )

    inside = tuple(e for e in all_entries if _inside(e.date, cutoff))
    for entry in inside:
        if entry.amount != -delta or id(entry) in claimed:
            continue
        twin = _looks_like_duplicate_of(entry, all_entries, tolerance)
        if twin is not None and take(entry):
            # Claim the twin too. Both halves of a suspected pair have the
            # amount that closes the gap and both have a twin, so without this
            # the same pair is offered twice as two "alternatives" that are in
            # fact one — and the candidate list, whose whole value is being
            # short, doubles in length saying nothing new.
            take(twin)
            findings.append(
                Finding(
                    kind=KIND_DUPLICATE,
                    delta=-entry.amount,
                    explanation=(
                        "possible duplicate: the ledger holds two entries for this "
                        "amount and description within days of each other, and dropping "
                        "one closes the gap exactly. Check the bank's own listing before "
                        "removing either."
                    ),
                    ledger_entries=(entry, twin),
                )
            )

    for entry in inside:
        if entry.amount == -delta and take(entry):
            findings.append(
                Finding(
                    kind=KIND_AMOUNT_MATCH,
                    delta=-entry.amount,
                    explanation=(
                        "this entry's amount is exactly the difference. If it should not "
                        "be in the ledger, or its amount is wrong, it accounts for the "
                        "whole gap on its own."
                    ),
                    ledger_entries=(entry,),
                )
            )

    if not findings:
        findings.append(
            Finding(
                kind=KIND_MISSING,
                delta=delta,
                explanation=(
                    f"nothing in the ledger accounts for this difference: no entry of "
                    f"{-delta} to remove, and nothing near the closing date to re-date. "
                    f"The likeliest reading is a transaction of {delta} that the bank "
                    "posted and the ledger never received. Export the statement as CSV "
                    "and re-run with --statement to have it named."
                ),
            )
        )
    return findings


def _cap(findings: list[Finding], notes: list[str]) -> list[Finding]:
    """Trim *unconfirmed* findings to `MAX_CANDIDATES` per kind.

    The rendered output is read by someone hunting one transaction. Forty
    same-amount rows is a dump, and a dump is where a real finding goes to
    hide -- but silently dropping evidence is worse, so the count survives as
    a note.

    **Confirmed findings are never capped, and that is a correctness
    requirement rather than a presentation preference.** `explained` is summed
    over the confirmed findings in the returned list, so trimming one does not
    hide it -- it removes it from the arithmetic. A sync gap leaving eight
    missing charges of 10.00 reported `explained` of 60.00 against a delta of
    80.00, and the 20.00 residual then told the user the rest "starts before
    the period, or the closing balance itself is wrong". Both false: the
    module had accounted for all of it and then sent them somewhere else to
    look. That is the same failure this module was already fixed for once, so
    the cap now applies only to hypotheses, which carry a zero delta and
    genuinely are a browsable list.
    """
    kept: list[Finding] = []
    seen: dict[str, int] = {}
    withheld: dict[str, int] = {}
    for finding in findings:
        if finding.confirmed:
            kept.append(finding)
            continue
        seen[finding.kind] = seen.get(finding.kind, 0) + 1
        if seen[finding.kind] <= MAX_CANDIDATES:
            kept.append(finding)
        else:
            withheld[finding.kind] = withheld.get(finding.kind, 0) + 1
    for kind, count in sorted(withheld.items()):
        notes.append(
            f"{count} further {kind} candidate(s) not shown (showing the first "
            f"{MAX_CANDIDATES}); narrow the period to see them"
        )
    return kept


def _feed_findings(
    entries: list[Any], account: str, statement: Statement, assertion_date: date | None
) -> list[Finding]:
    """The ledger's own SimpleFIN balance assertion, where it contradicts the
    statement.

    This locates a discrepancy rather than explaining it, so it carries a zero
    delta — but it is often the most valuable line in the report. If the feed's
    assertion for the same instant disagrees with the paper statement, the
    ledger is a faithful record of a feed that is itself wrong or stale, and no
    amount of hunting through transactions will find the problem.
    """
    if assertion_date is None or statement.closing_balance is None:
        return []
    for entry in entries:
        if not isinstance(entry, Balance) or entry.account != account:
            continue
        if entry.date != assertion_date or entry.amount is None:
            continue
        if entry.amount.number == statement.closing_balance:
            continue
        return [
            Finding(
                kind=KIND_FEED_DISAGREES,
                delta=Decimal(0),
                explanation=(
                    f"the ledger already asserts {entry.amount.number} for this account at "
                    f"this same moment ({assertion_date.isoformat()}, from SimpleFIN's own "
                    f"reported balance), and your statement says "
                    f"{statement.closing_balance}. The feed and the bank disagree — the "
                    "discrepancy is upstream of the ledger, not inside it."
                ),
                confirmed=True,
            )
        ]
    return []


def _sign_convention_note(
    lines: tuple[StatementLine, ...], entries: tuple[LedgerEntry, ...], tolerance: int
) -> str | None:
    """Detect a whole-file sign inversion, and refuse to fix it silently.

    A CSV whose withdrawals are positive is common and is the one parsing
    mistake that produces a fully-populated, entirely wrong report. It is
    detected by matching the file both ways and comparing: if flipping the
    signs matches strictly more lines, the file is inverted.

    Never applied automatically. Negating a user's money on a heuristic is
    exactly the silent wrongness this module exists to catch; the user passes
    `--flip-signs` once they have read the sentence and agree.
    """
    if not lines or not entries:
        return None
    as_is = len(_match_lines(lines, entries, tolerance)[0])
    flipped_lines = tuple(replace(line, amount=-line.amount) for line in lines)
    flipped = len(_match_lines(flipped_lines, entries, tolerance)[0])
    if flipped <= as_is:
        return None
    return (
        f"this file's signs look inverted: {flipped} of {len(lines)} lines match the "
        f"ledger with their signs flipped, against {as_is} as written. Your bank is "
        "probably exporting withdrawals as positive numbers. Nothing was flipped "
        "automatically — re-run with --flip-signs if you agree."
    )


# -- the reconciliation ----------------------------------------------------


def reconcile_entries(
    entries: list[Any],
    statement: Statement,
    *,
    tolerance: int = MATCH_WINDOW_DAYS,
    options: dict[str, Any] | None = None,
) -> ReconcileResult:
    """Compare an already-loaded ledger against `statement`. Pure.

    Split from `run_reconcile` for the same reason `compute_envelope_state` is
    split from `envelope_report`: the API holds an mtime-cached ledger and
    must not pay `loader.load_file` again per request (§5.1).

    Raises `ReconcileError` for a request that cannot be answered at all --
    an unknown account, mixed currencies. A *discrepancy* is not an error: it
    is a successful reconciliation whose answer is non-zero, and it comes back
    as a result with `ok=False`.
    """
    account = resolve_account(entries, statement.account)
    ledger_entries = flatten_account_postings(entries, account)
    currency = (
        account_currency(entries, account)
        or statement.currency
        or str((options or {}).get("operating_currency", [DEFAULT_CURRENCY])[0])
    )
    if statement.currency and statement.currency != currency:
        raise ReconcileError(
            f"the statement is in {statement.currency} but {account} is denominated in "
            f"{currency}; reconcile one currency at a time"
        )

    cutoff = statement.closing_date
    assertion_date = cutoff + timedelta(days=1) if cutoff else None
    ledger_balance = balance_asof(ledger_entries, cutoff)
    delta = (
        statement.closing_balance - ledger_balance
        if statement.closing_balance is not None
        else None
    )

    notes: list[str] = list(statement.warnings)
    findings: list[Finding] = []
    matched = 0

    covers_from = statement.covers_from
    covers_to = statement.covers_to
    # Ledger entries the statement's period speaks for, widened by the
    # tolerance so a transaction the bank dated inside the period and the
    # ledger dated a day outside it is still available to match. Without the
    # widening, the very off-by-one this module exists to catch would present
    # as a missing transaction plus an unexplained residual.
    period = (covers_from, covers_to) if covers_from and covers_to else None
    if period is not None:
        window = tuple(
            e
            for e in ledger_entries
            if period[0] - timedelta(days=tolerance)
            <= e.date
            <= period[1] + timedelta(days=tolerance)
        )
        # What the period actually contains, as opposed to what was made
        # available to match against. The two are different numbers and the
        # report claims the first one.
        in_period = tuple(e for e in window if period[0] <= e.date <= period[1])
    else:
        window = in_period = ledger_entries

    # How many spurious copies the dedup-key check has already accounted for,
    # per id. Consumed by the line diff below so the same double-import is not
    # reported — and counted — a second time.
    absorbed: dict[str, int] = {}
    for group in _duplicate_dedup_keys(window):
        if group[0].simplefin_id:
            absorbed[group[0].simplefin_id] = len(group) - 1
        findings.append(
            Finding(
                kind=KIND_DUPLICATE,
                # Every copy after the first is spurious. Confirmed regardless
                # of the balance: `simplefin-id` + account is the dedup key
                # (`ingest/render.py`), so a repeat is a broken invariant, not
                # a resemblance. Only the copies dated inside the closing
                # balance move it, so only those are counted — a spurious copy
                # dated after the cutoff is still a defect, just not this
                # statement's arithmetic.
                delta=-sum(
                    (e.amount for e in group[1:] if _inside(e.date, cutoff)), Decimal(0)
                ),
                explanation=(
                    f"{len(group)} ledger entries share the SimpleFIN id "
                    f"{group[0].simplefin_id!r} on this account. That pair is the import "
                    "dedup key, so this is the same bank transaction recorded more than "
                    "once."
                ),
                confirmed=True,
                ledger_entries=group,
            )
        )

    if statement.lines:
        inverted = _sign_convention_note(statement.lines, window, tolerance)
        if inverted:
            # Carried as a finding only, not also as a note: the render prints
            # both sections, and forty identical words twice on one page reads
            # as a formatting bug rather than as emphasis.
            findings.append(
                Finding(
                    kind=KIND_SIGN_CONVENTION,
                    delta=Decimal(0),
                    explanation=inverted,
                    confirmed=True,
                )
            )
        diff, matched = _line_diff_findings(
            statement.lines, window, ledger_entries, cutoff, tolerance, period, absorbed
        )
        findings.extend(diff)
    elif delta is not None:
        # Hunt the *unexplained* part of the gap, not the whole of it. Anything
        # already observed — a dedup-key duplicate, most often — has both
        # explained its share and ruled itself out as a hypothesis, so
        # searching for the full delta looks for an explanation of something
        # that is no longer unexplained. When nothing is left over there is
        # nothing to hypothesise about, and this is what stops the fallback
        # below from printing "nothing in the ledger accounts for this
        # difference" directly beneath a finding that accounted for all of it.
        remaining = delta - sum((f.delta for f in findings if f.confirmed), Decimal(0))
        if remaining != 0:
            named = frozenset(id(e) for f in findings for e in f.ledger_entries)
            findings.extend(_hypotheses(remaining, ledger_entries, cutoff, tolerance, named))

    findings.extend(_feed_findings(entries, account, statement, assertion_date))

    if statement.count is not None and period is not None:
        counted = len(in_period)
        if counted != statement.count:
            findings.append(
                Finding(
                    kind=KIND_COUNT,
                    # A count says nothing about amounts, so it explains none
                    # of the difference — but it catches what a balance cannot:
                    # two errors of equal and opposite size.
                    delta=Decimal(0),
                    explanation=(
                        f"the statement says {statement.count} transaction(s) in this "
                        f"period; the ledger has {counted}. A balance alone cannot see "
                        "two errors that cancel; a count can."
                    ),
                    confirmed=True,
                )
            )

    findings = _cap(findings, notes)
    explained = sum((f.delta for f in findings if f.confirmed), Decimal(0))
    residual = (delta - explained) if delta is not None else Decimal(0)

    # Only worth saying when something *was* accounted for: on a run that
    # explained nothing, "the rest is unexplained" is the whole report
    # restated, and a report that repeats itself is one people skim.
    partly_explained = any(f.confirmed and f.delta != 0 for f in findings)
    if delta is not None and delta != 0 and residual != 0 and partly_explained:
        notes.append(
            f"{residual} of the difference is not accounted for by anything in this "
            "statement's period. That part of the discrepancy starts before "
            f"{covers_from.isoformat() if covers_from else 'the period'}, or the closing "
            "balance itself is wrong."
        )
    if delta is None:
        notes.append(
            "no closing balance was given, so this run proves only that the "
            "transactions match — not that the account totals do. Re-run with the "
            "statement's closing balance to check the money."
        )

    breaking = any(f.confirmed and f.kind in BREAKING_KINDS for f in findings)
    ok = not breaking and (delta is None or delta == 0)

    return ReconcileResult(
        ok=ok,
        account=account,
        currency=currency,
        source=statement.source,
        closing_date=cutoff,
        assertion_date=assertion_date,
        statement_balance=statement.closing_balance,
        ledger_balance=ledger_balance,
        delta=delta,
        explained=explained,
        residual=residual,
        from_date=covers_from,
        to_date=covers_to,
        statement_lines=len(statement.lines),
        ledger_entries=len(in_period),
        matched=matched,
        findings=tuple(findings),
        notes=notes,
    )


def _coerce_date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _flip(statement: Statement) -> Statement:
    """`statement` with every line's sign reversed. Opt-in only.

    The closing balance is deliberately left alone: a debit-positive export is
    a per-line column convention, not a claim that the account's balance is
    negated.
    """
    return replace(
        statement,
        lines=tuple(replace(line, amount=-line.amount) for line in statement.lines),
        warnings=(*statement.warnings, "every statement amount was sign-flipped (--flip-signs)"),
    )


def run_reconcile(
    account: str,
    balance: str | Decimal | None = None,
    on: date | str | None = None,
    statement: str | None = None,
    *,
    since: date | str | None = None,
    count: int | None = None,
    window: int = MATCH_WINDOW_DAYS,
    flip_signs: bool = False,
) -> ReconcileResult:
    """Load the ledger and reconcile `account` against a statement.

    The simplest call is the one every user can make: an account, a closing
    balance, and the date it was the balance on. `statement` adds a CSV export
    of the same period, which is what turns "you disagree by 47.13" into a
    named transaction.

    Every failure that is the *caller's* -- an unreadable file, an unknown
    account, an unparseable date -- comes back as a result with `ok=False` and
    an error, not an exception, because `cli.py`'s dispatcher prints
    `.render()` and returns `.ok`. A discrepancy is likewise a result, not an
    error; the two are told apart by `errors` being empty.
    """
    try:
        closing_date = _coerce_date(on)
        from_date = _coerce_date(since)
        closing_balance = (
            balance
            if balance is None or isinstance(balance, Decimal)
            else parse_money(str(balance))
        )
    except (ValueError, StatementError) as exc:
        return ReconcileResult(ok=False, account=account, errors=[str(exc)])

    if closing_balance is not None and closing_date is None:
        return ReconcileResult(
            ok=False,
            account=account,
            errors=[
                (
                    "a closing balance needs the date it was the balance on -- pass --on "
                    "YYYY-MM-DD. A balance without a date cannot be compared against "
                    "anything."
                )
            ],
        )
    if closing_balance is None and statement is None:
        return ReconcileResult(
            ok=False,
            account=account,
            errors=[
                (
                    "nothing to reconcile against: give a closing balance (--balance with "
                    "--on) or a statement export (--statement), or both."
                )
            ],
        )

    entries, bean_errors, options = load_ledger()

    try:
        if statement is not None:
            parsed = load_statement_csv(
                statement,
                account=account,
                closing_date=closing_date,
                closing_balance=closing_balance,
                from_date=from_date,
            )
        else:
            parsed = Statement(
                account=account,
                closing_date=closing_date,
                closing_balance=closing_balance,
                from_date=from_date,
                source="balance only",
            )
        if count is not None:
            parsed = replace(parsed, count=count)
        if flip_signs:
            parsed = _flip(parsed)
        result = reconcile_entries(entries, parsed, tolerance=window, options=options)
    except (ReconcileError, StatementError) as exc:
        return ReconcileResult(ok=False, account=account, errors=[str(exc)])

    if bean_errors:
        # A note, never a refusal. The single likeliest reason this ledger does
        # not load cleanly is a *failing balance assertion* — which is the
        # exact moment a user reaches for this command. Refusing to run then
        # would withhold the tool precisely when it is wanted.
        result.notes.append(
            f"the ledger loaded with {len(bean_errors)} error(s), so the figures above "
            "may be built on an incomplete parse — run `bookkeeper verify` to see them"
        )
    return result
