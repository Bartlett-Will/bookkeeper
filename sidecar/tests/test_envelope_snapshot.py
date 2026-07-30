"""Golden-file snapshot tests (PLAN.md §5.2 point 4).

Month-end envelope balances for the `basic` fixture are committed as text
files under tests/fixtures/basic/golden/. Any change to the computation
semantics shows up here as a reviewable diff instead of a number that
quietly shifts. If you change the math on purpose, update the golden file
in the same commit and explain why.
"""

from __future__ import annotations

from datetime import date

from beancount import loader

from bookkeeper.envelope.compute import compute_envelope_state

GOLDEN_DATES = [date(2026, 1, 31), date(2026, 2, 28)]


def _load_basic(root):
    entries, errors, _options_map = loader.load_file(str(root / "ledger" / "main.beancount"))
    assert not errors, errors
    return entries


def test_month_end_snapshots_match_golden_files(fixture_root):
    root = fixture_root("basic")
    entries = _load_basic(root)

    for asof in GOLDEN_DATES:
        report = compute_envelope_state(entries, asof)
        golden_path = root / "golden" / f"{asof.isoformat()}.txt"
        expected = golden_path.read_text()
        actual = report.render() + "\n"
        assert actual == expected, (
            f"envelope computation for {asof} no longer matches "
            f"{golden_path}. If this is an intentional semantics change, "
            "update the golden file and explain why in the commit message."
        )
