"""`bookkeeper verify` — the checks PLAN.md §5.2 requires because a computed
(non-posting) envelope model gets no validation for free from `bean-check`.

1. Unmapped-expense check — hard error, never a silent skip. This is the
   real silent-drift vector: spending against an unmapped `Expenses:*`
   account vanishes from the budget view while the ledger stays valid.
   Also catches an account mapped to more than one envelope.
2. Over-allocation check — `available(asof) >= 0`, i.e.
   `Σ balance(E) <= budgeted cash`. Deliberately NOT `Σ allocations <=
   budgeted cash`, which false-positives once any money has been spent
   (see the regression test in tests/test_envelope_verify.py).
3. Balance assertions / bean-check — `beancount.loader.load_file` already
   runs the same plugin pipeline `bean-check` does, so its `errors` list
   *is* bean-check's output; we just surface it instead of shelling out.

(Golden-file snapshot tests, §5.2 point 4, live in the test suite, not here
— they are a property of the test harness, not something a runtime check
can assert about a single ledger load.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from beancount.core.data import Custom, Transaction
from beancount.parser import printer

from bookkeeper.envelope.compute import compute_envelope_state, load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    find_ambiguous_accounts,
    format_ambiguous_accounts,
    parse_envelope_directives,
)

EXPENSE_ACCOUNT_PREFIX = "Expenses:"


@dataclass
class VerifyResult:
    ok: bool
    errors: list[str] = field(default_factory=list)

    def render(self) -> str:
        if self.ok:
            return "verify: OK"
        lines = [f"verify: FAILED ({len(self.errors)} error(s))"]
        for e in self.errors:
            lines.append(f"  - {e}")
        return "\n".join(lines)


def _format_bean_errors(errors) -> list[str]:
    return [printer.format_error(e).rstrip() for e in errors]


def _find_unmapped_expense_accounts(entries, known_accounts: set[str]) -> set[str]:
    """Expense accounts with postings that aren't in `known_accounts`.

    `known_accounts` is "has at least one `map` directive naming it", not
    "resolves to exactly one envelope" — an ambiguously-mapped account is
    known (and already reported as ambiguous by the caller), just not
    usably mapped. Conflating the two would report the same account as
    both double-mapped and unmapped, which is self-contradictory.
    """
    unmapped: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        for posting in entry.postings:
            account = posting.account
            if account.startswith(EXPENSE_ACCOUNT_PREFIX) and account not in known_accounts:
                unmapped.add(account)
    return unmapped


def _latest_activity_date(entries) -> date | None:
    """Latest date among transactions and envelope directives.

    Used instead of `date.today()` so the over-allocation check reflects the
    ledger's own recorded activity rather than wall-clock time — the same
    ledger loaded a year from now must still report the same verify result.
    """
    dates = [
        e.date
        for e in entries
        if isinstance(e, Transaction) or (isinstance(e, Custom) and e.type == "envelope")
    ]
    return max(dates) if dates else None


def run_verify() -> VerifyResult:
    entries, bean_errors, _options_map = load_ledger()
    errors: list[str] = []

    # Check 3: balance assertions / bean-check.
    errors.extend(_format_bean_errors(bean_errors))

    # Checks 1: unmapped accounts, and the "mapped to >1 envelope" case.
    try:
        parsed = parse_envelope_directives(entries)
    except DirectiveError as exc:
        errors.append(str(exc))
        account_to_envelope: dict[str, str] = {}
        known_accounts: set[str] = set()
        directives_ok = False
    else:
        ambiguous = find_ambiguous_accounts(parsed.maps)
        if ambiguous:
            errors.append(
                "account(s) mapped to more than one envelope: "
                f"{format_ambiguous_accounts(ambiguous)}"
            )
        account_to_envelope = {
            m.account: m.envelope for m in parsed.maps if m.account not in ambiguous
        }
        # An ambiguously-mapped account is "known" (and already reported
        # above) even though it has no single resolved envelope, so it must
        # not also be flagged unmapped below.
        known_accounts = set(account_to_envelope) | set(ambiguous)
        directives_ok = not ambiguous

    unmapped = _find_unmapped_expense_accounts(entries, known_accounts)
    for account in sorted(unmapped):
        errors.append(
            f'unmapped expense account "{account}": it has postings but no '
            '`custom "envelope" "map"` directive targets it, so its spending '
            "does not count against any envelope"
        )

    # Check 2: over-allocation, evaluated as of the ledger's own latest activity.
    # Skipped only if the directives themselves were malformed/ambiguous above
    # (that error is reported on its own, and computing balances from a
    # mapping we know is wrong would just add a confusing, derived error) —
    # unrelated failures like bean-check errors or unmapped accounts don't
    # block this check, since the balance math is still well-defined.
    if directives_ok:
        asof = _latest_activity_date(entries)
        if asof is not None:
            report = compute_envelope_state(entries, asof)
            if report.available < 0:
                errors.append(
                    f"over-allocated as of {asof.isoformat()}: available = "
                    f"{report.available:.2f} (budgeted cash {report.budgeted_cash:.2f} - "
                    f"envelope balances {report.total_envelope_balance:.2f})"
                )

    return VerifyResult(ok=not errors, errors=errors)
