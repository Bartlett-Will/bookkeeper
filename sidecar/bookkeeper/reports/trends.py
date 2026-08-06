"""Spending direction and unusual transactions (PLAN.md §6 Phase 5).

This is the report most able to mislead, so every judgement it makes is
stated as a rule, computed from figures it also returns, and declined when
the data cannot support it.

**The series is `spending_report`'s series.** Direction is computed over the
per-period totals that `/reports/spending` already publishes, so a chart and
its trend line can never disagree, and the envelope rollup stays the
ledger's (§5.2) rather than this module's.

**The window is clamped to the ledger's own first and last transaction.**
A month before any data exists is not a month of zero spending; it is not a
month at all. `spending_report` deliberately emits a zero point for every
period in the requested range -- correct for a chart, whose x-axis should be
the range that was asked for -- but a *statistic* over those zeros is not
merely padded, it is wrong: a rent fixed at 1000.00 reported `up` given an
early `from`, and `down` given a `to` past the end of the ledger, because
five empty months followed by six real ones is a rising line. That is the
exact failure this module is built to prevent, inverted: rather than
declining to judge, it manufactured a *confident* direction out of an
artefact of the window, and "your rent is going up" about a fixed cost is a
claim a user acts on.

Clamping is at **ledger** scope, never per envelope, and the difference
matters. Within the ledger's span a zero month is real evidence -- you did
spend nothing on coffee in January -- and an envelope that starts or stops
mid-ledger is a genuine finding that per-envelope clamping would erase.
Outside the ledger's span there is no evidence either way. Only the second
is dropped, and the narrowing is reported as a warning rather than done
quietly.

It is clamped to whole **months**, not to the transaction dates, and the
distinction is the whole of the trailing edge. A window is what the report
*covers*; the last transaction is where the data *stops*, and they are not
the same claim -- a January whose last transaction is the 28th is still
covered through the 31st, because the ledger continuing afterwards is
evidence that nothing happened rather than evidence that nobody looked.
Since the month is the unit being measured, trimming a window back to the
last transaction inside its final month relabels the window without changing
one figure in it. Measured on a rent fixed at 1000.00 through 2026-06-03:
asking through 2026-06-30 gives `flat` over six periods either way, while
asking through 2026-07-31 invents a seventh, empty month and reports `down`
at -107.14 a month. The first is a label; the second is a phantom period.
Only phantom periods are removed.

Outlier detection is unaffected by this and always was: observations are
drawn from transactions, not from periods, so a range holding no
transactions contributes nothing to a median and cannot shift a score. The
tests assert that invariance directly rather than leaving it to reasoning.

**Direction: the least-squares slope of period totals, in `Decimal`.** With
`n` periods and totals `y_i`, using integer weights `w_i = 2i - (n-1)`:

    slope = 2 * Σ(w_i * y_i) / Σ(w_i²)        [currency per period]

The numerator is exact; the single division is the only inexact step, at the
default 28-digit context, and the result is then quantized to cents. A slope
is called a direction only when it is large relative to the envelope's own
typical spending:

    relative_slope = slope / mean            [mean = Σy / n, > 0]

`|relative_slope| < 0.10` is **flat** -- a line has to move at least a tenth
of a typical period's spend, per period, before "your grocery spending is
going up" is a claim worth making. Above that, the sign gives `up` or
`down`. Both `slope` and `mean` are returned quantized *as used*, so a
reader can recompute the verdict from the response and disagree with it.

**Abstention is a first-class answer, and is not "flat".**
`insufficient_data` with a `reason` is returned when there are fewer than
three periods -- a trend over two points is a line through two points, not a
trend -- or when mean spending is not positive, which makes a relative
comparison undefined. This matches §5.4's tiers, which return `None` rather
than a low-confidence guess, exactly so that coverage and precision stay
separable numbers. `flat` is a positive finding: it means the slope was
measured and is small.

It is a fourth enum value rather than a null `direction` or a `flat` with a
missing delta, because both of those make a client infer abstention from an
absence -- which flattens "we don't know yet" into "steady", and those are
two things a user acts on differently. `periods_observed` and
`periods_required` come back alongside `reason` so the abstention can be
interrogated rather than merely displayed.

**Outliers: the Iglewicz-Hoaglin modified z-score, threshold 3.5.** For each
envelope, each transaction's net posting to that envelope's accounts is one
observation, and

    score = (amount - median) / scale

is flagged when `|score| > 3.5`. `scale` is a *robust* dispersion, not the
standard deviation, and that is the point: a standard deviation is inflated
by the very outlier being tested, so one large charge raises the bar enough
to hide itself. Two scales, and the response says which was used:

- `mad` -- `scale = MAD / 0.6745`, where MAD is the median absolute
  deviation from the median. This is the published form.
- `mean_absolute_deviation` -- `scale = 1.253314 * MeanAD`, the published
  fallback for when MAD is zero. MAD goes to zero whenever more than half
  the observations share one amount, which for a budget is the *normal*
  case, not a rare one: rent is the same number every month. Declining to
  judge those envelopes would decline exactly where a doubled charge
  matters most.

Both `median` and `scale` come back on every flagged transaction, so
`(amount - median) / scale` can be recomputed by hand from the response --
an outlier flag a reader cannot interrogate is worse than none.

**Below five transactions in an envelope, no judgement is made.** The
modified z-score is a rank-based statistic and needs a sample; calling the
second-ever transaction in an envelope unusual is a statement about the
sample size, not about the transaction. Envelopes that were not judged are
returned with the reason, so "no outliers" and "not enough data to look" are
distinguishable.

Everything above is computed in `Decimal` in the sidecar. No statistic is
ever handed to the browser as a float to finish.
"""

from __future__ import annotations

import calendar
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from beancount.core.data import Transaction

from bookkeeper.envelope.compute import coerce_asof, load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)
from bookkeeper.reports.budget import ledger_bounds
from bookkeeper.reports.spending import EXPENSE_ACCOUNT_PREFIX, SpendPoint, spending_report

#: Fewer periods than this and no direction is reported. Three is the
#: smallest number of points that can disagree with a straight line.
MIN_PERIODS = 3

#: `|slope / mean|` below this is flat rather than `up` or `down`.
FLAT_BAND = Decimal("0.10")

#: Fewer transactions than this in an envelope and no outlier judgement is
#: made for it at all.
MIN_TRANSACTIONS = 5

#: Iglewicz-Hoaglin's cutoff for the modified z-score.
OUTLIER_THRESHOLD = Decimal("3.5")

#: Consistency constant relating the MAD to a normal standard deviation.
_MAD_CONSISTENCY = Decimal("0.6745")

#: The same, for the mean absolute deviation (the published MAD=0 fallback).
_MEAN_AD_CONSISTENCY = Decimal("1.253314")

#: Every direction this report can return. `insufficient_data` is abstention
#: and is deliberately a value of its own rather than `flat` or a null.
DIRECTIONS: tuple[str, ...] = ("up", "down", "flat", "insufficient_data")

_CENTS = Decimal("0.01")
_RATIO = Decimal("0.0001")


@dataclass(frozen=True)
class EnvelopeTrend:
    """One envelope's direction, with the figures the verdict was read from.

    `slope` is currency per period and `mean` is currency; `relative_slope`
    is their ratio and is `None` when `mean` is not positive. `reason` always
    says how the verdict was reached, including when it is
    `insufficient_data`.

    `periods_observed` and `periods_required` are carried per line rather
    than only as a report-level constant so a single envelope's card is
    self-contained: "3 of 3" and "1 of 3" are the difference between a
    verdict and an abstention, and a client should not have to reach back up
    to the envelope of the response to render it.
    """

    name: str
    direction: str
    periods_observed: int
    periods_required: int
    total: Decimal
    mean: Decimal
    slope: Decimal
    relative_slope: Decimal | None
    reason: str
    points: tuple[SpendPoint, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "direction": self.direction,
            "periods_observed": self.periods_observed,
            "periods_required": self.periods_required,
            "total": str(self.total),
            "mean": str(self.mean),
            "slope": str(self.slope),
            "relative_slope": (
                None if self.relative_slope is None else str(self.relative_slope)
            ),
            "reason": self.reason,
            "points": [p.to_dict() for p in self.points],
        }


@dataclass(frozen=True)
class Outlier:
    """One transaction that is unusual for its envelope.

    Self-contained on purpose: `median`, `scale` and `scale_method` travel
    with the flag so `(amount - median) / scale` can be recomputed from a
    single card, without the reader having to hold the whole report.
    """

    envelope: str
    posted_date: date
    description: str
    amount: Decimal
    score: Decimal
    median: Decimal
    scale: Decimal
    scale_method: str
    threshold: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope,
            "posted_date": self.posted_date.isoformat(),
            "description": self.description,
            "amount": str(self.amount),
            "score": str(self.score),
            "median": str(self.median),
            "scale": str(self.scale),
            "scale_method": self.scale_method,
            "threshold": str(self.threshold),
        }


@dataclass(frozen=True)
class OutlierAssessment:
    """Whether an envelope was judged for outliers at all, and on what basis.

    Returned for every envelope, judged or not, so that "nothing unusual" is
    distinguishable from "not enough data to look" -- the difference between
    a finding and an absence of one.
    """

    envelope: str
    sample_size: int
    judged: bool
    reason: str
    median: Decimal | None = None
    scale: Decimal | None = None
    scale_method: str = ""
    outliers_found: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope,
            "sample_size": self.sample_size,
            "judged": self.judged,
            "reason": self.reason,
            "median": None if self.median is None else str(self.median),
            "scale": None if self.scale is None else str(self.scale),
            "scale_method": self.scale_method,
            "outliers_found": self.outliers_found,
        }


@dataclass
class TrendsReport:
    ok: bool
    from_date: date
    to_date: date
    currency: str = "USD"
    periods: tuple[str, ...] = ()
    envelopes: tuple[EnvelopeTrend, ...] = ()
    outliers: tuple[Outlier, ...] = ()
    assessments: tuple[OutlierAssessment, ...] = ()
    unmapped_total: Decimal = Decimal(0)
    unmapped_accounts: tuple[str, ...] = ()
    unmapped_transactions: int = 0
    #: The thresholds this run actually applied, echoed so the response
    #: describes its own method rather than requiring the reader to have the
    #: source open. A summary that cannot be checked is not a finding.
    min_periods: int = MIN_PERIODS
    flat_band: Decimal = FLAT_BAND
    min_transactions: int = MIN_TRANSACTIONS
    outlier_threshold: Decimal = OUTLIER_THRESHOLD
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "currency": self.currency,
            "periods": list(self.periods),
            "envelopes": [e.to_dict() for e in self.envelopes],
            "outliers": [o.to_dict() for o in self.outliers],
            "assessments": [a.to_dict() for a in self.assessments],
            "unmapped_total": str(self.unmapped_total),
            "unmapped_accounts": list(self.unmapped_accounts),
            "unmapped_transactions": self.unmapped_transactions,
            "min_periods": self.min_periods,
            "flat_band": str(self.flat_band),
            "min_transactions": self.min_transactions,
            "outlier_threshold": str(self.outlier_threshold),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["trends report failed:", *(f"  - {e}" for e in self.errors)])

        lines = [
            (
                f"Spending trends, {self.from_date.isoformat()} to "
                f"{self.to_date.isoformat()} (by month, {self.currency})"
            ),
            "",
        ]
        name_width = max((len(e.name) for e in self.envelopes), default=len("Envelope"))
        name_width = max(name_width, len("Envelope"))
        header = (
            f"{'Envelope':<{name_width}}  {'Direction':<13}  {'Per month':>12}  "
            f"{'Mean':>12}  Basis"
        )
        lines.append(header)
        lines.append("-" * len(header))
        for e in self.envelopes:
            lines.append(
                f"{e.name:<{name_width}}  {e.direction:<13}  {e.slope:>+12.2f}  "
                f"{e.mean:>12.2f}  {e.reason}"
            )

        lines.append("")
        if self.outliers:
            lines.append(
                f"Unusual transactions (|modified z| > {self.outlier_threshold}, "
                f"{len(self.outliers)} found):"
            )
            for o in self.outliers:
                lines.append(
                    f"  {o.posted_date.isoformat()}  {o.amount:>10.2f} {self.currency}  "
                    f"{o.envelope} -- {o.description}"
                )
                lines.append(
                    f"      score {o.score:+.2f} = ({o.amount:.2f} - median {o.median:.2f})"
                    f" / scale {o.scale:.4f} [{o.scale_method}]"
                )
        else:
            lines.append("No transaction scored beyond the outlier threshold.")

        declined = [a for a in self.assessments if not a.judged]
        if declined:
            lines.append("")
            lines.append("Not judged for outliers:")
            lines.extend(f"  - {a.envelope}: {a.reason}" for a in declined)

        if self.unmapped_total:
            lines.append("")
            lines.append(
                f"{self.unmapped_transactions} transaction(s) totalling "
                f"{self.unmapped_total:.2f} {self.currency} belong to no envelope and are "
                "outside every figure above:"
            )
            lines.extend(f"  - {a}" for a in self.unmapped_accounts)
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _median(values: list[Decimal]) -> Decimal:
    """The median of an already-sortable list. Exact for an odd count."""
    ordered = sorted(values)
    n = len(ordered)
    middle = n // 2
    if n % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _slope(totals: list[Decimal]) -> Decimal:
    """Least-squares slope of `totals` against period index, in `Decimal`.

    Integer weights `w_i = 2i - (n-1)` keep the numerator exact -- they are
    twice the deviation of each index from the mean index, which avoids the
    half-integers a direct `i - (n-1)/2` would introduce. The lone division
    is the only inexact operation.
    """
    n = len(totals)
    weights = [2 * i - (n - 1) for i in range(n)]
    denominator = sum(w * w for w in weights)
    if denominator == 0:
        return Decimal(0)
    numerator = sum((w * y for w, y in zip(weights, totals, strict=True)), Decimal(0))
    return (2 * numerator / denominator).quantize(_CENTS)


def _direction(totals: list[Decimal]) -> tuple[str, Decimal, Decimal, Decimal | None, str]:
    """Classify a series. Returns `(direction, mean, slope, relative, reason)`.

    The returned `mean` and `slope` are the quantized values the verdict was
    actually read from, so the response reproduces its own reasoning.
    """
    n = len(totals)
    if n == 0:
        return ("insufficient_data", Decimal(0), Decimal(0), None, "no periods in the window")

    # Measured even when the verdict is abstention: a `slope` of 0.00 printed
    # beside "undetermined" would read as "we measured it and it was flat",
    # which is the one thing abstention is here to avoid saying.
    mean = (sum(totals, Decimal(0)) / n).quantize(_CENTS)
    slope = _slope(totals)

    if n < MIN_PERIODS:
        return (
            "insufficient_data",
            mean,
            slope,
            None,
            f"{n} period(s) in the window; a direction needs at least {MIN_PERIODS}",
        )

    if all(t == 0 for t in totals):
        return ("flat", mean, slope, None, f"no spending in any of the {n} periods")
    if mean <= 0:
        return (
            "insufficient_data",
            mean,
            slope,
            None,
            (
                "mean spending over the window is not positive (refunds meet or exceed "
                "spending), so a relative trend is undefined"
            ),
        )

    relative = (slope / mean).quantize(_RATIO)
    if abs(relative) < FLAT_BAND:
        return (
            "flat",
            mean,
            slope,
            relative,
            f"|slope/mean| {abs(relative)} is below the {FLAT_BAND} flat band",
        )
    direction = "up" if relative > 0 else "down"
    return (
        direction,
        mean,
        slope,
        relative,
        f"|slope/mean| {abs(relative)} clears the {FLAT_BAND} flat band over {n} periods",
    )


@dataclass(frozen=True)
class _Observation:
    posted_date: date
    description: str
    amount: Decimal


def _observations(
    entries: list[Any],
    account_to_envelope: dict[str, str],
    start: date,
    end: date,
    currency: str,
) -> tuple[dict[str, list[_Observation]], int]:
    """One observation per (transaction, envelope), plus the unmapped count.

    A transaction split across two accounts in the same envelope is *one*
    observation at its net amount, not two: an outlier detector that saw a
    split shop as two half-sized charges would systematically under-report
    large ones. Transactions netting to zero against an envelope (a same-day
    charge and reversal) are dropped -- there is no amount to judge.
    """
    by_envelope: dict[str, list[_Observation]] = defaultdict(list)
    unmapped_transactions = 0

    for entry in entries:
        if not isinstance(entry, Transaction) or not start <= entry.date <= end:
            continue
        per_envelope: dict[str, Decimal] = defaultdict(Decimal)
        has_unmapped = False
        for posting in entry.postings:
            units = posting.units
            if units is None or units.currency != currency:
                continue
            envelope = account_to_envelope.get(posting.account)
            if envelope is not None:
                per_envelope[envelope] += units.number
            elif posting.account.startswith(EXPENSE_ACCOUNT_PREFIX):
                has_unmapped = True
        if has_unmapped:
            unmapped_transactions += 1
        description = entry.narration or entry.payee or ""
        for envelope, amount in per_envelope.items():
            if amount != 0:
                by_envelope[envelope].append(
                    _Observation(
                        posted_date=entry.date, description=description, amount=amount
                    )
                )
    return by_envelope, unmapped_transactions


def _scale(values: list[Decimal], median: Decimal) -> tuple[Decimal | None, str]:
    """The denominator of the modified z-score, and which rule produced it.

    Returns the value such that `score = (x - median) / scale`, quantized so
    that the score printed is the score the reported numbers reproduce. A
    `None` scale means the observations have no dispersion at all -- every
    amount identical -- and no threshold can mean anything.
    """
    deviations = [abs(v - median) for v in values]
    mad = _median(deviations)
    if mad > 0:
        return ((mad / _MAD_CONSISTENCY).quantize(_RATIO), "mad")
    mean_ad = sum(deviations, Decimal(0)) / len(deviations)
    if mean_ad > 0:
        scale = (_MEAN_AD_CONSISTENCY * mean_ad).quantize(_RATIO)
        if scale > 0:
            return (scale, "mean_absolute_deviation")
    return (None, "")


def _judge(
    envelope: str, observations: list[_Observation]
) -> tuple[OutlierAssessment, list[Outlier]]:
    """Score one envelope's transactions, or say why it was not judged."""
    sample_size = len(observations)
    if sample_size < MIN_TRANSACTIONS:
        return (
            OutlierAssessment(
                envelope=envelope,
                sample_size=sample_size,
                judged=False,
                reason=(
                    f"{sample_size} transaction(s) in the window; at least "
                    f"{MIN_TRANSACTIONS} are needed before an amount can be called unusual"
                ),
            ),
            [],
        )

    amounts = [o.amount for o in observations]
    median = _median(amounts)
    scale, method = _scale(amounts, median)
    if scale is None:
        return (
            OutlierAssessment(
                envelope=envelope,
                sample_size=sample_size,
                judged=False,
                reason=(
                    "every transaction in this envelope is the same amount, so there is "
                    "no dispersion against which anything could be unusual"
                ),
                median=median,
            ),
            [],
        )

    found = [
        Outlier(
            envelope=envelope,
            posted_date=o.posted_date,
            description=o.description,
            amount=o.amount,
            score=((o.amount - median) / scale).quantize(_CENTS),
            median=median,
            scale=scale,
            scale_method=method,
            threshold=OUTLIER_THRESHOLD,
        )
        for o in observations
    ]
    flagged = [o for o in found if abs(o.score) > OUTLIER_THRESHOLD]
    return (
        OutlierAssessment(
            envelope=envelope,
            sample_size=sample_size,
            judged=True,
            reason=(
                f"scored {sample_size} transaction(s) against median {median} and "
                f"{method} scale {scale}"
            ),
            median=median,
            scale=scale,
            scale_method=method,
            outliers_found=len(flagged),
        ),
        flagged,
    )


def trends_report(
    from_date: date | str | None = None,
    to_date: date | str | None = None,
    *,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> TrendsReport:
    """Direction per envelope and unusual transactions between the bounds.

    Both bounds are inclusive and default to the ledger's own first and last
    transaction dates. Periods are months and only months: an envelope budget
    is a monthly instrument (§5.2), and offering a granularity would let a
    caller pick the one that produced the answer it wanted.

    Raises `ValueError` on an unparseable date, matching `spending_report`.
    """
    if entries is None:
        entries, errors, options = load_ledger()

    # Coerced here rather than left to `spending_report` so the window can be
    # clamped before the series is built. `coerce_asof` raises on unparseable
    # input, which is the documented behaviour and is preserved.
    requested_start = coerce_asof(from_date) if from_date is not None else None
    requested_end = coerce_asof(to_date) if to_date is not None else None
    bounds = ledger_bounds(entries)

    clamp_warning: str | None = None
    if bounds is not None:
        first, last = bounds
        start = requested_start if requested_start is not None else first
        end = requested_end if requested_end is not None else last
        if end >= start:
            # Clamped to whole *months*, not to the transaction dates, because
            # the month is the unit being measured. See the module docstring:
            # a window ending mid-month covers that whole month, so trimming
            # it back to the last transaction would relabel the window without
            # changing a single figure in it.
            ledger_from = first.replace(day=1)
            ledger_to = last.replace(day=calendar.monthrange(last.year, last.month)[1])
            clamped_start, clamped_end = max(start, ledger_from), min(end, ledger_to)
            if clamped_end < clamped_start:
                # The requested window lies wholly outside the ledger. A
                # well-formed question with an empty answer, not an error --
                # the same reading `search` gives a query that matches
                # nothing.
                return TrendsReport(
                    ok=True,
                    from_date=start,
                    to_date=end,
                    currency=str(
                        (options or {}).get("operating_currency", ["USD"])[0]
                    ),
                    warnings=[
                        (
                            f"the requested window {start.isoformat()} to "
                            f"{end.isoformat()} lies outside the ledger, which runs "
                            f"{first.isoformat()} to {last.isoformat()}; there is "
                            "nothing in it to find a trend in"
                        )
                    ],
                )
            if (clamped_start, clamped_end) != (start, end):
                clamp_warning = (
                    f"window narrowed from {start.isoformat()}..{end.isoformat()} to the "
                    f"ledger's own range {clamped_start.isoformat()}.."
                    f"{clamped_end.isoformat()}: months outside it hold no data and are "
                    "not months of zero spending, and counting them as such invents a "
                    "direction out of the window"
                )
            requested_start, requested_end = clamped_start, clamped_end

    spending = spending_report(
        requested_start, requested_end, "month", entries=entries, errors=errors, options=options
    )
    if not spending.ok:
        return TrendsReport(
            ok=False,
            from_date=spending.from_date,
            to_date=spending.to_date,
            currency=spending.currency,
            errors=list(spending.errors),
        )

    try:
        parsed = parse_envelope_directives(entries)
        account_to_envelope = build_account_map(parsed.maps)
    except DirectiveError as exc:  # pragma: no cover - spending_report fails first
        return TrendsReport(
            ok=False,
            from_date=spending.from_date,
            to_date=spending.to_date,
            currency=spending.currency,
            errors=[f"envelope mapping is unusable: {exc}"],
        )

    trends: list[EnvelopeTrend] = []
    for series in spending.envelopes:
        totals = [p.amount for p in series.points]
        direction, mean, slope, relative, reason = _direction(totals)
        trends.append(
            EnvelopeTrend(
                name=series.name,
                direction=direction,
                periods_observed=len(totals),
                periods_required=MIN_PERIODS,
                total=series.total,
                mean=mean,
                slope=slope,
                relative_slope=relative,
                reason=reason,
                points=series.points,
            )
        )

    observed, unmapped_transactions = _observations(
        entries,
        account_to_envelope,
        spending.from_date,
        spending.to_date,
        spending.currency,
    )

    assessments: list[OutlierAssessment] = []
    outliers: list[Outlier] = []
    for name in sorted({s.name for s in spending.envelopes} | set(observed)):
        assessment, flagged = _judge(name, observed.get(name, []))
        assessments.append(assessment)
        outliers.extend(flagged)

    # Worst first: if anything downstream shows only the top few, the few it
    # shows should be the ones furthest from ordinary.
    outliers.sort(key=lambda o: (-abs(o.score), o.posted_date, o.envelope))

    return TrendsReport(
        ok=True,
        from_date=spending.from_date,
        to_date=spending.to_date,
        currency=spending.currency,
        periods=spending.periods,
        envelopes=tuple(trends),
        outliers=tuple(outliers),
        assessments=tuple(assessments),
        unmapped_total=spending.unmapped_total,
        unmapped_accounts=spending.unmapped_accounts,
        unmapped_transactions=unmapped_transactions,
        # The clamp goes first: it changes which window every figure below
        # describes, so it outranks the per-currency and coverage notes.
        warnings=([clamp_warning] if clamp_warning else []) + list(spending.warnings),
    )
