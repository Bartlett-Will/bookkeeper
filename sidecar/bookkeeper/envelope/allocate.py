"""Move money into an envelope (PLAN.md §5.3, `allocate_to_envelope`).

This is the **only write tool** the chat layer exposes, which sets the bar for
everything below. An 8B model picks the arguments (§3.3), so every one of them
is treated as untrusted, and the write is arranged so that a bad call is
either refused before it lands or trivially undone after:

- **The envelope must already exist.** Allocations are only accepted for
  envelopes some `custom "envelope" "map"` directive already names. A model
  that invents "Groccerys" would otherwise create a real envelope holding real
  money that no account maps into, and the typo would be discoverable only by
  reading the budget file.
- **The amount must be positive and well-formed.** Negative amounts are
  refused rather than treated as a de-allocation: "take 600 back out of
  Groceries" is a plausible thing for a model to emit while summarizing, and
  the cost of getting it wrong is a budget that silently drains. Correcting a
  mistaken allocation is `git revert`, which §9 makes the undo system for
  exactly this reason.
- **The file must still parse afterwards.** The appended directive is verified
  by reloading the whole ledger, and the file is restored byte-for-byte if the
  reload reports errors the ledger did not already have. A half-valid
  `budget.beancount` would break every other endpoint, including `/verify`,
  which is what you would reach for to diagnose it.
- **The write is committed.** Via `categorize.gitcommit.commit_ledger`, the
  same committer every other ledger write uses -- one auditable set of
  committable paths, not two.

**Over-allocation is reported, not prevented.** Allocating past `available` is
precisely what `verify`'s over-allocation check exists to catch (§5.2), so the
result carries `available` recomputed after the write and an `over_allocated`
flag. Refusing the write outright would be the wrong trade: the numbers move
when a sync lands, the user may be mid-way through a rebalance, and the
condition is already loudly reported by `verify`. Silently creating an
over-allocated budget and saying nothing is the only unacceptable option.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, DecimalException
from pathlib import Path
from typing import Any

from bookkeeper import paths
from bookkeeper.categorize.gitcommit import CommitResult, commit_ledger
from bookkeeper.envelope.compute import coerce_asof, compute_envelope_state, load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)

#: beancount's own currency shape. Enforced here rather than discovered at
#: parse time so a bad currency is a 422 with a reason, not a corrupted file
#: that gets rolled back.
_CURRENCY_RE = re.compile(r"^[A-Z][A-Z0-9'._-]{0,22}[A-Z0-9]$")

#: Allocations are recorded to the cent, matching how every other amount in
#: the ledger is rendered.
_CENTS = Decimal("0.01")

DEFAULT_CURRENCY = "USD"


@dataclass(frozen=True)
class AllocateResult:
    ok: bool
    envelope: str = ""
    amount: Decimal = Decimal(0)
    currency: str = DEFAULT_CURRENCY
    allocated_on: date | None = None
    directive: str = ""
    path: str = ""
    available: Decimal | None = None
    over_allocated: bool = False
    known_envelopes: tuple[str, ...] = ()
    commit: CommitResult | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "envelope": self.envelope,
            "amount": str(self.amount),
            "currency": self.currency,
            "allocated_on": self.allocated_on.isoformat() if self.allocated_on else None,
            "directive": self.directive,
            "path": self.path,
            "available": None if self.available is None else str(self.available),
            "over_allocated": self.over_allocated,
            "known_envelopes": list(self.known_envelopes),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            lines = ["allocate failed:", *(f"  - {e}" for e in self.errors)]
            if self.known_envelopes:
                lines.append(f"  known envelopes: {', '.join(self.known_envelopes)}")
            return "\n".join(lines)

        lines = [
            f"allocated {self.amount:.2f} {self.currency} to {self.envelope}",
            f"  {self.path}: {self.directive}",
        ]
        if self.available is not None:
            lines.append(f"  available to budget: {self.available:.2f} {self.currency}")
        if self.over_allocated:
            lines.append(
                "  OVER-ALLOCATED: envelopes now hold more than the budgeted cash on hand; "
                "`bookkeeper verify` will fail until this is unwound"
            )
        if self.commit is not None:
            lines.append(self.commit.render())
        if self.warnings:
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _coerce_amount(amount: Decimal | str | float) -> tuple[Decimal, list[str]]:
    """`amount` as a positive, two-decimal `Decimal`, or a `ValueError`.

    `str()` before `Decimal()` even for floats: `Decimal(0.1)` is
    0.1000000000000000055511151231257827, and rounding that into a ledger is
    how a cent goes missing.
    """
    try:
        value = Decimal(str(amount))
    except (DecimalException, ValueError) as exc:
        raise ValueError(f"amount {amount!r} is not a number") from exc
    if not value.is_finite():
        raise ValueError(f"amount {amount!r} is not a finite number")
    if value <= 0:
        raise ValueError(
            f"amount must be positive, got {value}; to undo an allocation, revert its commit"
        )

    quantized = value.quantize(_CENTS)
    warnings = []
    if quantized != value:
        warnings.append(f"amount {value} rounded to {quantized} ({DEFAULT_CURRENCY} cents)")
    return quantized, warnings


def _validate_currency(currency: str) -> str:
    normalized = (currency or DEFAULT_CURRENCY).strip()
    if not _CURRENCY_RE.match(normalized):
        raise ValueError(f"{normalized!r} is not a valid beancount currency")
    return normalized


def render_allocate_directive(
    envelope: str, amount: Decimal, currency: str, allocated_on: date
) -> str:
    """The exact ledger line for one allocation.

    Public so a test can assert the rendered text against what
    `envelope.directives` parses back, which is the property that actually
    matters: this module's output must be that module's input.
    """
    return (
        f'{allocated_on.isoformat()} custom "envelope" "allocate" '
        f'"{envelope}" {amount:.2f} {currency}'
    )


def _append_directive(budget_path: Path, line: str) -> str:
    """Append `line`, returning the file's previous contents for rollback.

    A missing `budget.beancount` is created: `main.beancount` includes it
    unconditionally, so a ledger without one does not load at all, and the
    reload check below would catch that anyway.
    """
    original = budget_path.read_text(encoding="utf-8") if budget_path.exists() else ""
    separator = "" if not original or original.endswith("\n") else "\n"
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    budget_path.write_text(f"{original}{separator}{line}\n", encoding="utf-8")
    return original


def allocate_to_envelope(
    envelope: str,
    amount: Decimal | str | float,
    *,
    currency: str = DEFAULT_CURRENCY,
    allocated_on: date | str | None = None,
    commit: bool = True,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
) -> AllocateResult:
    """Append an `allocate` directive for `envelope` and commit it.

    `entries`/`errors` let the sidecar pass its mtime-cached ledger in for the
    *pre*-write checks (§5.1); the post-write validation always reloads from
    disk, because the whole question it answers is what the file on disk now
    says.
    """
    if entries is None:
        entries, errors, _options = load_ledger()
    errors_before = len(errors or [])

    try:
        parsed = parse_envelope_directives(entries)
        account_to_envelope = build_account_map(parsed.maps)
    except DirectiveError as exc:
        return AllocateResult(
            ok=False,
            envelope=envelope,
            errors=(f"envelope mapping is unusable, refusing to add to it: {exc}",),
        )

    known = tuple(sorted(set(account_to_envelope.values())))
    name = (envelope or "").strip()
    if name not in known:
        return AllocateResult(
            ok=False,
            envelope=name,
            known_envelopes=known,
            errors=(
                (
                    f"{name!r} is not an envelope in this ledger; add a "
                    '`custom "envelope" "map"` directive for it before allocating to it'
                ),
            ),
        )

    try:
        resolved_currency = _validate_currency(currency)
        resolved_amount, warnings = _coerce_amount(amount)
        day = coerce_asof(allocated_on)
    except ValueError as exc:
        return AllocateResult(ok=False, envelope=name, known_envelopes=known, errors=(str(exc),))

    line = render_allocate_directive(name, resolved_amount, resolved_currency, day)
    budget_path = paths.budget_ledger()
    original = _append_directive(budget_path, line)

    # The ledger is the only authority on whether the write was well-formed.
    # Comparing against the error count from *before* the write means a ledger
    # that was already failing its balance assertions does not get this
    # allocation rolled back for a problem it did not cause.
    after_entries, after_errors, _options = load_ledger()
    if len(after_errors) > errors_before:
        budget_path.write_text(original, encoding="utf-8")
        new_errors = after_errors[errors_before:]
        return AllocateResult(
            ok=False,
            envelope=name,
            amount=resolved_amount,
            currency=resolved_currency,
            allocated_on=day,
            directive=line,
            path=str(budget_path),
            known_envelopes=known,
            errors=(
                f"the allocation would not parse and was rolled back: {new_errors[0]}",
            ),
        )

    # Dated off the allocation itself, not today: an allocation backdated into
    # a closed month must be judged against the cash that existed then, which
    # is the same date convention `compute_envelope_state` uses everywhere.
    report = compute_envelope_state(after_entries, max(day, _latest_date(after_entries, day)))
    over_allocated = report.available < 0
    if over_allocated:
        warnings.append(
            f"available to budget is now {report.available:.2f} {resolved_currency}: envelopes "
            "hold more than the cash on hand, and `bookkeeper verify` will report this as an "
            "error until it is unwound"
        )

    # Guarded, not merely reported-or-not. This previously called
    # `commit_ledger` unconditionally and then chose whether to *show* the
    # result, so `commit=False` still wrote a git commit and simply hid it --
    # a caller asking not to commit got one anyway, silently.
    commit_result = (
        commit_ledger(f"Allocate {resolved_amount:.2f} {resolved_currency} to {name}")
        if commit
        else None
    )
    return AllocateResult(
        ok=True,
        envelope=name,
        amount=resolved_amount,
        currency=resolved_currency,
        allocated_on=day,
        directive=line,
        path=str(budget_path),
        available=report.available,
        over_allocated=over_allocated,
        known_envelopes=known,
        commit=commit_result,
        warnings=tuple(warnings),
    )


def _latest_date(entries: list[Any], fallback: date) -> date:
    """The ledger's latest dated entry, so the guard sees all recorded cash."""
    dates = [e.date for e in entries if getattr(e, "date", None) is not None]
    return max(dates) if dates else fallback
