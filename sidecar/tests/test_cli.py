"""CLI parity for the Phase 4 operations (PLAN.md §9).

Every operation the chat can invoke must also run headless. That is a
design requirement, not a convenience: a capability reachable only through a
model is one nobody can script, diff, or run when Ollama is down. These
tests assert the three new subcommands exist, dispatch to the right module,
and set an exit code that means something -- the `.ok` / `.render()`
contract the dispatcher in `cli.py` is built around.

What is *not* asserted here is the content of the reports; that belongs to
each module's own tests. The subject is the dispatch layer.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from bookkeeper.cli import main

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def read_only_fixture(monkeypatch):
    """Point BOOKKEEPER_ROOT at a committed fixture. For commands that read."""

    def _use(name: str) -> Path:
        root = FIXTURES_DIR / name
        monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
        return root

    return _use


@pytest.fixture
def writable_fixture(tmp_path, monkeypatch):
    """A throwaway copy of a committed fixture, for commands that write.

    `allocate` appends to `budget.beancount`. Running it against the checked-in
    fixture would leave the repo dirty and make every other test's result
    depend on whether this one had run yet.
    """

    def _use(name: str) -> Path:
        root = tmp_path / name
        shutil.copytree(FIXTURES_DIR / name, root)
        monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
        return root

    return _use


# --- search ---------------------------------------------------------------


def test_search_finds_transactions_and_exits_zero(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["search", "groceries"]) == 0
    out = capsys.readouterr().out
    assert "5 transaction(s) match 'groceries'" in out
    assert "Expenses:Food:Groceries" in out


def test_search_with_no_matches_is_still_a_success(read_only_fixture, capsys):
    """Nothing found is an answer, not a failure. An exit code of 1 here
    would make `bookkeeper search` unusable in a shell pipeline."""
    read_only_fixture("basic")
    assert main(["search", "kayak rental"]) == 0
    assert "no transactions match" in capsys.readouterr().out


def test_search_honours_limit(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["search", "groceries", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "showing 2" in out
    assert "limited to 2" in out


# --- report ---------------------------------------------------------------


def test_report_renders_spending_by_envelope(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["report", "--from", "2026-01-01", "--to", "2026-02-28"]) == 0
    out = capsys.readouterr().out
    assert "Spending by envelope, 2026-01-01 to 2026-02-28" in out
    assert "Groceries" in out


def test_report_defaults_to_the_ledgers_own_range(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["report"]) == 0
    assert "2026-01-01 to 2026-02-14" in capsys.readouterr().out


def test_report_by_year(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["report", "--period", "year"]) == 0
    assert "(by year, USD)" in capsys.readouterr().out


def test_report_rejects_an_unknown_period_without_a_traceback(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["report", "--period", "decade"]) == 1
    assert "period must be one of" in capsys.readouterr().out


def test_report_rejects_an_unparseable_date_without_a_traceback(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["report", "--from", "last tuesday"]) == 1
    assert "report failed" in capsys.readouterr().out


# --- allocate -------------------------------------------------------------


def test_allocate_appends_a_directive_and_exits_zero(writable_fixture, capsys):
    root = writable_fixture("basic")
    assert main(["allocate", "Groceries", "125.50", "--on", "2026-03-01"]) == 0

    out = capsys.readouterr().out
    assert "allocated 125.50 USD to Groceries" in out
    budget = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")
    assert '2026-03-01 custom "envelope" "allocate" "Groceries" 125.50 USD' in budget


def test_allocate_keeps_the_amount_exact(writable_fixture):
    """The amount stays a string until `allocate_to_envelope` converts it.

    `argparse(type=float)` would round the money before the module that
    cares about cents ever saw it, and 0.1 as a double is not 0.1.
    """
    root = writable_fixture("basic")
    assert main(["allocate", "Groceries", "0.10", "--on", "2026-03-01"]) == 0
    budget = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")
    assert '"Groceries" 0.10 USD' in budget


def test_allocate_refuses_an_unknown_envelope_and_names_the_real_ones(writable_fixture, capsys):
    root = writable_fixture("basic")
    before = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")

    assert main(["allocate", "Groccerys", "50.00"]) == 1
    out = capsys.readouterr().out
    assert "is not an envelope in this ledger" in out
    assert "known envelopes:" in out
    assert "Groceries" in out
    assert (root / "ledger" / "budget.beancount").read_text(encoding="utf-8") == before


def test_allocate_refuses_a_negative_amount(writable_fixture, capsys):
    """Negative is refused, not read as a de-allocation: a budget that
    silently drains is worse than a rejected command. `git revert` is the
    undo (§9)."""
    root = writable_fixture("basic")
    before = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")

    assert main(["allocate", "Groceries", "-600.00"]) == 1
    assert "amount must be positive" in capsys.readouterr().out
    assert (root / "ledger" / "budget.beancount").read_text(encoding="utf-8") == before


# --- budget ---------------------------------------------------------------


def test_budget_renders_allocations_against_actual_spending(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["budget", "--from", "2026-01-01", "--to", "2026-02-28"]) == 0
    out = capsys.readouterr().out
    assert "Budget vs actual, 2026-01-01 to 2026-02-28 (USD)" in out
    assert "Groceries" in out
    assert "% used" in out


def test_budget_defaults_to_the_ledgers_own_range(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["budget"]) == 0
    assert "2026-01-01 to 2026-02-14" in capsys.readouterr().out


def test_budget_always_states_the_overspend_total(read_only_fixture, capsys):
    """Even at zero. A line that appears only sometimes is one readers stop
    expecting, and overspend is the figure §5.2 exists to keep visible."""
    read_only_fixture("basic")
    assert main(["budget"]) == 0
    assert "Overspent (total): 0.00 USD" in capsys.readouterr().out


def test_budget_rejects_an_unparseable_date_without_a_traceback(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["budget", "--from", "last tuesday"]) == 1
    assert "budget report failed" in capsys.readouterr().out


def test_budget_exits_one_on_a_backwards_window(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["budget", "--from", "2026-03-01", "--to", "2026-01-01"]) == 1
    assert "is after" in capsys.readouterr().out


# --- trends ---------------------------------------------------------------


def test_trends_renders_directions_and_what_it_declined_to_judge(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["trends"]) == 0
    out = capsys.readouterr().out
    assert "Spending trends, 2026-01-01 to 2026-02-14 (by month, USD)" in out
    # The fixture spans two months, so every direction is an abstention with
    # a stated reason rather than a confident "flat".
    assert "insufficient_data" in out
    assert "a direction needs at least 3" in out
    assert "Not judged for outliers:" in out


def test_trends_shows_the_arithmetic_behind_a_flag(read_only_fixture, capsys):
    """An outlier a reader cannot interrogate is worse than none, so the
    median, the scale and the rule that produced them are on the page."""
    read_only_fixture("basic")
    assert main(["trends"]) == 0
    out = capsys.readouterr().out
    assert "Unusual transactions (|modified z| > 3.5" in out
    assert "Refund - overcharged dinner" in out
    assert "median" in out and "scale" in out and "[mad]" in out


def test_trends_rejects_an_unparseable_date_without_a_traceback(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["trends", "--to", "next year"]) == 1
    assert "trends report failed" in capsys.readouterr().out


def test_trends_exits_one_on_a_backwards_window(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["trends", "--from", "2026-03-01", "--to", "2026-01-01"]) == 1
    assert "is after" in capsys.readouterr().out


# --- month-end ------------------------------------------------------------


def test_month_end_renders_the_composite_report(read_only_fixture, capsys):
    read_only_fixture("basic")
    assert main(["month-end", "--month", "2026-01"]) == 0
    out = capsys.readouterr().out
    assert "Month-end report — January 2026" in out
    assert "January 2026, complete" in out
    # Budget vs actual, the envelope table, and the cash summary all present.
    assert "Allocated" in out and "Remaining" in out
    assert "Available to budget" in out


def test_month_end_defaults_to_the_ledgers_last_month(read_only_fixture, capsys):
    """Not the wall-clock month: a fixed ledger must not start reporting an
    empty month because a day passed."""
    read_only_fixture("basic")
    assert main(["month-end"]) == 0
    assert "Month-end report — February 2026" in capsys.readouterr().out


def test_month_end_names_an_uncategorized_month_rather_than_showing_zeros(
    read_only_fixture, capsys
):
    """The failure this report is most likely to commit: every envelope figure
    is legitimately zero and the page looks complete."""
    read_only_fixture("unmapped_account")
    assert main(["month-end", "--month", "2026-01"]) == 0
    out = capsys.readouterr().out
    assert "PARTIALLY CATEGORIZED" in out
    assert "Expenses:Misc:Other" in out


def test_month_end_rejects_an_unparseable_month_without_a_traceback(
    read_only_fixture, capsys
):
    read_only_fixture("basic")
    assert main(["month-end", "--month", "january"]) == 1
    assert "month-end report failed" in capsys.readouterr().out


def test_search_shows_a_total_over_the_matches(read_only_fixture, capsys):
    """PLAN.md §9: the total the chat can see must be reachable headless."""
    read_only_fixture("basic")
    assert main(["search", "groceries"]) == 0
    out = capsys.readouterr().out
    assert "Totals over all 5 matching transaction(s)" in out
    assert "spent            365.00" in out
    assert "net spend        365.00" in out
