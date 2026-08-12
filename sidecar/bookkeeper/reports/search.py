"""Free-text search over ledger transactions (PLAN.md §5.3, `search_transactions`).

Reached from a chat message, so `q` is untrusted text that a model or a user
typed. Two separate injection surfaces have to be closed, and closing one does
not close the other:

- **BQL injection.** `q` is bound as a *query parameter* (`%(q)s`, beanquery's
  pyformat paramstyle) and never formatted into the query string. Nothing the
  user types can add a clause, and the query text is a constant this module
  owns.
- **Regex injection.** BQL's `~` is a regular-expression match, so a bound
  parameter is still compiled as a pattern. `(a+)+$` is a valid parameter and a
  catastrophic backtracker; `[` is a valid parameter and a `re.error` 500. So
  the parameter is `re.escape`d and prefixed with `(?i)`, which makes the whole
  search a case-insensitive *literal substring* match -- the thing a person
  typing into a chat box means anyway.

One row per transaction *leg on a funding account*, not one row per posting:
a spend has two postings and returning both would show every result twice.
The funding leg is the useful one -- it carries the signed amount the bank
reported -- and the categorized account is recovered from the same row's
`other_accounts`, so a match still reports where the money was filed.

Matching spans narration, payee, and any account on the transaction
(`has_account`), so "groceries" finds both the merchant and the envelope
account it was filed under.

**Totals.** *"How much did I spend at Whole Foods"* had no correct tool: this
module matched the text without adding it up, and the spending report groups
by envelope rather than by merchant, so the question could only be answered
wrongly. `amount_totals` closes that without a new tool (§5.3 caps the tool
surface).

The total is deliberately *not* one number. A match set spans accounts, both
currencies and both directions, and a single figure labelled "total" over that
mixture is a claim a user would act on and that would be false:

- **Currencies are never added.** One `CurrencyTotal` per currency; if the
  matches span more than one, that is said out loud and no cross-currency
  figure is offered, because there is no exchange rate in the ledger and
  inventing one here would be worse than declining.
- **Refunds do not silently reduce spending.** Money out and money in are
  summed separately and both are reported; `net` is their difference and is
  always labelled as net. A single netted figure alone would answer "how much
  did I spend" with a smaller number than was actually spent.
- **Transfers between the user's own accounts are excluded and named.** A
  credit-card payment matches on both of its funding legs, so counting it
  would report the same money as both spent and received. A transaction whose
  every counter-account is itself a funding account is money moved, not money
  spent, and it gets its own line rather than a silent drop.

Totals cover every match, not the page of matches `limit` returned -- a total
over the visible rows would understate the answer precisely when the result
was large enough for someone to need the total.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import beanquery

from bookkeeper.envelope.compute import load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)

#: How many matches to return when the caller does not say. Small on purpose:
#: this feeds a chat surface that renders cards, not a spreadsheet.
DEFAULT_LIMIT = 50

#: Hard ceiling regardless of what the caller asks for. A tool argument comes
#: from an 8B model (§3.3) and `limit=100000` is a plausible thing for one to
#: emit; the ledger read is cheap but the JSON is not.
MAX_LIMIT = 500

#: The account namespaces the user owns: money sitting in them, or owed from
#: them, is theirs to move. Liabilities included so a credit card charge is
#: found the same way a debit is.
_FUNDING_PREFIXES: tuple[str, ...] = ("Assets:", "Liabilities:")

#: The accounts a transaction is funded from, as the BQL pattern the query
#: binds. Derived from `_FUNDING_PREFIXES` so the rows the query returns and
#: the rows `_amount_totals` calls a transfer can never disagree about what
#: "an account the user owns" means.
_FUNDING_ACCOUNT_PATTERN = "^(" + "|".join(p.rstrip(":") for p in _FUNDING_PREFIXES) + "):"

#: Constant query text. The only variable parts are bound parameters.
_SEARCH_QUERY = """
SELECT
    id,
    date,
    narration,
    payee,
    account,
    other_accounts,
    number,
    currency,
    entry_meta("simplefin-id") AS simplefin_id,
    entry_meta("simplefin-payee") AS simplefin_payee,
    entry_meta("simplefin-memo") AS simplefin_memo
FROM #postings
WHERE account ~ %(funding)s
  AND (narration ~ %(q)s OR payee ~ %(q)s OR has_account(%(q)s))
ORDER BY date DESC, narration
"""


@dataclass(frozen=True)
class TransactionMatch:
    """One matching transaction, with enough detail to identify it by eye.

    Primitives and `Decimal`/`date` only -- no beancount objects -- for the
    same reason `categorize.review.ReviewEntry` is: this is rendered in a
    browser, and a beancount object leaking into the JSON would be discovered
    there rather than here.
    """

    posted_date: date
    description: str
    amount: Decimal
    currency: str
    account: str
    categorized_account: str | None = None
    envelope: str | None = None
    simplefin_id: str | None = None
    payee: str | None = None
    memo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "posted_date": self.posted_date.isoformat(),
            "description": self.description,
            "amount": str(self.amount),
            "currency": self.currency,
            "account": self.account,
            "categorized_account": self.categorized_account,
            "envelope": self.envelope,
            "simplefin_id": self.simplefin_id,
            "payee": self.payee,
            "memo": self.memo,
        }


@dataclass(frozen=True)
class CurrencyTotal:
    """What the matches add up to, in one currency, in each direction.

    Three figures rather than one because the three answer different
    questions and only one of them is "how much did I spend":

    - `spent` — money that left a funding account, as a positive figure.
    - `received` — money that arrived: refunds, income, reimbursements.
    - `net` — `spent - received`. Useful, and dangerous unlabelled, which is
      why it is never the only figure reported.

    `transferred` is money moved between the user's own accounts (a card
    payment, a transfer to savings) and is in *none* of the three: it is not
    spending, and the search returns both of its funding legs, so counting it
    would report the same dollar as spent and received at once. Reported
    rather than dropped so the figures visibly account for every match.
    """

    currency: str
    spent: Decimal
    received: Decimal
    net: Decimal
    transferred: Decimal
    spend_count: int
    receipt_count: int
    transfer_count: int
    #: The funding accounts these figures span, so a total across a checking
    #: account and a credit card is legible as such rather than looking like
    #: one account's activity.
    accounts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "spent": str(self.spent),
            "received": str(self.received),
            "net": str(self.net),
            "transferred": str(self.transferred),
            "spend_count": self.spend_count,
            "receipt_count": self.receipt_count,
            "transfer_count": self.transfer_count,
            "accounts": list(self.accounts),
        }


@dataclass
class TransactionSearch:
    ok: bool
    query: str = ""
    matches: list[TransactionMatch] = field(default_factory=list)
    #: How many transactions matched. A *count*, not money — the money is in
    #: `amount_totals`, which is named apart from this on purpose: `total`
    #: and `totals` one keystroke apart in the generated TypeScript is a bug
    #: waiting to be written.
    total: int = 0
    #: One entry per currency among the matches, over *all* matches rather
    #: than the `limit`ed page in `matches`. Empty only when nothing matched.
    amount_totals: list[CurrencyTotal] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def mixed_currency(self) -> bool:
        """True when the matches cannot be reduced to a single figure.

        The ledger carries no exchange rates, so more than one currency in a
        result means the honest answer is several numbers. Exposed so a caller
        can refuse to render a headline figure instead of picking one.
        """
        return len(self.amount_totals) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "query": self.query,
            "total": self.total,
            "shown": len(self.matches),
            "limit": self.limit,
            "truncated": self.truncated,
            "matches": [m.to_dict() for m in self.matches],
            "amount_totals": [t.to_dict() for t in self.amount_totals],
            "mixed_currency": self.mixed_currency,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def _render_totals(self) -> list[str]:
        """The totals block, as lines, or nothing when there is nothing to add.

        Spent / received / net are printed together and always, even at zero,
        because they are meant to be read as arithmetic: a "net" figure whose
        two operands are sometimes absent is a figure a reader has to guess
        the meaning of. `transferred` is printed only when it is non-zero —
        it is an *exclusion* note rather than a term in that sum, and a
        permanent "transferred: 0.00" on every search would train readers to
        skip the line on the one occasion it matters.
        """
        if not self.amount_totals:
            return []

        scope = f"all {self.total} matching transaction(s)"
        if self.truncated:
            scope += f" — not only the {len(self.matches)} shown"
        lines = ["", f"Totals over {scope}:"]
        if self.mixed_currency:
            currencies = ", ".join(t.currency for t in self.amount_totals)
            lines.append(f"  matches span {currencies}; each is totalled on its own — ")
            lines.append("  the ledger holds no exchange rates, so there is no combined figure.")

        for t in self.amount_totals:
            # The currency gets its own line so the figures below it line up
            # regardless of how long the code is; a ragged column is how a
            # reader ends up comparing the wrong two numbers.
            lines.append(f"  {t.currency}")
            lines.append(f"    spent      {t.spent:>12.2f}  over {t.spend_count} transaction(s)")
            lines.append(
                f"    received   {t.received:>12.2f}  over {t.receipt_count} "
                "(refunds or money in)"
            )
            lines.append(f"    net spend  {t.net:>12.2f}")
            if t.transferred:
                lines.append(
                    f"    excluded   {t.transferred:>12.2f}  over {t.transfer_count} "
                    "moved between your own accounts, which is not spending"
                )
            lines.append(f"    accounts:  {', '.join(t.accounts)}")
        return lines

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["search failed:", *(f"  - {e}" for e in self.errors)])
        if not self.matches:
            return f"no transactions match {self.query!r}"

        lines = [f"{self.total} transaction(s) match {self.query!r}; showing {len(self.matches)}."]
        if self.truncated:
            lines.append(f"(limited to {self.limit} — narrow the search or raise --limit)")
        lines.append("")
        for m in self.matches:
            lines.append(
                f"{m.posted_date.isoformat()}  {m.amount:>12} {m.currency}  {m.description}"
            )
            filed = m.categorized_account or "—"
            envelope = f"  [{m.envelope}]" if m.envelope else ""
            lines.append(f"    {m.account} -> {filed}{envelope}")
            if m.simplefin_id:
                lines.append(f"    id: {m.simplefin_id}")

        lines.extend(self._render_totals())

        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines).rstrip("\n")


def literal_pattern(q: str) -> str:
    """`q` as a regex that can only ever match itself, case-insensitively.

    Public because it is the guard, and a guard that cannot be tested directly
    is a guard nobody checks. See the module docstring for why binding `q` as
    a parameter is necessary but not sufficient.
    """
    return f"(?i){re.escape(q)}"


def _coerce_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _envelope_map(entries: list[Any]) -> tuple[dict[str, str], list[str]]:
    """Account -> envelope, or an empty map plus a warning.

    A malformed or ambiguous mapping must not fail a *search*: the envelope
    label is decoration on a result row, while the transactions themselves are
    exactly what someone debugging a broken mapping is trying to look at.
    `verify` is the endpoint that judges the mapping.
    """
    try:
        parsed = parse_envelope_directives(entries)
        return build_account_map(parsed.maps), []
    except DirectiveError as exc:
        return {}, [f"envelope labels omitted: {exc}"]


def _counter_accounts(value: Any) -> tuple[str, ...]:
    """A row's `other_accounts` as a sorted tuple.

    Sorted rather than arbitrary so repeated searches over an unchanged ledger
    return byte-identical results.
    """
    if not value:
        return ()
    if isinstance(value, (set, frozenset, list, tuple)):
        return tuple(sorted(value))
    return (str(value),)


def _first_other_account(value: Any) -> str | None:
    """The counter-account from a row's `other_accounts` set.

    A split transaction reports the whole set joined, because dropping legs
    would misrepresent where the money went.
    """
    accounts = _counter_accounts(value)
    return ", ".join(accounts) if accounts else None


def _is_transfer(counter_accounts: tuple[str, ...]) -> bool:
    """True when every counter-account is one the user owns.

    A card payment (`Assets:Checking` -> `Liabilities:CreditCard`) has both
    legs on funding accounts, so the search returns it twice — once as money
    out and once as money in. Summing those as spending and as a refund would
    report the same dollar in both directions and net it back to nothing,
    which reads as "you spent nothing at Chase" on a real payment.

    An empty set is not a transfer: a one-legged or padded entry has nowhere
    to have moved *to*, and calling it a transfer would silently drop it.
    """
    return bool(counter_accounts) and all(
        a.startswith(_FUNDING_PREFIXES) for a in counter_accounts
    )


@dataclass
class _Bucket:
    """Mutable accumulator for one currency. Collapsed into a `CurrencyTotal`."""

    spent: Decimal = Decimal(0)
    received: Decimal = Decimal(0)
    transferred: Decimal = Decimal(0)
    spend_count: int = 0
    receipt_count: int = 0
    transfer_count: int = 0
    accounts: set[str] = field(default_factory=set)


def _amount_totals(rows: list[dict[str, Any]]) -> tuple[CurrencyTotal, ...]:
    """Sum matched rows per currency, keeping direction and transfers apart.

    Every arithmetic step is `Decimal`; a float anywhere here would put a
    rounding error into the one number the user reads as the answer.

    Currencies are bucketed, never combined — see the module docstring. Rows
    with a zero amount join no bucket's figures but still name their account,
    since a zero is not evidence of a different currency's activity.
    """
    buckets: dict[str, _Bucket] = defaultdict(_Bucket)
    for row in rows:
        amount = row["number"]
        if amount is None:
            continue
        bucket = buckets[row["currency"] or ""]
        bucket.accounts.add(row["account"])
        if _is_transfer(_counter_accounts(row["other_accounts"])):
            # Counted on the outgoing leg only: the matching incoming leg is
            # the same money and is in this same result set.
            if amount < 0:
                bucket.transferred += -amount
                bucket.transfer_count += 1
        elif amount < 0:
            bucket.spent += -amount
            bucket.spend_count += 1
        elif amount > 0:
            bucket.received += amount
            bucket.receipt_count += 1

    return tuple(
        CurrencyTotal(
            currency=currency,
            spent=bucket.spent,
            received=bucket.received,
            net=bucket.spent - bucket.received,
            transferred=bucket.transferred,
            spend_count=bucket.spend_count,
            receipt_count=bucket.receipt_count,
            transfer_count=bucket.transfer_count,
            accounts=tuple(sorted(bucket.accounts)),
        )
        for currency, bucket in sorted(buckets.items())
    )


def search_transactions(
    q: str,
    limit: int | None = None,
    *,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> TransactionSearch:
    """Transactions whose narration, payee, or accounts contain `q`.

    `entries`/`errors`/`options` let the sidecar feed its mtime-cached ledger
    in rather than paying `loader.load_file` per keystroke (§5.1); omitting
    them loads the ledger from disk, which is what the CLI wants.
    """
    text = q.strip() if q else ""
    if not text:
        return TransactionSearch(ok=False, query=q or "", errors=["search text is empty"])

    resolved_limit = _coerce_limit(limit)
    if entries is None:
        entries, errors, options = load_ledger()

    account_to_envelope, warnings = _envelope_map(entries)

    connection = beanquery.connect(
        "beancount:", entries=entries, errors=list(errors or []), options=dict(options or {})
    )
    cursor = connection.execute(
        _SEARCH_QUERY, {"q": literal_pattern(text), "funding": _FUNDING_ACCOUNT_PATTERN}
    )
    columns = [d.name for d in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    # One transaction, not one posting. `#postings` yields a row per matching
    # funding leg, and a transfer has two of them -- a card payment came back
    # as two rows, once at -500.00 and once at +500.00, and `total` counted it
    # twice. `_is_transfer` already keeps that from double-counting the *money*
    # (it is excluded from spend and receipt totals), but the count and the
    # rendered rows never got the same treatment, so the card showed the
    # payment twice while the totals block below it correctly said "excluded
    # 500.00" -- two halves of one response contradicting each other.
    #
    # `id` is beanquery's entry identity, so both legs of one transaction carry
    # the same value. Deduping on it is exact, where a date-and-narration key
    # would collapse two genuinely distinct identical charges on one day.
    seen_entries: set[str] = set()
    distinct: list[dict[str, Any]] = []
    for row in rows:
        entry_id = row["id"]
        if entry_id in seen_entries:
            continue
        seen_entries.add(entry_id)
        distinct.append(row)

    matches = []
    for row in distinct[:resolved_limit]:
        categorized = _first_other_account(row["other_accounts"])
        matches.append(
            TransactionMatch(
                posted_date=row["date"],
                description=row["narration"] or "",
                amount=row["number"],
                currency=row["currency"] or "",
                account=row["account"],
                categorized_account=categorized,
                envelope=account_to_envelope.get(categorized or ""),
                simplefin_id=row["simplefin_id"],
                payee=row["payee"] or row["simplefin_payee"],
                memo=row["simplefin_memo"],
            )
        )

    # Over `rows`, not `matches`: the limit truncates what is *shown*, and a
    # total over the visible page would understate the answer exactly when the
    # result was big enough for someone to need a total.
    totals = _amount_totals(rows)
    if len(totals) > 1:
        warnings.append(
            "matches span "
            + ", ".join(t.currency for t in totals)
            + "; each currency is totalled separately and they are not added — "
            "the ledger carries no exchange rates"
        )

    return TransactionSearch(
        ok=True,
        query=text,
        matches=matches,
        total=len(distinct),
        amount_totals=list(totals),
        limit=resolved_limit,
        truncated=len(distinct) > resolved_limit,
        warnings=warnings,
    )
