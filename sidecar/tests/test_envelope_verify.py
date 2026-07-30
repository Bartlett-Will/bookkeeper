"""Tests for bookkeeper.envelope.verify — the four checks PLAN.md §5.2
requires (unmapped-expense, over-allocation, bean-check, and — separately,
in test_envelope_snapshot.py — golden-file snapshots).
"""

from __future__ import annotations

from bookkeeper.envelope.verify import run_verify


def test_basic_fixture_passes_verify(fixture_root):
    fixture_root("basic")
    result = run_verify()
    assert result.ok, result.render()
    assert result.errors == []


def test_unmapped_expense_account_fails_loudly(fixture_root):
    """Exit criterion 2: an unmapped expense account fails verify loudly,
    naming the account."""
    fixture_root("unmapped_account")
    result = run_verify()
    assert not result.ok
    assert any("Expenses:Misc:Other" in e for e in result.errors), result.render()


def test_account_mapped_to_two_envelopes_fails(fixture_root):
    fixture_root("duplicate_mapping")
    result = run_verify()
    assert not result.ok
    assert any("Expenses:Food:Groceries" in e for e in result.errors), result.render()


def test_ambiguous_account_is_not_also_reported_unmapped(fixture_root):
    """An account mapped to two envelopes is ambiguous, not unmapped — it
    must get exactly one diagnosis, not a self-contradictory pair saying
    it's both double-mapped and unmapped."""
    fixture_root("duplicate_mapping")
    result = run_verify()
    assert not result.ok
    assert len(result.errors) == 1, result.render()
    assert "more than one envelope" in result.errors[0]
    assert not any("unmapped expense account" in e for e in result.errors)


def test_true_over_allocation_fails(fixture_root):
    """Exit criterion 3 (direction 1): a real over-allocation fails verify."""
    fixture_root("over_allocation")
    result = run_verify()
    assert not result.ok
    assert any("over-allocated" in e for e in result.errors), result.render()


def test_false_positive_available_does_not_fail(fixture_root):
    """Exit criterion 3 (direction 2) — THE subtle regression case from
    PLAN.md §5.2: opening 500, income 3000, allocations 3400, spending 110.

    cash = 3390, Sigma allocations = 3400 (a naive `allocations <= cash`
    check wrongly fires: 3400 > 3390), but Sigma envelope balances = 3290
    and available = cash - Sigma balances = +100. The budget is fine and
    verify must say so.
    """
    fixture_root("false_positive_available")
    result = run_verify()
    assert result.ok, result.render()
    assert result.errors == []


def test_bean_check_errors_are_surfaced(fixture_root):
    fixture_root("bean_check_failure")
    result = run_verify()
    assert not result.ok
    assert any("Balance failed" in e for e in result.errors), result.render()


def test_render_ok_and_failed_shapes(fixture_root):
    fixture_root("basic")
    ok_result = run_verify()
    assert ok_result.render() == "verify: OK"

    fixture_root("over_allocation")
    failed_result = run_verify()
    rendered = failed_result.render()
    assert rendered.startswith("verify: FAILED")
    assert "over-allocated" in rendered
