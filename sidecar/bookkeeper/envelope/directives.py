"""Parsing for the `custom "envelope" ...` directives (PLAN.md §5.2).

Two shapes appear in the ledger, both as beancount `Custom` entries with
`entry.type == "envelope"`:

    2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries" "Groceries"
    2026-01-01 custom "envelope" "allocate" "Groceries" 600.00 USD

`entry.values` is a list of `ValueType(value, dtype)` pairs (beancount 3.2.3);
we only care about `.value`. This module does no filesystem or ledger-loading
work — it is a pure transform over an already-loaded entries list, which is
what keeps `envelope.compute` and `envelope.verify` testable without I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from beancount.core.amount import Amount
from beancount.core.data import Custom, Directive

ENVELOPE_CUSTOM_TYPE = "envelope"


class DirectiveError(ValueError):
    """A malformed or ambiguous `custom "envelope"` directive."""


@dataclass(frozen=True)
class MapDirective:
    date: date
    account: str
    envelope: str


@dataclass(frozen=True)
class AllocateDirective:
    date: date
    envelope: str
    amount: Decimal
    currency: str


@dataclass(frozen=True)
class ParsedDirectives:
    maps: tuple[MapDirective, ...]
    allocations: tuple[AllocateDirective, ...]


def _location(entry: Custom) -> str:
    meta = entry.meta or {}
    return f'{meta.get("filename", "<unknown>")}:{meta.get("lineno", "?")}'


def parse_envelope_directives(entries: list[Directive]) -> ParsedDirectives:
    """Extract and validate every `custom "envelope" ...` directive.

    Raises `DirectiveError` on a directive that doesn't match either known
    shape — we never silently drop a malformed directive, since that is
    exactly the kind of silent drift PLAN.md §5.2 warns about.
    """
    maps: list[MapDirective] = []
    allocations: list[AllocateDirective] = []

    for entry in entries:
        if not isinstance(entry, Custom) or entry.type != ENVELOPE_CUSTOM_TYPE:
            continue

        values = [v.value for v in entry.values]
        where = _location(entry)

        if not values:
            raise DirectiveError(f"{where}: empty `custom \"envelope\"` directive")

        kind = values[0]
        if kind == "map":
            if (
                len(values) != 3
                or not isinstance(values[1], str)
                or not isinstance(values[2], str)
            ):
                raise DirectiveError(
                    f'{where}: `custom "envelope" "map"` expects '
                    '(account: str, envelope: str), got '
                    f"{values[1:]!r}"
                )
            maps.append(MapDirective(date=entry.date, account=values[1], envelope=values[2]))

        elif kind == "allocate":
            if (
                len(values) != 3
                or not isinstance(values[1], str)
                or not isinstance(values[2], Amount)
            ):
                raise DirectiveError(
                    f'{where}: `custom "envelope" "allocate"` expects '
                    "(envelope: str, amount: Amount), got "
                    f"{values[1:]!r}"
                )
            amount = values[2]
            allocations.append(
                AllocateDirective(
                    date=entry.date,
                    envelope=values[1],
                    amount=amount.number,
                    currency=amount.currency,
                )
            )

        else:
            raise DirectiveError(f'{where}: unknown envelope directive kind "{kind}"')

    return ParsedDirectives(maps=tuple(maps), allocations=tuple(allocations))


def find_ambiguous_accounts(maps: tuple[MapDirective, ...]) -> dict[str, list[str]]:
    """Accounts mapped to more than one *distinct* envelope.

    Non-raising, so callers that need to report this alongside other
    diagnostics (e.g. `verify`, which must not also call such an account
    "unmapped") can inspect it without catching an exception.
    """
    account_to_all_envelopes: dict[str, set[str]] = {}
    for m in maps:
        account_to_all_envelopes.setdefault(m.account, set()).add(m.envelope)

    return {
        account: sorted(envelopes)
        for account, envelopes in account_to_all_envelopes.items()
        if len(envelopes) > 1
    }


def format_ambiguous_accounts(ambiguous: dict[str, list[str]]) -> str:
    return "; ".join(
        f'"{account}" -> {envelopes}' for account, envelopes in sorted(ambiguous.items())
    )


def build_account_map(maps: tuple[MapDirective, ...]) -> dict[str, str]:
    """Collapse `map` directives into account -> envelope, one entry per account.

    Mapping the same account to two *different* envelopes is ambiguous —
    PLAN.md §5.2 calls this out explicitly as something `verify` must catch,
    so it is a hard error here rather than "last one wins".
    """
    ambiguous = find_ambiguous_accounts(maps)
    if ambiguous:
        raise DirectiveError(
            f"account(s) mapped to more than one envelope: {format_ambiguous_accounts(ambiguous)}"
        )

    return {m.account: m.envelope for m in maps}
