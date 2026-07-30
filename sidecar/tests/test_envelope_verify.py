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


def test_overspent_envelope_is_a_note_not_a_failure(fixture_root):
    """PLAN.md §5.2 minimal case (cash 100, allocate 50, spend 80).

    available is 20, so nothing is over-allocated and verify passes -- but
    the overspend must be reported rather than absorbed. It is a note, not
    an error, because overspending is an ordinary budgeting event covered
    from the next allocation, and failing the build on it would teach the
    user to ignore a red verify.
    """
    fixture_root("overspent_envelope")
    result = run_verify()
    assert result.ok, result.render()
    assert result.errors == []
    assert len(result.notes) == 1
    assert "Groceries" in result.notes[0]
    assert "overspent by 30.00" in result.notes[0]
    assert "  note: " in result.render()


def test_overspend_no_longer_silences_the_over_allocation_guard(fixture_root):
    """THE regression the original fixtures missed.

    Inflows 200, allocations 200 (Groceries 50 + Dining Out 150), 100 spent
    on Groceries. Cash is 100 but 150 is still committed to Dining Out, so
    available is -50 and this is a real over-allocation.

    Under the old `cash - Sigma balances` formula available came out to
    exactly 0 and verify reported OK: the overspent envelope's -50 credited
    itself back and silenced the one guard meant to catch budgeting money
    you don't have.
    """
    fixture_root("overspend_silences_guard")
    result = run_verify()
    assert not result.ok, result.render()
    assert any("over-allocated" in e for e in result.errors), result.render()
    assert any("-50.00" in e for e in result.errors), result.render()
    # And the cause is named alongside the symptom.
    assert any("Groceries" in n and "overspent by 50.00" in n for n in result.notes)


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
