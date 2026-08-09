"""`bookkeeper verify` — the checks PLAN.md §5.2 requires because a computed
(non-posting) envelope model gets no validation for free from `bean-check`.

1. Unmapped-expense check — hard error, never a silent skip. This is the
   real silent-drift vector: spending against an unmapped `Expenses:*`
   account vanishes from the budget view while the ledger stays valid.
   Also catches an account mapped to more than one envelope.
2. Over-allocation check — `available(asof) >= 0`, i.e.
   `Σ max(balance(E), 0) <= budgeted cash`. Deliberately NOT `Σ allocations
   <= budgeted cash`, which false-positives once any money has been spent
   (see the regression test in tests/test_envelope_verify.py). Equally
   deliberately not `Σ balance(E) <= budgeted cash`: that form let an
   overspent envelope's negative balance credit itself back and silence this
   check outright (PLAN.md §5.2, fixed 2026-07-30).
3. Balance assertions / bean-check — `beancount.loader.load_file` already
   runs the same plugin pipeline `bean-check` does, so its `errors` list
   *is* bean-check's output; we just surface it instead of shelling out.
4. Assertion *coverage* — whether check 3 is actually guarding each
   SimpleFIN-backed account, and how far. Added in Phase 6 alongside
   `bookkeeper reconcile`; see `_assertion_coverage_notes` for why it is
   here and why it is a note.

Overspent envelopes are reported as **notes, not errors**, and this is a
deliberate choice rather than an oversight. `verify` is the integrity gate:
it exits non-zero, it runs on every sync and in CI, and everything it calls
an error is something that makes the books *wrong* or lets spending vanish
silently. Overspending an envelope does neither. It is an ordinary, expected
budgeting event — YNAB's own model is that you cover it from the next
period's allocation — and since the §5.2 fix it is no longer silent either:
it is subtracted from `available`, totalled as `total_overspend`, and marked
on its own row in the rendered report. Failing the build on a normal event
would teach the user to ignore a red `verify`, which costs us the checks
above that genuinely matter. So it is surfaced, loudly, without gating.

(Golden-file snapshot tests, §5.2 point 4, live in the test suite, not here
— they are a property of the test harness, not something a runtime check
can assert about a single ledger load.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from beancount.core.data import Balance, Custom, Transaction
from beancount.parser import printer

from bookkeeper.envelope.compute import compute_envelope_state, load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    find_ambiguous_accounts,
    format_ambiguous_accounts,
    parse_envelope_directives,
)

EXPENSE_ACCOUNT_PREFIX = "Expenses:"

#: The accounts `bookkeeper.ingest` opens and is responsible for asserting a
#: balance on. Check 4 is scoped to these; see `_assertion_coverage_notes`.
SIMPLEFIN_ASSET_PREFIX = "Assets:SimpleFIN:"


@dataclass
class VerifyResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    """Findings worth showing that are not integrity failures — see the module
    docstring. `notes` never affects `ok` or the process exit code; if a note
    ever should fail the build, it belongs in `errors` instead."""

    def render(self) -> str:
        if self.ok:
            lines = ["verify: OK"]
        else:
            lines = [f"verify: FAILED ({len(self.errors)} error(s))"]
            for e in self.errors:
                lines.append(f"  - {e}")
        for n in self.notes:
            lines.append(f"  note: {n}")
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


def _assertion_coverage_notes(entries) -> list[str]:
    """Where the cash-truth guard (§5.2 item 3) is not actually guarding.

    **Why this belongs in `verify` when full reconciliation does not.** §5.2
    item 3's promise is that "if we ever drop or duplicate a transaction,
    `bean-check` fails at the next assertion date". Check 3 above enforces the
    assertions that exist. Nothing enforced that they exist at all — and
    `render_balances_file` rewrites `balances.beancount` wholesale from
    whatever SimpleFIN currently reports, so an account that stops being
    reported loses its assertion and the guard silently stops covering it,
    with a green `verify` throughout. That is the one part of reconciliation
    that needs no statement, so it is the one part that can run unattended on
    every sync. Comparing against an actual statement cannot: it needs an
    input `verify` does not have, which is why `bookkeeper reconcile` is its
    own command.

    Scoped to `Assets:SimpleFIN:` deliberately. Those are the accounts
    `bookkeeper.ingest` opens and is responsible for asserting; a hand-written
    ledger's own asset accounts were never promised an assertion, and flagging
    them would fire on every fixture in the test suite — noise that trains
    people to stop reading this section.

    Notes, not errors, for the reason the module docstring gives about
    overspend: an unasserted account does not make the books *wrong*. It
    means one guard is not armed. Failing the build on it would gate every
    sync on a condition the user cannot always fix (a bank can revoke a
    connection), and a `verify` people learn to ignore costs us the checks
    that genuinely matter.
    """
    posted: dict[str, date] = {}
    for entry in entries:
        if not isinstance(entry, Transaction):
            continue
        for posting in entry.postings:
            account = posting.account
            if not account.startswith(SIMPLEFIN_ASSET_PREFIX) or posting.units is None:
                continue
            posted[account] = max(entry.date, posted.get(account, entry.date))

    asserted: dict[str, date] = {}
    for entry in entries:
        if not isinstance(entry, Balance):
            continue
        if not entry.account.startswith(SIMPLEFIN_ASSET_PREFIX):
            continue
        asserted[entry.account] = max(entry.date, asserted.get(entry.account, entry.date))

    notes: list[str] = []
    for account in sorted(posted):
        latest_assertion = asserted.get(account)
        if latest_assertion is None:
            notes.append(
                f'no balance assertion covers "{account}", which has postings through '
                f"{posted[account].isoformat()}. The §5.2 cash-truth guard is not armed "
                "for this account: a dropped or duplicated transaction here would not "
                "fail bean-check. Run `bookkeeper sync` to regenerate balances.beancount."
            )
            continue
        # A `balance` directive dated D asserts the balance at the *start* of
        # D (see `normalize.NormalizedBalance`), so it covers transactions
        # dated strictly before D. Anything on or after D is unguarded, and
        # comparing the two dates without that offset would report a
        # correctly-covered account as one day short on every sync.
        unguarded = sum(
            1
            for e in entries
            if isinstance(e, Transaction)
            and e.date >= latest_assertion
            and any(p.account == account for p in e.postings)
        )
        if unguarded:
            notes.append(
                f'"{account}" is asserted only through {latest_assertion.isoformat()}; '
                f"{unguarded} later transaction(s) are not covered by any assertion. "
                "That is normal between syncs — it stops being normal if it does not "
                "move after one."
            )
    return notes


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


def verify_entries(entries, bean_errors) -> VerifyResult:
    """The checks themselves, over an already-loaded ledger.

    Split out from `run_verify` for the same reason `compute_envelope_state`
    is split from `envelope_report`: the API holds an mtime-cached ledger and
    must not pay `loader.load_file` again per request (PLAN.md §5.1), while
    the CLI has nothing loaded and wants the one-call wrapper.
    """
    errors: list[str] = []
    notes: list[str] = []

    # Check 3: balance assertions / bean-check.
    errors.extend(_format_bean_errors(bean_errors))

    # Check 4: is check 3 actually covering the SimpleFIN-backed accounts?
    notes.extend(_assertion_coverage_notes(entries))

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
                    f"envelope balances {report.total_envelope_balance:.2f} - "
                    f"overspend {report.total_overspend:.2f})"
                )
            for envelope in report.overspent_envelopes:
                notes.append(
                    f'envelope "{envelope.name}" is overspent by '
                    f"{envelope.overspend:.2f} as of {asof.isoformat()} "
                    f"(allocated {envelope.allocated:.2f}, spent {envelope.spent:.2f}); "
                    "that money has already left the bank and is not available "
                    "to budget — cover it from the next allocation"
                )

    return VerifyResult(ok=not errors, errors=errors, notes=notes)


def run_verify() -> VerifyResult:
    """Load the ledger from disk and run every check over it."""
    entries, bean_errors, _options_map = load_ledger()
    return verify_entries(entries, bean_errors)
