"""Pure computation of envelope state (PLAN.md §5.2).

    balance(E, asof) = Σ allocate(E, t≤asof) − Σ postings(accounts mapped to E, t≤asof)
    available(asof)  = Σ budgeted-cash(asof) − Σ max(balance(E, asof), 0) over all E

Note the `max(·, 0)`: an envelope with a *negative* balance has been overspent,
and that money has already left the bank. It is not headroom to re-budget, so
it must not credit back into `available`. The earlier form of this formula
summed raw balances and so reported `available` figures that could exceed total
cash — and, worse, silenced the over-allocation guard in `verify.py` at exactly
the moment an envelope went into the red. See the §5.2 defect note in PLAN.md
for the worked case and `tests/fixtures/overspent_envelope/`.

The clipped-out amount is not discarded, it is reported separately as
`total_overspend`, because the two identities

    Σ max(b, 0) = Σ b + Σ max(−b, 0)
    available   = budgeted_cash − total_envelope_balance − total_overspend

are what make the rendered report add up on the page. Overspend is a real
YNAB-style obligation — it is covered out of the next period's allocation —
so it is surfaced as its own figure rather than folded invisibly into another.

`compute_envelope_state` is the pure core: given a beancount entries list and
an `asof` date, it returns an `EnvelopeReport` with no filesystem access, no
mutation of its inputs, and no hidden state — same inputs, same outputs,
every time. `envelope_report` is the thin, impure wrapper that loads the real
ledger (via `bookkeeper.paths`) and calls into the pure core; that's the only
seam `bookkeeper verify`, the CLI, and tests need to cross.

"budgeted cash" is the balance of every `Assets:*` account as of `asof` —
this is an envelope-budgeting system over cash accounts only (PLAN.md §8:
no investment tracking), so summing postings under the `Assets:` namespace
is exactly "money currently on hand, including opening balances and income,
net of everything already spent".
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from beancount import loader
from beancount.core.data import Directive, Transaction

from bookkeeper import paths
from bookkeeper.envelope.directives import build_account_map, parse_envelope_directives

CASH_ACCOUNT_PREFIX = "Assets:"


@dataclass(frozen=True)
class EnvelopeBalance:
    name: str
    allocated: Decimal
    spent: Decimal
    balance: Decimal

    @property
    def overspent(self) -> bool:
        """True when spending has exceeded this envelope's allocation.

        Derived rather than stored so it can never disagree with `balance`;
        the whole point of the §5.2 fix is that overspend and balance are two
        readings of one number, not two independently-maintained ones.
        """
        return self.balance < 0

    @property
    def overspend(self) -> Decimal:
        """The amount overspent, as a positive figure (0 when not overspent).

        Positive-by-convention so callers can sum it and print it without
        sign-juggling; the direction is already carried by the field name.
        """
        return -self.balance if self.balance < 0 else Decimal(0)


@dataclass(frozen=True)
class EnvelopeReport:
    asof: date
    envelopes: tuple[EnvelopeBalance, ...]
    budgeted_cash: Decimal
    total_envelope_balance: Decimal
    total_overspend: Decimal
    available: Decimal

    @property
    def overspent_envelopes(self) -> tuple[EnvelopeBalance, ...]:
        """Just the envelopes in the red, for callers that want to list them."""
        return tuple(e for e in self.envelopes if e.overspent)

    @property
    def ok(self) -> bool:
        """Not a validation result — `envelopes` never fails on its own.

        `.ok` is provided for symmetry with `VerifyResult` in case a caller
        wants a uniform interface; it is always True here. `bookkeeper verify`
        is what actually judges the numbers.
        """
        return True

    def render(self) -> str:
        lines = [f"Envelope report as of {self.asof.isoformat()}", ""]
        name_width = max((len(e.name) for e in self.envelopes), default=len("Envelope"))
        header = (
            f"{'Envelope':<{name_width}}  {'Allocated':>12}  {'Spent':>12}  {'Balance':>12}"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for e in sorted(self.envelopes, key=lambda e: e.name):
            # The marker is appended only to the rows that need it, so the
            # header and rule widths stay put and an overspent envelope is
            # the only thing that stands out in the column.
            marker = "  OVERSPENT" if e.overspent else ""
            lines.append(
                f"{e.name:<{name_width}}  {e.allocated:>12.2f}  {e.spent:>12.2f}  "
                f"{e.balance:>12.2f}{marker}"
            )
        lines.append("-" * len(header))
        lines.append(f"Budgeted cash:              {self.budgeted_cash:>12.2f}")
        lines.append(f"Envelope balances (total):  {self.total_envelope_balance:>12.2f}")
        # Always printed, even at zero: a line that appears only sometimes is
        # a line readers stop expecting, and the summary block is meant to be
        # read as arithmetic (cash - balances - overspend = available).
        lines.append(f"Overspent (total):          {self.total_overspend:>12.2f}")
        lines.append(f"Available to budget:        {self.available:>12.2f}")
        return "\n".join(lines)


def compute_envelope_state(entries: list[Directive], asof: date) -> EnvelopeReport:
    """Pure function of (entries, asof) — see module docstring."""
    parsed = parse_envelope_directives(entries)
    account_to_envelope = build_account_map(parsed.maps)

    allocated: dict[str, Decimal] = defaultdict(Decimal)
    for a in parsed.allocations:
        if a.date <= asof:
            allocated[a.envelope] += a.amount

    spent: dict[str, Decimal] = defaultdict(Decimal)
    cash = Decimal(0)

    for entry in entries:
        if not isinstance(entry, Transaction) or entry.date > asof:
            continue
        for posting in entry.postings:
            units = posting.units
            if units is None:
                continue
            envelope = account_to_envelope.get(posting.account)
            if envelope is not None:
                spent[envelope] += units.number
            if posting.account.startswith(CASH_ACCOUNT_PREFIX):
                cash += units.number

    envelope_names = set(account_to_envelope.values()) | set(allocated) | set(spent)
    envelopes = tuple(
        sorted(
            (
                EnvelopeBalance(
                    name=name,
                    allocated=allocated.get(name, Decimal(0)),
                    spent=spent.get(name, Decimal(0)),
                    balance=allocated.get(name, Decimal(0)) - spent.get(name, Decimal(0)),
                )
                for name in envelope_names
            ),
            key=lambda e: e.name,
        )
    )
    total_envelope_balance = sum((e.balance for e in envelopes), Decimal(0))
    total_overspend = sum((e.overspend for e in envelopes), Decimal(0))
    # `Σ max(balance, 0)`, written as `Σ balance + Σ overspend` so the number
    # printed on the "Overspent" line is provably the same number that was
    # withheld from `available` (see the module docstring's identities).
    committed = total_envelope_balance + total_overspend
    available = cash - committed

    return EnvelopeReport(
        asof=asof,
        envelopes=envelopes,
        budgeted_cash=cash,
        total_envelope_balance=total_envelope_balance,
        total_overspend=total_overspend,
        available=available,
    )


def load_ledger(ledger_path=None):
    """Load the main ledger (or an explicit path) via beancount's full loader.

    Returns `(entries, errors, options_map)` exactly as `beancount.loader`
    does — `errors` includes everything `bean-check` would report (balance
    assertions, open/close violations, plugin errors), since `load_file`
    already runs the same pipeline `bean-check` does.
    """
    path = ledger_path if ledger_path is not None else paths.main_ledger()
    return loader.load_file(str(path))


def coerce_asof(asof: date | str | None) -> date:
    """Normalize an `asof` argument to a `date`, defaulting to the UTC day.

    Public because the API layer resolves `asof` itself: it feeds
    `compute_envelope_state` from its own mtime-cached ledger rather than
    going through `envelope_report`, and both paths must agree on what a
    missing `asof` means.
    """
    if asof is None:
        # UTC calendar day via an explicit tz-aware instant (ruff DTZ011:
        # `date.today()` is timezone-naive). UTC rather than local time
        # keeps this deterministic regardless of the host's timezone — the
        # sidecar may not run in the user's own timezone.
        return datetime.now(UTC).date()
    if isinstance(asof, date):
        return asof
    return date.fromisoformat(asof)


def envelope_report(asof: date | str | None = None) -> EnvelopeReport:
    """Impure wrapper: load the real ledger, then run the pure computation."""
    report_date = coerce_asof(asof)
    entries, _errors, _options_map = load_ledger()
    return compute_envelope_state(entries, report_date)
