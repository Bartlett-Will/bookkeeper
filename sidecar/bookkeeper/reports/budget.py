"""Budget vs actual over a window (PLAN.md §5.2, §6 Phase 5).

Four decisions carry this module, each guarding a specific way the report
could be confidently wrong.

**`spent` here is `/envelopes`' `spent`, by construction.** Both window
figures come from differencing two `compute_envelope_state` calls -- one at
`to`, one at the day before `from`. Because that function sums allocations
with `date <= asof` and postings with `date <= asof`, the difference of two
cumulative reports *is* the window's activity, exactly. This is the whole
reason the module reuses the envelope engine rather than re-deriving which
accounts roll up where: a second notion of "what counts as Groceries" would
drift from the budget it is supposed to describe, and §5.2 exists because
that drift is silent (see `reports/spending.py`, which makes the same choice
for the same reason).

**Carry-in is shown, so the arithmetic is checkable.** An envelope's balance
does not start at zero on an arbitrary `from` date. Each line reports
`carried_in` (balance the day before the window) alongside window
`allocated`/`spent`, and the identity

    balance = carried_in + allocated - spent

holds per line by construction. Without `carried_in` on the page, a reader
comparing this report's `remaining` against `/envelopes`' `balance` would
find two different numbers and no way to reconcile them.

**`consumed_ratio` against a zero allocation is `None`, not `0` and not
`1`.** It is undefined, and an envelope with real spending against no
allocation is a *more* interesting state than either plausible-looking
number -- so it gets its own `status` (`unbudgeted`) rather than being
rounded into the ordinary case. Overspend is still computed for it, because
`spent - 0` is genuinely overspent.

Note the deliberate type asymmetry, which is the ratified contract: the
ratio is a **float** because a ratio is not money, while every currency
figure beside it stays `Decimal` and crosses the wire as a string. The
division is still done in `Decimal` and only the finished value is narrowed,
so no money is ever routed through binary floating point on the way.

**Overspend is surfaced, never clipped.** Phase 3 fixed a defect where
overspent envelopes silently inflated available headroom (§5.2's defect
note). The same shape must not come back here: `overspend` is reported per
line and summed, and `remaining` is allowed to go negative rather than being
floored at zero. `overspent` is a server-computed boolean rather than
something a client derives by parsing `overspend` -- a browser cannot do
`Decimal` arithmetic and must not try, which is the same reasoning behind
`EnvelopeBalance.overspent` in the envelope engine.

Unmapped spend gets its own figure for the same reason `spending.py` gives
it one: auto-apply is off (Phase 3), so nearly everything still posts to
`Expenses:Unknown`, which no `map` directive targets. A budget report that
silently omitted it would read as "you spent nothing".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from beancount.core.data import Transaction

from bookkeeper.envelope.compute import coerce_asof, compute_envelope_state, load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)
from bookkeeper.reports.spending import EXPENSE_ACCOUNT_PREFIX

#: What a line's `allocated`/`spent` pair says about it. Four states rather
#: than an `over: bool`, because "spent against no allocation" and "spent
#: more than allocated" are different problems with different fixes, and
#: "budgeted but untouched" is not the same as "not budgeted at all".
STATUSES: tuple[str, ...] = ("within", "over", "unbudgeted", "unused")

_FALLBACK_CURRENCY = "USD"

_CENTS = Decimal("0.01")

#: Four places on the consumed ratio: enough to render "83.33%" exactly,
#: without implying a precision the underlying division does not have.
_RATIO = Decimal("0.0001")


@dataclass(frozen=True)
class BudgetLine:
    """One envelope's allocation and actual spending over the window.

    `consumed_ratio` is a fraction (0.8333 means 83.33% consumed), or `None`
    when `allocated` is zero -- see the module docstring. A ratio is not
    money, so it is a `float`; every currency field beside it is `Decimal`.

    `overspent` is derived from `overspend` rather than stored independently,
    so the two can never disagree -- the discipline `EnvelopeBalance` follows
    for the same pair. Both describe the *window*: an envelope can overspend
    this month's allocation while still being in the black overall, and
    `balance` is where that cumulative reading lives.
    """

    name: str
    allocated: Decimal
    spent: Decimal
    remaining: Decimal
    consumed_ratio: float | None
    status: str
    overspend: Decimal
    carried_in: Decimal
    balance: Decimal

    @property
    def overspent(self) -> bool:
        """True when this window's spending exceeded this window's allocation."""
        return self.overspend > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "allocated": str(self.allocated),
            "spent": str(self.spent),
            "remaining": str(self.remaining),
            "consumed_ratio": self.consumed_ratio,
            "status": self.status,
            "overspent": self.overspent,
            "overspend": str(self.overspend),
            "carried_in": str(self.carried_in),
            "balance": str(self.balance),
        }


@dataclass
class BudgetReport:
    ok: bool
    from_date: date
    to_date: date
    currency: str = _FALLBACK_CURRENCY
    envelopes: tuple[BudgetLine, ...] = ()
    total_allocated: Decimal = Decimal(0)
    total_spent: Decimal = Decimal(0)
    total_remaining: Decimal = Decimal(0)
    total_overspend: Decimal = Decimal(0)
    unmapped_total: Decimal = Decimal(0)
    unmapped_accounts: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "currency": self.currency,
            "envelopes": [e.to_dict() for e in self.envelopes],
            "total_allocated": str(self.total_allocated),
            "total_spent": str(self.total_spent),
            "total_remaining": str(self.total_remaining),
            "total_overspend": str(self.total_overspend),
            "unmapped_total": str(self.unmapped_total),
            "unmapped_accounts": list(self.unmapped_accounts),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["budget report failed:", *(f"  - {e}" for e in self.errors)])

        lines = [
            (
                f"Budget vs actual, {self.from_date.isoformat()} to "
                f"{self.to_date.isoformat()} ({self.currency})"
            ),
            "",
        ]
        name_width = max((len(e.name) for e in self.envelopes), default=len("Envelope"))
        name_width = max(name_width, len("Envelope"))
        header = (
            f"{'Envelope':<{name_width}}  {'Allocated':>12}  {'Spent':>12}  "
            f"{'Remaining':>12}  {'% used':>8}  Status"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for e in self.envelopes:
            # "--" rather than "0.00" or "100.00": the ratio is undefined
            # against a zero allocation, and printing a plausible number would
            # hide exactly the case worth looking at.
            pct = "--" if e.consumed_ratio is None else f"{e.consumed_ratio * 100:.2f}"
            lines.append(
                f"{e.name:<{name_width}}  {e.allocated:>12.2f}  {e.spent:>12.2f}  "
                f"{e.remaining:>12.2f}  {pct:>8}  {e.status}"
            )
        lines.append("-" * len(header))
        lines.append(
            f"{'Total':<{name_width}}  {self.total_allocated:>12.2f}  "
            f"{self.total_spent:>12.2f}  {self.total_remaining:>12.2f}"
        )
        # Always printed, even at zero, for the same reason `compute.render()`
        # always prints its overspend line: a line that appears only sometimes
        # is one readers stop expecting.
        lines.append(f"Overspent (total): {self.total_overspend:.2f} {self.currency}")

        if self.unmapped_total:
            lines.append("")
            lines.append(
                f"{self.unmapped_total:.2f} {self.currency} of spending in this window "
                "belongs to no envelope and no budget line:"
            )
            lines.extend(f"  - {a}" for a in self.unmapped_accounts)
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _ledger_bounds(entries: list[Any]) -> tuple[date, date] | None:
    """The first and last transaction dates in the ledger.

    The default window, for the reason `reports/spending.py` gives: a report
    of a fixed ledger must not change meaning because a day passed, and a
    demo ledger whose data sits outside a wall-clock window would render as
    an empty budget.
    """
    dates = [e.date for e in entries if isinstance(e, Transaction)]
    return (min(dates), max(dates)) if dates else None


def _consumed_ratio(allocated: Decimal, spent: Decimal) -> float | None:
    """`spent / allocated` as a fraction, or `None` when undefined.

    `None` is the honest answer for a zero allocation and the only one that
    cannot be misread: 0 suggests untouched, 1 suggests exhausted, and both
    are claims this data does not support.

    Divided in `Decimal` and narrowed to `float` only at the end. The
    narrowing is safe precisely because this is the one field that is *not*
    money -- it is a display ratio, and nothing downstream reconstructs an
    amount from it.
    """
    if allocated == 0:
        return None
    return float((spent / allocated).quantize(_RATIO))


def _status(allocated: Decimal, spent: Decimal) -> str:
    """Classify a line. `unbudgeted` is checked first, and deliberately.

    Spending against a zero allocation is technically "over", but calling it
    that would file it with envelopes whose budget merely ran short, when the
    fix is different: one needs a bigger number, the other needs a budget at
    all. The `overspend` figure is computed regardless, so nothing is hidden
    by the finer label.
    """
    if allocated == 0:
        return "unbudgeted" if spent != 0 else "unused"
    return "over" if spent > allocated else "within"


def _window_state(entries: list[Any], start: date, end: date) -> dict[str, dict[str, Decimal]]:
    """Per-envelope `allocated`/`spent`/`carried_in`/`balance` over [start, end].

    Both figures are differences of two cumulative `compute_envelope_state`
    reports, which is what makes them provably the same arithmetic
    `/envelopes` performs -- see the module docstring.
    """
    at_end = compute_envelope_state(entries, end)
    if start == date.min:
        # `start - 1 day` would overflow, and there is nothing before the
        # first representable date to carry in anyway.
        prior = {}
    else:
        before = compute_envelope_state(entries, start - timedelta(days=1))
        prior = {e.name: e for e in before.envelopes}

    zero = Decimal(0)
    state: dict[str, dict[str, Decimal]] = {}
    for e in at_end.envelopes:
        was = prior.get(e.name)
        state[e.name] = {
            "allocated": e.allocated - (was.allocated if was else zero),
            "spent": e.spent - (was.spent if was else zero),
            "carried_in": was.balance if was else zero,
            "balance": e.balance,
        }
    return state


def _unmapped_spend(
    entries: list[Any],
    account_to_envelope: dict[str, str],
    start: date,
    end: date,
    currency: str,
) -> tuple[dict[str, Decimal], set[str]]:
    """Expense postings in the window that no `map` directive claims.

    Same rule `reports/spending.py` applies: only accounts under `Expenses:`
    count, because funding and equity legs are the other side of the same
    money and counting them would net every expense back to zero.

    Also returns the non-operating currencies seen on *mapped* accounts, so
    the report can say that its per-envelope figures mix currencies rather
    than quietly presenting a sum of unlike units. `compute_envelope_state`
    sums `units.number` without regard to currency -- that is the ledger's
    own budget arithmetic and this module does not second-guess it, but it
    does declare it.
    """
    unmapped: dict[str, Decimal] = defaultdict(Decimal)
    mixed: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Transaction) or not start <= entry.date <= end:
            continue
        for posting in entry.postings:
            units = posting.units
            if units is None:
                continue
            if posting.account in account_to_envelope:
                if units.currency != currency:
                    mixed.add(units.currency)
                continue
            if posting.account.startswith(EXPENSE_ACCOUNT_PREFIX):
                if units.currency != currency:
                    mixed.add(units.currency)
                    continue
                unmapped[posting.account] += units.number
    return unmapped, mixed


def budget_report(
    from_date: date | str | None = None,
    to_date: date | str | None = None,
    *,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> BudgetReport:
    """Allocated vs actually spent per envelope between `from_date` and `to_date`.

    Both bounds are inclusive and default to the ledger's own first and last
    transaction dates. `entries`/`errors`/`options` let the sidecar feed its
    mtime-cached ledger in instead of reloading it (§5.1).

    Raises `ValueError` on an unparseable date; the caller decides whether
    that is a 422 or a CLI error message, exactly as `spending_report` does.
    """
    if entries is None:
        entries, _errors, options = load_ledger()

    bounds = _ledger_bounds(entries)
    today = coerce_asof(None)
    start = coerce_asof(from_date) if from_date is not None else (bounds[0] if bounds else today)
    end = coerce_asof(to_date) if to_date is not None else (bounds[1] if bounds else today)

    if end < start:
        return BudgetReport(
            ok=False,
            from_date=start,
            to_date=end,
            errors=[f"from ({start.isoformat()}) is after to ({end.isoformat()})"],
        )

    currency = str((options or {}).get("operating_currency", [_FALLBACK_CURRENCY])[0])

    try:
        parsed = parse_envelope_directives(entries)
        account_to_envelope = build_account_map(parsed.maps)
    except DirectiveError as exc:
        return BudgetReport(
            ok=False,
            from_date=start,
            to_date=end,
            currency=currency,
            errors=[f"envelope mapping is unusable: {exc}"],
        )

    state = _window_state(entries, start, end)
    unmapped, mixed_currencies = _unmapped_spend(
        entries, account_to_envelope, start, end, currency
    )

    lines = tuple(
        BudgetLine(
            name=name,
            allocated=figures["allocated"],
            spent=figures["spent"],
            # Not floored at zero: an overspent line must read as overspent,
            # not as "nothing left", which is what clipping made it look like
            # before the §5.2 fix.
            remaining=figures["allocated"] - figures["spent"],
            consumed_ratio=_consumed_ratio(figures["allocated"], figures["spent"]),
            status=_status(figures["allocated"], figures["spent"]),
            overspend=max(figures["spent"] - figures["allocated"], Decimal(0)),
            carried_in=figures["carried_in"],
            balance=figures["balance"],
        )
        for name, figures in sorted(state.items())
    )

    warnings: list[str] = []
    unmapped_total = sum(unmapped.values(), Decimal(0))
    if unmapped_total:
        warnings.append(
            f"{unmapped_total:.2f} {currency} of spending in this window is not mapped to "
            "any envelope and is therefore in no budget line; run `bookkeeper verify` for "
            "the accounts involved"
        )
    if mixed_currencies:
        warnings.append(
            "this ledger has envelope-mapped postings in "
            f"{', '.join(sorted(mixed_currencies))} as well as {currency}; envelope "
            "arithmetic sums them without conversion, so the figures above mix currencies"
        )

    return BudgetReport(
        ok=True,
        from_date=start,
        to_date=end,
        currency=currency,
        envelopes=lines,
        total_allocated=sum((line.allocated for line in lines), Decimal(0)),
        total_spent=sum((line.spent for line in lines), Decimal(0)),
        total_remaining=sum((line.remaining for line in lines), Decimal(0)),
        total_overspend=sum((line.overspend for line in lines), Decimal(0)),
        unmapped_total=unmapped_total,
        unmapped_accounts=tuple(sorted(unmapped)),
        warnings=warnings,
    )
