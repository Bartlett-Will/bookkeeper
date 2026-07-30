"""Tests for bookkeeper.envelope.directives — parsing the raw `custom
"envelope" ...` entries into typed MapDirective/AllocateDirective objects.
"""

from __future__ import annotations

import tempfile
import textwrap
from datetime import date
from decimal import Decimal

import pytest
from beancount import loader

from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)


def _load(src: str):
    with tempfile.NamedTemporaryFile(suffix=".beancount", mode="w", delete=False) as f:
        f.write(textwrap.dedent(src))
        path = f.name
    entries, errors, _options_map = loader.load_file(path)
    assert not errors, errors
    return entries


def test_parses_map_and_allocate():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 open Expenses:Food:Groceries
        2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
        2026-01-01 custom "envelope" "allocate" "Groceries" 600.00 USD
        """
    )
    parsed = parse_envelope_directives(entries)

    assert len(parsed.maps) == 1
    assert parsed.maps[0].account == "Expenses:Food:Groceries"
    assert parsed.maps[0].envelope == "Groceries"
    assert parsed.maps[0].date == date(2026, 1, 1)

    assert len(parsed.allocations) == 1
    assert parsed.allocations[0].envelope == "Groceries"
    assert parsed.allocations[0].amount == Decimal("600.00")
    assert parsed.allocations[0].currency == "USD"


def test_amount_arrives_as_decimal_not_string():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "allocate" "Groceries" 12.34 USD
        """
    )
    parsed = parse_envelope_directives(entries)
    amount = parsed.allocations[0].amount
    assert isinstance(amount, Decimal)
    assert amount == Decimal("12.34")


def test_ignores_non_envelope_custom_directives():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "budget" "something" "else"
        """
    )
    parsed = parse_envelope_directives(entries)
    assert parsed.maps == ()
    assert parsed.allocations == ()


def test_malformed_map_directive_raises():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"
        """
    )
    with pytest.raises(DirectiveError):
        parse_envelope_directives(entries)


def test_allocate_with_string_instead_of_amount_raises():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "allocate" "Groceries" "not-an-amount"
        """
    )
    with pytest.raises(DirectiveError):
        parse_envelope_directives(entries)


def test_unknown_directive_kind_raises():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "delete" "Groceries"
        """
    )
    with pytest.raises(DirectiveError):
        parse_envelope_directives(entries)


def test_build_account_map_collapses_maps():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
        2026-01-01 custom "envelope" "map" "Expenses:Food:Dining" "Dining Out"
        """
    )
    parsed = parse_envelope_directives(entries)
    account_map = build_account_map(parsed.maps)
    assert account_map == {
        "Expenses:Food:Groceries": "Groceries",
        "Expenses:Food:Dining": "Dining Out",
    }


def test_build_account_map_raises_on_ambiguous_account():
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
        2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Food"
        """
    )
    parsed = parse_envelope_directives(entries)
    with pytest.raises(DirectiveError, match="Expenses:Food:Groceries"):
        build_account_map(parsed.maps)


def test_build_account_map_allows_duplicate_identical_mapping():
    """Mapping the same account to the same envelope twice is redundant but
    not ambiguous, and should not raise."""
    entries = _load(
        """
        option "operating_currency" "USD"
        2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
        2026-02-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
        """
    )
    parsed = parse_envelope_directives(entries)
    account_map = build_account_map(parsed.maps)
    assert account_map == {"Expenses:Food:Groceries": "Groceries"}
