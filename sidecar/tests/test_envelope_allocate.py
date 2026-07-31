"""Unit tests for `allocate_to_envelope`.

This is the only *write* tool the chat layer exposes, and an 8B model picks
its arguments (§3.3), so the module treats every one of them as untrusted.
Each guard below exists because the alternative is a silent, wrong change to
someone's budget:

- an invented envelope name would create a real envelope holding real money
  that no account maps into, discoverable only by reading the budget file;
- a negative amount read as a de-allocation would drain a budget silently;
- a directive that does not parse would break every other endpoint,
  `/verify` included -- the one you would reach for to diagnose it.

Every test runs against a throwaway copy of a committed fixture under
`BOOKKEEPER_ROOT`; the real `ledger/` is never written to. The commit tests
create a real git repo in that copy, because `commit_ledger` treats a missing
repo as a warning and carries on -- against a repo-less fixture, "makes no
commit" would pass without a commit ever having been possible.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from bookkeeper.envelope.allocate import allocate_to_envelope, render_allocate_directive
from bookkeeper.envelope.compute import load_ledger
from bookkeeper.envelope.directives import parse_envelope_directives

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def ledger_root(tmp_path, monkeypatch):
    """A throwaway copy of a committed fixture tree.

    `allocate_to_envelope` appends to `budget.beancount`. Pointing it at the
    checked-in fixture would leave the repo dirty and make every other test's
    result depend on whether this one had run.
    """

    def _use(name: str = "basic") -> Path:
        root = tmp_path / name
        shutil.copytree(FIXTURES_DIR / name, root)
        monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
        return root

    return _use


def _git(args: list[str], cwd: Path) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return out.stdout


def _git_init(root: Path) -> None:
    """A real repo, so auto-commit is exercised rather than skipped.

    Identity is set locally because CI and fresh machines have no global git
    identity, and `commit_ledger` would report the failure as a warning
    rather than raising -- which would look like a pass.
    """
    _git(["init", "-q"], root)
    _git(["config", "user.email", "harness@example.invalid"], root)
    _git(["config", "user.name", "Allocate harness"], root)
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "fixture"], root)


def _commit_count(root: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], root).strip())


def _budget(root: Path) -> str:
    return (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")


# --- the round trip -------------------------------------------------------


def test_a_rendered_directive_parses_back_as_the_same_allocation():
    """This module's output must be the directives module's input.

    Rendering is string formatting, so nothing but a test enforces that what
    it writes is what `envelope.directives` can read. If these drift, money
    lands in a file and then vanishes from every report that reads it.
    """
    from beancount import loader

    line = render_allocate_directive("Groceries", Decimal("125.50"), "USD", date(2026, 3, 1))
    assert line == '2026-03-01 custom "envelope" "allocate" "Groceries" 125.50 USD'

    entries, errors, _options = loader.load_string(line)
    assert not errors
    parsed = parse_envelope_directives(entries)
    assert len(parsed.allocations) == 1
    allocation = parsed.allocations[0]
    assert allocation.envelope == "Groceries"
    assert allocation.amount == Decimal("125.50")
    assert allocation.currency == "USD"
    assert allocation.date == date(2026, 3, 1)


def test_an_allocation_is_visible_to_the_ledger_that_reads_it_back(ledger_root):
    root = ledger_root()
    result = allocate_to_envelope("Groceries", "125.50", allocated_on="2026-03-01", commit=False)
    assert result.ok, result.errors

    entries, errors, _options = load_ledger()
    assert not errors
    allocations = parse_envelope_directives(entries).allocations
    assert any(
        a.envelope == "Groceries" and a.amount == Decimal("125.50") and a.date == date(2026, 3, 1)
        for a in allocations
    ), [a for a in allocations if a.envelope == "Groceries"]
    assert '"Groceries" 125.50 USD' in _budget(root)


# --- committing -----------------------------------------------------------


def test_commit_false_writes_the_ledger_but_makes_no_commit(ledger_root):
    """Regression: `commit_ledger` used to run unconditionally.

    The ternary only chose whether to *report* the result, so a caller asking
    not to commit got one anyway and never knew. The assertion that matters
    is the commit count, not `result.commit` -- the old code returned
    `commit=None` here too while still having committed.
    """
    root = ledger_root()
    _git_init(root)
    before = _commit_count(root)

    result = allocate_to_envelope("Groceries", "50.00", commit=False)

    assert result.ok, result.errors
    assert result.commit is None
    assert _commit_count(root) == before, "commit=False still created a git commit"
    assert '"Groceries" 50.00 USD' in _budget(root)


def test_commit_true_makes_exactly_one_commit_naming_the_allocation(ledger_root):
    root = ledger_root()
    _git_init(root)
    before = _commit_count(root)

    result = allocate_to_envelope("Groceries", "50.00")

    assert result.ok, result.errors
    assert result.commit is not None
    assert result.commit.committed is True
    assert result.commit.sha
    assert result.commit.message == "Allocate 50.00 USD to Groceries"
    assert _commit_count(root) - before == 1


def test_a_refused_allocation_never_commits(ledger_root):
    root = ledger_root()
    _git_init(root)
    before = _commit_count(root)

    assert allocate_to_envelope("Groccerys", "50.00").ok is False
    assert _commit_count(root) == before


def test_a_missing_repo_is_a_warning_not_a_failed_allocation(ledger_root):
    """The ledger is usable without version control. Losing the undo system
    is worth saying out loud, but it is not a reason to fail a write that
    already succeeded."""
    root = ledger_root()
    result = allocate_to_envelope("Groceries", "50.00")

    assert result.ok, result.errors
    assert result.commit is not None
    assert result.commit.ok is True
    assert result.commit.committed is False
    assert '"Groceries" 50.00 USD' in _budget(root)


# --- the envelope must already exist --------------------------------------


def test_an_unknown_envelope_is_refused_and_the_real_names_are_returned(ledger_root):
    """A model that invents "Groccerys" would otherwise create a real
    envelope holding real money that no account maps into."""
    root = ledger_root()
    before = _budget(root)

    result = allocate_to_envelope("Groccerys", "50.00")

    assert result.ok is False
    assert "is not an envelope in this ledger" in result.errors[0]
    assert result.known_envelopes == (
        "Dining Out",
        "Groceries",
        "Rent",
        "Transport",
        "Utilities",
    )
    assert _budget(root) == before


@pytest.mark.parametrize("envelope", ["", "   ", "groceries", "GROCERIES"])
def test_envelope_names_are_matched_exactly(ledger_root, envelope):
    """Case-insensitive matching would be a kindness that guesses. An
    envelope is a ledger identifier, and the caller is told the real list."""
    root = ledger_root()
    before = _budget(root)

    assert allocate_to_envelope(envelope, "50.00").ok is False
    assert _budget(root) == before


def test_an_unusable_envelope_mapping_refuses_rather_than_adding_to_it(ledger_root):
    """One account mapped to two envelopes is ambiguous.

    Allocating into a budget whose own rollup is undefined would produce a
    number no report could reproduce, so the write is refused and `verify` is
    left to explain the mapping.
    """
    ledger_root("duplicate_mapping")
    result = allocate_to_envelope("Groceries", "50.00")

    assert result.ok is False
    assert "envelope mapping is unusable" in result.errors[0]
    assert result.known_envelopes == ()


# --- the amount must be positive and well-formed --------------------------


@pytest.mark.parametrize("amount", ["-600.00", "-0.01", "0", "0.00"])
def test_a_non_positive_amount_is_refused(ledger_root, amount):
    """Negative is refused rather than treated as a de-allocation.

    "Take 600 back out of Groceries" is a plausible thing for a model to emit
    while summarizing, and the cost of honouring it is a budget that silently
    drains. Correcting a mistaken allocation is `git revert` (§9).
    """
    root = ledger_root()
    before = _budget(root)

    result = allocate_to_envelope("Groceries", amount)

    assert result.ok is False
    assert "amount must be positive" in result.errors[0]
    assert "revert" in result.errors[0]
    assert _budget(root) == before


@pytest.mark.parametrize("amount", ["abc", "", "12.00 USD", "1,000"])
def test_an_unparseable_amount_is_refused(ledger_root, amount):
    root = ledger_root()
    before = _budget(root)

    result = allocate_to_envelope("Groceries", amount)

    assert result.ok is False
    assert "is not a number" in result.errors[0]
    assert _budget(root) == before


@pytest.mark.parametrize("amount", [float("inf"), float("nan"), Decimal("Infinity")])
def test_a_non_finite_amount_is_refused(ledger_root, amount):
    """`Decimal("NaN")` is a perfectly constructible Decimal and would render
    into the ledger as the literal text `NaN`."""
    root = ledger_root()
    before = _budget(root)

    result = allocate_to_envelope("Groceries", amount)

    assert result.ok is False
    assert _budget(root) == before


def test_a_float_amount_is_not_rounded_through_binary(ledger_root):
    """`Decimal(0.1)` is 0.1000000000000000055511151231257827.

    The module goes through `str()` first, which is the difference between
    recording a dime and recording a rounding artifact.
    """
    root = ledger_root()
    result = allocate_to_envelope("Groceries", 0.1, commit=False)

    assert result.ok, result.errors
    assert result.amount == Decimal("0.10")
    assert '"Groceries" 0.10 USD' in _budget(root)


def test_sub_cent_precision_is_rounded_and_said_out_loud(ledger_root):
    """Rounding money silently is how a cent goes missing and nobody can say
    when."""
    root = ledger_root()
    result = allocate_to_envelope("Groceries", "10.019", commit=False)

    assert result.ok, result.errors
    assert result.amount == Decimal("10.02")
    assert any("rounded" in w for w in result.warnings), result.warnings
    assert '"Groceries" 10.02 USD' in _budget(root)


@pytest.mark.parametrize(
    "currency", ["usd", "US$", "TOOLONGACURRENCYNAMEINDEEDXX", "1USD", "   "]
)
def test_an_invalid_currency_is_refused_before_it_reaches_the_file(ledger_root, currency):
    """Caught here rather than discovered at parse time, so a bad currency is
    a reason rather than a corrupted file that has to be rolled back."""
    root = ledger_root()
    before = _budget(root)

    result = allocate_to_envelope("Groceries", "50.00", currency=currency)

    assert result.ok is False
    assert _budget(root) == before


def test_an_empty_currency_falls_back_to_the_default(ledger_root):
    """Empty is "didn't say", not "said something invalid", so it gets USD.

    Note the boundary, pinned deliberately: `""` defaults, but `"   "` is
    *refused* by the case above. `_validate_currency` treats the empty string
    as falsy and substitutes the default before stripping, so whitespace
    survives to the regex and fails it. That asymmetry is unlikely to matter
    -- both arrive only from a caller that meant to omit the field -- but it
    is real, and a test that quietly accepted either would hide it.
    """
    root = ledger_root()
    result = allocate_to_envelope("Groceries", "50.00", currency="", commit=False)

    assert result.ok, result.errors
    assert result.currency == "USD"
    assert '"Groceries" 50.00 USD' in _budget(root)


def test_a_non_default_currency_is_accepted(ledger_root):
    root = ledger_root()
    result = allocate_to_envelope("Groceries", "50.00", currency="EUR", commit=False)

    assert result.ok, result.errors
    assert result.currency == "EUR"
    assert '"Groceries" 50.00 EUR' in _budget(root)


# --- dates ----------------------------------------------------------------


def test_the_allocation_date_defaults_to_today(ledger_root):
    ledger_root()
    result = allocate_to_envelope("Groceries", "50.00", commit=False)

    assert result.ok, result.errors
    assert result.allocated_on is not None
    assert result.directive.startswith(result.allocated_on.isoformat())


def test_an_explicit_date_is_honoured(ledger_root):
    root = ledger_root()
    result = allocate_to_envelope(
        "Groceries", "50.00", allocated_on="2026-02-15", commit=False
    )

    assert result.allocated_on == date(2026, 2, 15)
    assert '2026-02-15 custom "envelope" "allocate" "Groceries" 50.00 USD' in _budget(root)


def test_an_unparseable_date_is_refused(ledger_root):
    root = ledger_root()
    before = _budget(root)

    result = allocate_to_envelope("Groceries", "50.00", allocated_on="last tuesday")

    assert result.ok is False
    assert _budget(root) == before


# --- the file must still parse afterwards ---------------------------------


#: A directive that beancount reports as an *error*, not merely one it
#: dislikes. Worth stating explicitly: a line of pure garbage
#: (`!!! nonsense !!!`) loads with **zero** errors -- beancount skips text it
#: cannot recognise as a directive -- so using one here would make the
#: rollback tests below pass while never triggering a rollback.
BREAKS_THE_LEDGER = "2026-01-02 balance Assets:NeverOpened 5.00 USD\n"


def test_a_write_that_would_not_parse_is_rolled_back_byte_for_byte(ledger_root):
    """The ledger is the only authority on whether the write was well-formed.

    A half-valid `budget.beancount` would break every other endpoint,
    `/verify` included. The pre-write entries are passed in so the module
    compares against the error count from *before* the corruption, which is
    the same seam the sidecar uses to avoid reloading.
    """
    root = ledger_root()
    entries, errors, _options = load_ledger()
    assert not errors

    budget_path = root / "ledger" / "budget.beancount"
    corrupted = budget_path.read_text(encoding="utf-8") + BREAKS_THE_LEDGER
    budget_path.write_text(corrupted, encoding="utf-8")

    result = allocate_to_envelope("Groceries", "50.00", entries=entries, errors=errors)

    assert result.ok is False
    assert "rolled back" in result.errors[0]
    # Byte-for-byte, including the corruption this test introduced: the
    # module restores what it found, it does not try to repair the file.
    assert budget_path.read_text(encoding="utf-8") == corrupted
    assert '"Groceries" 50.00 USD' not in budget_path.read_text(encoding="utf-8")


def test_an_already_failing_ledger_does_not_get_the_allocation_rolled_back(ledger_root):
    """Comparing against the error count from *before* the write means a
    ledger that was already failing does not lose an allocation to a problem
    it did not cause."""
    root = ledger_root()
    budget_path = root / "ledger" / "budget.beancount"
    budget_path.write_text(
        budget_path.read_text(encoding="utf-8") + BREAKS_THE_LEDGER, encoding="utf-8"
    )
    _entries, errors_before, _options = load_ledger()
    assert errors_before, "this test needs a ledger that is already failing"

    result = allocate_to_envelope("Groceries", "50.00", commit=False)

    assert result.ok, result.errors
    assert '"Groceries" 50.00 USD' in _budget(root)


# --- over-allocation is reported, never prevented -------------------------


def test_over_allocating_succeeds_and_says_so_loudly(ledger_root):
    """Refusing outright would be the wrong trade: the numbers move when a
    sync lands and the user may be mid-rebalance. §5.2's `verify` is what
    judges a budget. Silently creating an over-allocated budget and saying
    nothing is the only unacceptable option."""
    root = ledger_root()
    result = allocate_to_envelope("Groceries", "1000000.00", commit=False)

    assert result.ok is True
    assert result.over_allocated is True
    assert result.available is not None
    assert result.available < 0
    assert any("available to budget is now" in w for w in result.warnings), result.warnings
    assert any("verify" in w for w in result.warnings)
    assert '"Groceries" 1000000.00 USD' in _budget(root)


def test_a_normal_allocation_is_not_flagged_as_over_allocated(ledger_root):
    ledger_root()
    result = allocate_to_envelope("Groceries", "10.00", commit=False)

    assert result.ok, result.errors
    assert result.over_allocated is False
    assert result.available is not None
    assert result.available >= 0
    assert result.warnings == ()


# --- rendering ------------------------------------------------------------


def test_render_reports_the_write_and_the_over_allocation(ledger_root):
    ledger_root()
    text = allocate_to_envelope("Groceries", "1000000.00", commit=False).render()

    assert "allocated 1000000.00 USD to Groceries" in text
    assert "available to budget:" in text
    assert "OVER-ALLOCATED" in text


def test_render_lists_the_known_envelopes_on_a_refusal(ledger_root):
    ledger_root()
    text = allocate_to_envelope("Groccerys", "50.00").render()

    assert text.startswith("allocate failed:")
    assert "known envelopes: Dining Out, Groceries, Rent, Transport, Utilities" in text


def test_to_dict_keeps_money_as_strings(ledger_root):
    """These cross to a browser. JSON numbers are doubles."""
    ledger_root()
    payload = allocate_to_envelope("Groceries", "50.00", commit=False).to_dict()

    assert payload["amount"] == "50.00"
    assert isinstance(payload["available"], str)
    assert payload["ok"] is True
    assert payload["allocated_on"] is not None
