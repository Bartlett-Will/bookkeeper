"""Spend by envelope over time (PLAN.md §5.3, `get_spending_report`).

Phase 4 ships the basic, correct version; §6 Phase 5 deepens the analysis and
the charting on top of it. "Correct" here means three things specifically:

**The envelope rollup is the ledger's, not this module's.** Which accounts
belong to which envelope is decided by the `custom "envelope" "map"`
directives and read through `envelope.directives`, the same code `compute.py`
and `verify.py` use. A second, report-local notion of "which accounts are
groceries" would drift from the budget it is supposed to describe, and the
drift would show up as a chart that disagrees with `/envelopes`.

**Spending outside every envelope is reported, not dropped.** Auto-apply is
off (Phase 3), so today essentially every transaction still posts to
`Expenses:Unknown`, which no `map` directive targets. A report that silently
summed only mapped accounts would currently render as an empty chart and look
like "you spent nothing", which is the exact silent-drift failure §5.2 builds
`verify` to prevent. Unmapped spend gets its own total and its own account
list.

**Empty periods are emitted.** Every series carries a point for every period
in the window, zero where there was no spending, so a chart's x-axis is the
requested range rather than the subset that happened to have activity.

The aggregation itself is beanquery's: grouping is what a query engine is
for, and pushing it down keeps the Python side to a rollup over one row per
(period, account, currency) rather than a scan over every posting.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import beanquery
from beancount.core.data import Transaction

from bookkeeper.envelope.compute import coerce_asof, load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)

#: Period granularities this report understands. `month` is the default
#: because an envelope budget is a monthly instrument.
PERIODS: tuple[str, ...] = ("month", "year")

DEFAULT_PERIOD = "month"

EXPENSE_ACCOUNT_PREFIX = "Expenses:"

_FALLBACK_CURRENCY = "USD"

#: Two constant queries rather than one assembled from the caller's `period`.
#: The grouping columns are the only difference and they are ours, not the
#: caller's -- `period` selects between fixed texts, it never becomes SQL.
_QUERY_BY_MONTH = """
SELECT year AS period_year, month AS period_month, account, currency, sum(number) AS total
FROM #postings
WHERE date >= %(from_date)s AND date <= %(to_date)s
GROUP BY period_year, period_month, account, currency
ORDER BY period_year, period_month, account
"""

_QUERY_BY_YEAR = """
SELECT year AS period_year, account, currency, sum(number) AS total
FROM #postings
WHERE date >= %(from_date)s AND date <= %(to_date)s
GROUP BY period_year, account, currency
ORDER BY period_year, account
"""


@dataclass(frozen=True)
class SpendPoint:
    period: str
    amount: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"period": self.period, "amount": str(self.amount)}


@dataclass(frozen=True)
class EnvelopeSeries:
    """One envelope's spending, one point per period in the requested window."""

    name: str
    total: Decimal
    points: tuple[SpendPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "total": str(self.total),
            "points": [p.to_dict() for p in self.points],
        }


@dataclass
class SpendingReport:
    ok: bool
    from_date: date
    to_date: date
    period: str = DEFAULT_PERIOD
    currency: str = _FALLBACK_CURRENCY
    periods: tuple[str, ...] = ()
    envelopes: tuple[EnvelopeSeries, ...] = ()
    total: Decimal = Decimal(0)
    unmapped_total: Decimal = Decimal(0)
    unmapped_accounts: tuple[str, ...] = ()
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "period": self.period,
            "currency": self.currency,
            "periods": list(self.periods),
            "envelopes": [e.to_dict() for e in self.envelopes],
            "total": str(self.total),
            "unmapped_total": str(self.unmapped_total),
            "unmapped_accounts": list(self.unmapped_accounts),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["spending report failed:", *(f"  - {e}" for e in self.errors)])

        header = (
            f"Spending by envelope, {self.from_date.isoformat()} to "
            f"{self.to_date.isoformat()} (by {self.period}, {self.currency})"
        )
        lines = [header, ""]
        if not self.periods:
            lines.append("no periods in range")
            return "\n".join(lines)

        name_width = max((len(e.name) for e in self.envelopes), default=len("Envelope"))
        name_width = max(name_width, len("Envelope"))
        columns = "".join(f"{p:>12}" for p in self.periods)
        title = f"{'Envelope':<{name_width}}{columns}{'Total':>12}"
        lines.append(title)
        lines.append("-" * len(title))
        for series in self.envelopes:
            cells = "".join(f"{p.amount:>12.2f}" for p in series.points)
            lines.append(f"{series.name:<{name_width}}{cells}{series.total:>12.2f}")
        lines.append("-" * len(title))
        lines.append(f"{'Total':<{name_width}}{'':>{12 * len(self.periods)}}{self.total:>12.2f}")

        if self.unmapped_total:
            lines.append("")
            lines.append(
                f"{self.unmapped_total:.2f} {self.currency} of spending belongs to no envelope:"
            )
            lines.extend(f"  - {a}" for a in self.unmapped_accounts)
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _period_label(day: date, period: str) -> str:
    return f"{day.year:04d}" if period == "year" else f"{day.year:04d}-{day.month:02d}"


def _period_labels(from_date: date, to_date: date, period: str) -> tuple[str, ...]:
    """Every period label between the bounds, inclusive, in order."""
    labels: list[str] = []
    if period == "year":
        labels = [f"{y:04d}" for y in range(from_date.year, to_date.year + 1)]
    else:
        year, month = from_date.year, from_date.month
        while (year, month) <= (to_date.year, to_date.month):
            labels.append(f"{year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(labels)


def _row_label(row: dict[str, Any], period: str) -> str:
    if period == "year":
        return f"{row['period_year']:04d}"
    return f"{row['period_year']:04d}-{row['period_month']:02d}"


def _ledger_bounds(entries: list[Any]) -> tuple[date, date] | None:
    """The first and last transaction dates in the ledger.

    Used as the default window rather than "the last six months of wall-clock
    time" for the same reason `verify` dates its over-allocation check off the
    ledger's own activity: a report of a fixed ledger must not change meaning
    because a day passed, and a demo ledger whose data sits outside the last
    six months would otherwise render as an empty chart.
    """
    dates = [e.date for e in entries if isinstance(e, Transaction)]
    return (min(dates), max(dates)) if dates else None


def spending_report(
    from_date: date | str | None = None,
    to_date: date | str | None = None,
    period: str = DEFAULT_PERIOD,
    *,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> SpendingReport:
    """Spend per envelope per period between `from_date` and `to_date`.

    Both bounds are inclusive and default to the ledger's own first and last
    transaction dates. `entries`/`errors`/`options` let the sidecar feed its
    mtime-cached ledger in instead of reloading it (§5.1).

    Raises `ValueError` on an unparseable date or an unknown `period`; the
    caller decides whether that is a 422 or a CLI error message.
    """
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}, got {period!r}")

    if entries is None:
        entries, errors, options = load_ledger()

    bounds = _ledger_bounds(entries)
    today = coerce_asof(None)
    start = coerce_asof(from_date) if from_date is not None else (bounds[0] if bounds else today)
    end = coerce_asof(to_date) if to_date is not None else (bounds[1] if bounds else today)

    warnings: list[str] = []
    if end < start:
        return SpendingReport(
            ok=False,
            from_date=start,
            to_date=end,
            period=period,
            errors=[f"from ({start.isoformat()}) is after to ({end.isoformat()})"],
        )

    currency = str((options or {}).get("operating_currency", [_FALLBACK_CURRENCY])[0])

    try:
        parsed = parse_envelope_directives(entries)
        account_to_envelope = build_account_map(parsed.maps)
    except DirectiveError as exc:
        return SpendingReport(
            ok=False,
            from_date=start,
            to_date=end,
            period=period,
            currency=currency,
            errors=[f"envelope mapping is unusable: {exc}"],
        )

    connection = beanquery.connect(
        "beancount:", entries=entries, errors=list(errors or []), options=dict(options or {})
    )
    query = _QUERY_BY_YEAR if period == "year" else _QUERY_BY_MONTH
    cursor = connection.execute(query, {"from_date": start, "to_date": end})
    columns = [d.name for d in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    labels = _period_labels(start, end, period)
    by_envelope: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    unmapped: dict[str, Decimal] = defaultdict(Decimal)
    other_currencies: set[str] = set()

    for row in rows:
        account = row["account"]
        envelope = account_to_envelope.get(account)
        if envelope is None and not account.startswith(EXPENSE_ACCOUNT_PREFIX):
            # Funding and equity legs are the other side of the same money;
            # counting them would net every expense back to zero.
            continue
        if row["currency"] != currency:
            other_currencies.add(row["currency"])
            continue
        label = _row_label(row, period)
        if envelope is None:
            unmapped[account] += row["total"]
        else:
            by_envelope[envelope][label] += row["total"]

    # Envelopes with a mapping but no spending in the window still get a row:
    # "we budget for this and spent nothing" is a real, useful reading, and a
    # series that appears and disappears between requests breaks a chart's
    # legend.
    for envelope in set(account_to_envelope.values()):
        by_envelope.setdefault(envelope, defaultdict(Decimal))

    series = tuple(
        sorted(
            (
                EnvelopeSeries(
                    name=name,
                    total=sum(amounts.values(), Decimal(0)),
                    points=tuple(
                        SpendPoint(period=label, amount=amounts.get(label, Decimal(0)))
                        for label in labels
                    ),
                )
                for name, amounts in by_envelope.items()
            ),
            key=lambda s: s.name,
        )
    )

    unmapped_total = sum(unmapped.values(), Decimal(0))
    if unmapped_total:
        warnings.append(
            f"{unmapped_total:.2f} {currency} of spending is not mapped to any envelope "
            "and is excluded from the per-envelope figures; run `bookkeeper verify` for "
            "the accounts involved"
        )
    if other_currencies:
        warnings.append(
            "excluded spending in "
            f"{', '.join(sorted(other_currencies))}: this report is single-currency "
            f"({currency})"
        )

    return SpendingReport(
        ok=True,
        from_date=start,
        to_date=end,
        period=period,
        currency=currency,
        periods=labels,
        envelopes=series,
        total=sum((s.total for s in series), Decimal(0)),
        unmapped_total=unmapped_total,
        unmapped_accounts=tuple(sorted(unmapped)),
        warnings=warnings,
    )
