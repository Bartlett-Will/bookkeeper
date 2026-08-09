"""Envelope gaps: unmapped expense accounts, and what they historically cost.

`bookkeeper verify` already hard-fails on an expense account with postings
and no `custom "envelope" "map"` directive -- PLAN.md §5.2's silent-drift
guard, and it stays a hard error. This module answers a different question.
After a real backfill a user meets many unmapped accounts at once, and
`verify`'s job ("your books are wrong, here is the list") is not the same
job as "how much of my budget is missing, and where". So the *set* here is
`verify`'s set, computed by calling `verify`'s own finder -- two answers to
"which accounts are unmapped" is precisely the drift §5.2 warns about -- and
what is added is size, span and history.

## The monthly figure is a description of the past

`observed_monthly_mean` is the mean of money already spent, over complete
months already recorded. It is not a recommended allocation and this module
has no opinion about what anyone should budget: it cannot see an intention,
a raise, a cancelled subscription or a move. The field is named for what it
measured, `render()` says "you averaged" and never "we suggest", and
`NOT_A_RECOMMENDATION` is carried in the response so a client rendering a
card has the disclaimer without having to write one. A user reading "$400"
as advice when it means "you averaged $400" is the confident-wrong claim
this project keeps having to design against.

## Complete months only, and the difference that makes

The divisor is whole months, from the month of the account's first posting
through the **last month the ledger covers completely**. A ledger whose last
transaction is the 9th does not have a full August in it, and dividing
August's nine days of spending by a whole month reports a rate that is wrong
in the direction that matters -- it under-states, so a user reading it as
advice under-budgets. This is the §5 distinction the earlier phases kept
being bitten by, in its third form: the window a figure *covers*, the month
the ledger's data *stops* in, and the months that are actually complete are
three different facts. The excluded partial month is reported as a warning
rather than dropped quietly.

Months **inside** that window with no spending are counted as zeros and are
not skipped. Within the ledger's span a zero month is real evidence -- you
genuinely spent nothing on that account in March -- and averaging only over
active months would report a per-active-month rate while calling it monthly.
This is the same rule `reports/trends.py` applies to its own window, for the
same reason, and the two are meant to agree.

## Where it declines

Below `MIN_MONTHS` complete months there is no monthly figure, only
`insufficient_history` and a reason -- a first-class abstention in the shape
`trends.py` established, never a shrug rendered as a number. Three is an
editorial minimum with a specific failure in mind: one or two months cannot
distinguish a recurring cost from a single annual insurance premium, and
turning a premium into "you spend $2,000 a month" is worse than saying
nothing. It declines equally when spending in the window is not positive
(refunds meet or exceed spending), because a negative monthly average is not
a description of anything a budget could hold.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from beancount.core.data import Transaction

# `Expenses:Unknown` is unmapped and `verify` errors on it, correctly. It is
# nevertheless not a *mapping* gap and is reported apart from them: the fix
# is to categorize the transactions, not to add a `map` directive. Mapping
# the catch-all to an envelope would silence §5.2's silent-drift guard
# permanently while the spending underneath stayed uncategorized -- turning
# the one check that catches vanishing money into a check that cannot fail.
# After a backfill it is also the largest line by far, and mixed into the
# same list it would bury every real gap.
from bookkeeper.categorize.models import UNKNOWN_ACCOUNTS
from bookkeeper.envelope.directives import (
    DirectiveError,
    find_ambiguous_accounts,
    parse_envelope_directives,
)

# `verify`'s own finder, imported rather than reimplemented: "unmapped" must
# mean exactly what the integrity gate means by it, including its deliberate
# treatment of an ambiguously-mapped account as known-but-not-usable. A
# second definition here would let this report and `verify` disagree about
# which accounts are missing, which is the §5.2 drift itself.
from bookkeeper.envelope.verify import _find_unmapped_expense_accounts
from bookkeeper.reports.budget import ledger_bounds

#: Fewer complete months than this and no monthly figure is reported at all.
MIN_MONTHS = 3

#: Carried in the response so the caveat travels with the number. A client
#: that renders `observed_monthly_mean` without this is rendering advice.
NOT_A_RECOMMENDATION = (
    "These are averages of money already spent over complete recorded months. "
    "They describe the past. They are not recommended allocations -- nothing here "
    "knows what you intend to spend next month."
)

#: Reasons a monthly figure was not produced, as first-class values rather
#: than a null the caller has to interpret.
BASIS_INSUFFICIENT_HISTORY = "insufficient_history"
BASIS_NOT_POSITIVE = "spending_not_positive"
BASIS_OBSERVED = "observed_mean"

_CENTS = Decimal("0.01")


def _month_start(day: date) -> date:
    return day.replace(day=1)


def _month_end(day: date) -> date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _months_between(start: date, end: date) -> int:
    """Inclusive count of whole months from `start`'s month to `end`'s month."""
    return (end.year - start.year) * 12 + (end.month - start.month) + 1


def _last_complete_month_end(last_posted: date) -> date | None:
    """The final day of the last month the ledger covers completely.

    A ledger ending on the last day of its month is complete through that
    month. Otherwise the trailing month is partial and the last complete
    month is the one before it -- which may be nothing at all, if the ledger
    is a single partial month old.
    """
    if last_posted == _month_end(last_posted):
        return last_posted
    first_of_month = _month_start(last_posted)
    if first_of_month.month == 1:
        previous = date(first_of_month.year - 1, 12, 1)
    else:
        previous = date(first_of_month.year, first_of_month.month - 1, 1)
    return _month_end(previous)


@dataclass(frozen=True)
class UnmappedAccount:
    """One expense account with postings and no envelope mapping.

    `total_spend` is everything ever posted to the account; `spend_in_window`
    is only what falls inside the complete months the mean was taken over.
    They are separate fields and never merged, because the difference between
    them is exactly the trailing partial month -- the figure that would
    silently corrupt the rate if it were folded in.

    `observed_monthly_mean` is `spend_in_window / months_observed`, quantized
    to cents, and is `None` whenever `basis` is not `observed_mean`. It is a
    measurement of the past. See the module docstring.
    """

    account: str
    transactions: int
    total_spend: Decimal
    first_posted: date
    last_posted: date
    basis: str
    reason: str
    months_observed: int
    months_required: int
    spend_in_window: Decimal
    observed_monthly_mean: Decimal | None = None
    window_from: date | None = None
    window_to: date | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "transactions": self.transactions,
            "total_spend": str(self.total_spend),
            "first_posted": self.first_posted.isoformat(),
            "last_posted": self.last_posted.isoformat(),
            "basis": self.basis,
            "reason": self.reason,
            "months_observed": self.months_observed,
            "months_required": self.months_required,
            "spend_in_window": str(self.spend_in_window),
            "observed_monthly_mean": (
                None
                if self.observed_monthly_mean is None
                else str(self.observed_monthly_mean)
            ),
            "window_from": None if self.window_from is None else self.window_from.isoformat(),
            "window_to": None if self.window_to is None else self.window_to.isoformat(),
        }


@dataclass
class EnvelopeGaps:
    ok: bool
    accounts: tuple[UnmappedAccount, ...] = ()
    #: `Expenses:Unknown` and friends, kept apart from `accounts` and out of
    #: the totals. See the import comment: a catch-all is a categorization
    #: gap, and mapping it would silence §5.2's guard rather than close it.
    uncategorized: tuple[UnmappedAccount, ...] = ()
    total_unmapped_spend: Decimal = Decimal(0)
    total_unmapped_transactions: int = 0
    currency: str = "USD"
    min_months: int = MIN_MONTHS
    #: The disclaimer, carried in the payload rather than only in `render()`,
    #: so a browser card cannot show the number without it.
    not_a_recommendation: str = NOT_A_RECOMMENDATION
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "accounts": [a.to_dict() for a in self.accounts],
            "uncategorized": [a.to_dict() for a in self.uncategorized],
            "total_unmapped_spend": str(self.total_unmapped_spend),
            "total_unmapped_transactions": self.total_unmapped_transactions,
            "currency": self.currency,
            "min_months": self.min_months,
            "not_a_recommendation": self.not_a_recommendation,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["envelope gaps failed:", *(f"  - {e}" for e in self.errors)])
        lines: list[str] = []
        if not self.accounts:
            lines.append(
                "No envelope gaps: every expense account with postings maps to an "
                "envelope."
            )
        else:
            lines.extend(
                [
                    (
                        f"{len(self.accounts)} expense account(s) have spending but no "
                        f"envelope mapping: {self.total_unmapped_transactions} "
                        f"transaction(s) totalling {self.total_unmapped_spend:.2f} "
                        f"{self.currency} sit outside every budget figure."
                    ),
                    "",
                    self.not_a_recommendation,
                    "",
                ]
            )
            for a in self.accounts:
                lines.append(
                    f"  {a.account}  -- {a.transactions} txn(s), "
                    f"{a.total_spend:.2f} {self.currency} all-time "
                    f"({a.first_posted.isoformat()}..{a.last_posted.isoformat()})"
                )
                if a.observed_monthly_mean is None:
                    lines.append(f"      no monthly figure: {a.reason}")
                else:
                    lines.append(
                        f"      you averaged {a.observed_monthly_mean:.2f} "
                        f"{self.currency}/month over {a.months_observed} complete "
                        f"month(s) ({a.window_from.isoformat()}.."
                        f"{a.window_to.isoformat()})"
                    )
                lines.append(
                    f'      to budget it: custom "envelope" "map" "{a.account}" '
                    '"<envelope>"'
                )
                lines.append("")

        for a in self.uncategorized:
            lines.append("")
            lines.append(
                f"{a.account} also has no mapping, and must not be given one: "
                f"{a.transactions} transaction(s) totalling {a.total_spend:.2f} "
                f"{self.currency} are simply not categorized yet. Mapping the catch-all "
                "to an envelope would silence the unmapped-account check instead of "
                "closing it. Run `bookkeeper review` or `bookkeeper suggest-rules`."
            )

        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines).rstrip("\n")


@dataclass(frozen=True)
class _Activity:
    transactions: int
    total: Decimal
    first: date
    last: date
    in_window: Decimal


def _activity(
    entries: list[Any], accounts: set[str], currency: str, window_end: date | None
) -> dict[str, _Activity]:
    """Per-account totals, span, and the subtotal inside the complete months."""
    counts: dict[str, int] = defaultdict(int)
    totals: dict[str, Decimal] = defaultdict(Decimal)
    windowed: dict[str, Decimal] = defaultdict(Decimal)
    firsts: dict[str, date] = {}
    lasts: dict[str, date] = {}

    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        touched: set[str] = set()
        for posting in entry.postings:
            units = posting.units
            if units is None or units.currency != currency:
                continue
            account = posting.account
            if account not in accounts:
                continue
            totals[account] += units.number
            if window_end is not None and entry.date <= window_end:
                windowed[account] += units.number
            touched.add(account)
        for account in touched:
            counts[account] += 1
            firsts[account] = min(firsts.get(account, entry.date), entry.date)
            lasts[account] = max(lasts.get(account, entry.date), entry.date)

    return {
        account: _Activity(
            transactions=counts[account],
            total=totals[account],
            first=firsts[account],
            last=lasts[account],
            in_window=windowed[account],
        )
        for account in counts
    }


def _assess(
    account: str, activity: _Activity, last_complete: date | None
) -> UnmappedAccount:
    """Build the line for one account, declining the mean where it must."""
    common = {
        "account": account,
        "transactions": activity.transactions,
        "total_spend": activity.total.quantize(_CENTS),
        "first_posted": activity.first,
        "last_posted": activity.last,
        "months_required": MIN_MONTHS,
    }

    window_from = _month_start(activity.first)
    if last_complete is None or last_complete < window_from:
        return UnmappedAccount(
            **common,
            basis=BASIS_INSUFFICIENT_HISTORY,
            months_observed=0,
            spend_in_window=Decimal("0.00"),
            reason=(
                "no month of this account's history is complete in the ledger yet, so "
                f"there is no monthly rate to report (at least {MIN_MONTHS} are needed)"
            ),
        )

    months = _months_between(window_from, last_complete)
    spend_in_window = activity.in_window.quantize(_CENTS)

    if months < MIN_MONTHS:
        return UnmappedAccount(
            **common,
            basis=BASIS_INSUFFICIENT_HISTORY,
            months_observed=months,
            spend_in_window=spend_in_window,
            window_from=window_from,
            window_to=last_complete,
            reason=(
                f"{months} complete month(s) of history; at least {MIN_MONTHS} are "
                "needed before a monthly average can tell a recurring cost from a "
                "one-off"
            ),
        )

    if spend_in_window <= 0:
        return UnmappedAccount(
            **common,
            basis=BASIS_NOT_POSITIVE,
            months_observed=months,
            spend_in_window=spend_in_window,
            window_from=window_from,
            window_to=last_complete,
            reason=(
                "spending over the complete months is not positive (refunds meet or "
                "exceed spending), so there is no monthly amount to describe"
            ),
        )

    return UnmappedAccount(
        **common,
        basis=BASIS_OBSERVED,
        months_observed=months,
        spend_in_window=spend_in_window,
        window_from=window_from,
        window_to=last_complete,
        observed_monthly_mean=(spend_in_window / months).quantize(_CENTS),
        reason=(
            f"mean of {spend_in_window} spent over {months} complete month(s), "
            f"{window_from.isoformat()}..{last_complete.isoformat()}, counting months "
            "with no spending as zero -- a description of the past, not a recommendation"
        ),
    )


def envelope_gaps(
    *,
    entries: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> EnvelopeGaps:
    """Expense accounts with spending and no envelope mapping, sized and dated.

    Read-only, and it fixes nothing: adding a `map` directive is a decision
    about what an account *is*, which is the user's to make. `verify` remains
    the gate that fails; this is the view that makes the list tractable.
    """
    if entries is None:
        from bookkeeper.envelope.compute import load_ledger

        entries, _errors, options = load_ledger()

    currency = str((options or {}).get("operating_currency", ["USD"])[0])

    try:
        parsed = parse_envelope_directives(entries)
    except DirectiveError as exc:
        return EnvelopeGaps(
            ok=False,
            currency=currency,
            errors=[
                (
                    "envelope directives are unusable, so nothing can be called "
                    f"unmapped with confidence: {exc}"
                )
            ],
        )

    ambiguous = find_ambiguous_accounts(parsed.maps)
    known = {m.account for m in parsed.maps} | set(ambiguous)
    unmapped = _find_unmapped_expense_accounts(entries, known)

    warnings: list[str] = []
    if ambiguous:
        warnings.append(
            f"{len(ambiguous)} account(s) map to more than one envelope and are not "
            "listed here; they are a `verify` error of their own, not a gap"
        )

    bounds = ledger_bounds(entries)
    last_complete = None if bounds is None else _last_complete_month_end(bounds[1])
    if bounds is not None and last_complete != _month_end(bounds[1]):
        warnings.append(
            f"the ledger's last transaction is {bounds[1].isoformat()}, so "
            f"{bounds[1].strftime('%Y-%m')} is only partly recorded and is excluded "
            "from every monthly figure below; a part-month divided by a whole month "
            "would under-state the rate"
        )

    activity = _activity(entries, unmapped, currency, last_complete)
    other_currency = sorted(unmapped - set(activity))
    if other_currency:
        # Not silently dropped: an account whose postings are all in another
        # currency is still a `verify` error, and reporting nothing about it
        # would read as "this one is fine".
        warnings.append(
            f"{len(other_currency)} unmapped account(s) have no postings in "
            f"{currency} and carry no figures here ({', '.join(other_currency)}); "
            "multi-currency is a stated non-goal (PLAN.md §8) and they are still a "
            "`verify` error"
        )

    assessed = [_assess(account, activity[account], last_complete) for account in sorted(activity)]
    # Biggest gap first: the account hiding the most spending is the one worth
    # mapping first, and that ordering is money rather than count because the
    # question here is "how much of my budget is missing", not "how much work".
    assessed.sort(key=lambda a: (-a.total_spend, a.account))
    lines = [a for a in assessed if a.account not in UNKNOWN_ACCOUNTS]
    uncategorized = [a for a in assessed if a.account in UNKNOWN_ACCOUNTS]

    return EnvelopeGaps(
        ok=True,
        accounts=tuple(lines),
        uncategorized=tuple(uncategorized),
        total_unmapped_spend=sum((a.total_spend for a in lines), Decimal(0)).quantize(_CENTS),
        total_unmapped_transactions=sum(a.transactions for a in lines),
        currency=currency,
        min_months=MIN_MONTHS,
        warnings=warnings,
    )
