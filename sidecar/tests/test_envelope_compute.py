"""Tests for bookkeeper.envelope.compute — the pure envelope-balance math
and the `envelope_report()` wrapper that loads the real/fixture ledger.
"""

from __future__ import annotations

from datetime import UTC, date
from decimal import Decimal

from beancount import loader

from bookkeeper.envelope.compute import compute_envelope_state, envelope_report


def _load(root):
    entries, errors, _options_map = loader.load_file(str(root / "ledger" / "main.beancount"))
    assert not errors, errors
    return entries


def test_allocate_spend_refund_sequence(fixture_root):
    """Exit criterion 1: allocate -> spend -> refund yields correct balances,
    with the refund crediting the envelope back."""
    root = fixture_root("basic")
    entries = _load(root)

    report = compute_envelope_state(entries, date(2026, 1, 31))
    dining = next(e for e in report.envelopes if e.name == "Dining Out")

    # Postings: -40 (real spend), -60 (mischarged), +60 (refund), -30 (real spend).
    # Net spent should be 70.00, not 130.00 -- the refund must cancel the charge
    # it corrects rather than just adding more "spending".
    assert dining.spent == Decimal("70.00")
    assert dining.allocated == Decimal("150.00")
    assert dining.balance == Decimal("80.00")


def test_asof_filters_future_postings(fixture_root):
    root = fixture_root("basic")
    entries = _load(root)

    jan_report = compute_envelope_state(entries, date(2026, 1, 31))
    feb_report = compute_envelope_state(entries, date(2026, 2, 28))

    groceries_jan = next(e for e in jan_report.envelopes if e.name == "Groceries")
    groceries_feb = next(e for e in feb_report.envelopes if e.name == "Groceries")

    # February postings/allocations must not leak into the January snapshot.
    assert groceries_jan.allocated == Decimal("600.00")
    assert groceries_feb.allocated == Decimal("1200.00")
    assert groceries_feb.spent > groceries_jan.spent


def test_multiple_accounts_roll_up_into_one_envelope(fixture_root):
    root = fixture_root("basic")
    entries = _load(root)
    report = compute_envelope_state(entries, date(2026, 1, 31))
    utilities = next(e for e in report.envelopes if e.name == "Utilities")
    # Electric (90.00) + Water (40.00), two accounts, one envelope.
    assert utilities.spent == Decimal("130.00")


def test_available_formula(fixture_root):
    root = fixture_root("basic")
    entries = _load(root)
    report = compute_envelope_state(entries, date(2026, 1, 31))
    # available = cash - Sigma max(balance, 0), equivalently
    #             cash - Sigma balance - Sigma overspend.
    assert report.available == (
        report.budgeted_cash - report.total_envelope_balance - report.total_overspend
    )
    assert report.budgeted_cash == Decimal("3758.00")
    assert report.total_envelope_balance == Decimal("428.00")
    # Utilities is 30.00 in the red in January (allocated 100, spent 130),
    # so 458.00 -- not 428.00 -- is what is actually committed.
    assert report.total_overspend == Decimal("30.00")
    assert report.available == Decimal("3300.00")


def test_overspent_envelope_does_not_credit_back_into_available(fixture_root):
    """PLAN.md §5.2 defect, minimal case: cash 100, allocate 50, spend 80.

    Real cash is 20 and Groceries is 30 in the hole, so at most 20 can be
    budgeted. The old formula reported 50 -- more than the account holds.
    """
    root = fixture_root("overspent_envelope")
    entries = _load(root)
    report = compute_envelope_state(entries, date(2026, 1, 31))

    groceries = next(e for e in report.envelopes if e.name == "Groceries")
    assert groceries.allocated == Decimal("50.00")
    assert groceries.spent == Decimal("80.00")
    assert groceries.balance == Decimal("-30.00")
    assert groceries.overspent
    assert groceries.overspend == Decimal("30.00")

    assert report.budgeted_cash == Decimal("20.00")
    assert report.total_envelope_balance == Decimal("-30.00")
    assert report.total_overspend == Decimal("30.00")
    assert report.available == Decimal("20.00")


def test_available_never_exceeds_budgeted_cash_when_overspent(fixture_root):
    """The property the defect violated, stated directly.

    `available` is money you could still put into an envelope; it can never
    be more than the cash actually on hand.
    """
    root = fixture_root("overspent_envelope")
    entries = _load(root)
    report = compute_envelope_state(entries, date(2026, 1, 31))
    assert report.available <= report.budgeted_cash


def test_overspend_is_zero_when_no_envelope_is_in_the_red(fixture_root):
    """February in the `basic` fixture has every envelope positive, so the
    fix must be a no-op there -- it only ever clips negatives."""
    root = fixture_root("basic")
    entries = _load(root)
    report = compute_envelope_state(entries, date(2026, 2, 28))
    assert report.overspent_envelopes == ()
    assert report.total_overspend == Decimal("0.00")
    assert report.available == report.budgeted_cash - report.total_envelope_balance
    assert report.available == Decimal("3610.00")


def test_purity_same_inputs_same_outputs(fixture_root):
    """compute_envelope_state must be a pure function: calling it twice with
    the same entries and asof gives byte-identical results, and it must not
    mutate the entries list it was given."""
    root = fixture_root("basic")
    entries = _load(root)
    entries_snapshot = list(entries)

    report1 = compute_envelope_state(entries, date(2026, 1, 31))
    report2 = compute_envelope_state(entries, date(2026, 1, 31))

    assert report1 == report2
    assert entries == entries_snapshot  # no mutation of the input


def test_envelope_report_defaults_asof_to_today(fixture_root, monkeypatch):
    fixture_root("basic")
    # Freeze "now" (UTC) inside the compute module so the test is deterministic.
    from datetime import datetime as real_datetime

    import bookkeeper.envelope.compute as compute_mod

    class _FixedDatetime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz is UTC
            return real_datetime(2026, 2, 15, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(compute_mod, "datetime", _FixedDatetime)
    report = envelope_report(asof=None)
    assert report.asof == date(2026, 2, 15)


def test_envelope_report_accepts_iso_string(fixture_root):
    fixture_root("basic")
    report = envelope_report(asof="2026-01-31")
    assert report.asof == date(2026, 1, 31)
    groceries = next(e for e in report.envelopes if e.name == "Groceries")
    assert groceries.balance == Decimal("325.00")


def test_render_contains_key_figures(fixture_root):
    fixture_root("basic")
    report = envelope_report(asof="2026-01-31")
    text = report.render()
    assert "Groceries" in text
    assert "Available to budget" in text
    assert "3300.00" in text


def test_render_marks_overspent_envelopes(fixture_root):
    """Overspend must be visible on the page, not just subtracted silently."""
    fixture_root("overspent_envelope")
    report = envelope_report(asof="2026-01-31")
    text = report.render()
    lines = text.splitlines()

    groceries_line = next(line for line in lines if line.startswith("Groceries"))
    assert groceries_line.endswith("OVERSPENT")
    assert "Overspent (total):" in text
    # The summary block must read as arithmetic: 20.00 - (-30.00) - 30.00.
    assert any(line.startswith("Overspent (total):") and "30.00" in line for line in lines)
    assert any(line.startswith("Available to budget:") and "20.00" in line for line in lines)


def test_render_shows_overspent_total_even_at_zero(fixture_root):
    """A line that appears only sometimes is a line readers stop expecting."""
    fixture_root("basic")
    report = envelope_report(asof="2026-02-28")
    lines = report.render().splitlines()
    assert any(line.startswith("Overspent (total):") and "0.00" in line for line in lines)
    assert not any(line.endswith("OVERSPENT") for line in lines)
