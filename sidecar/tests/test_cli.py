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
