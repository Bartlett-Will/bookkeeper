"""SimpleFIN response models.

Field set matches the *documented* protocol (simplefin.org/protocol.html)
plus a small number of fields confirmed present on the live demo server
that the spec doesn't mention (team-lead live-tested
`https://demo:demo@beta-bridge.simplefin.org/simplefin/accounts` on
2026-07-30): `mcc`, `payee`, `memo` on transactions, and a plain
`errors: list[str]` alongside the documented `errlist`. Those are modeled
explicitly, as optional, sourced from that observation -- not invented.
`Transaction.model_config` stays `extra="forbid"` on top of that: it's a
deliberate strictness gate so a *future* undocumented field shows up as a
loud test failure instead of silently vanishing.

All monetary fields are `Decimal`, never `float`: SimpleFIN sends amounts as
decimal strings, and a `float` round-trip is a silent precision bug waiting
to happen on a financial ledger. The `mode="before"` validators below reject
a bare JSON float outright rather than quietly accepting one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _reject_float(value: Any) -> Any:
    if isinstance(value, float):
        # Must be ValueError: pydantic v2 field_validator only wraps
        # ValueError/AssertionError into a ValidationError, not TypeError.
        raise ValueError("monetary amounts must be decimal strings, not floats")  # noqa: TRY004
    return value


class Transaction(BaseModel):
    """A SimpleFIN transaction. Undocumented fields we haven't reviewed are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")

    id: str
    posted: int
    amount: Decimal
    description: str
    transacted_at: int | None = None
    pending: bool | None = None
    extra: dict[str, Any] | None = None
    # Not in the protocol doc, but sent by the live demo server. Optional
    # and never relied upon by ingest -- a real bank feed may omit all three.
    mcc: str | None = None
    payee: str | None = None
    memo: str | None = None

    @field_validator("amount", mode="before")
    @classmethod
    def _amount_not_float(cls, v: Any) -> Any:
        return _reject_float(v)


class Connection(BaseModel):
    """Institution connection metadata. Permissive: this is descriptive only."""

    model_config = ConfigDict(extra="ignore")

    conn_id: str
    name: str | None = None
    org_id: str | None = None
    org_name: str | None = None
    org_url: str | None = None
    sfin_url: str | None = None


class Account(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    conn_id: str | None = None
    currency: str
    balance: Decimal
    available_balance: Decimal | None = Field(default=None, alias="available-balance")
    balance_date: int = Field(alias="balance-date")
    transactions: list[Transaction] = Field(default_factory=list)
    extra: dict[str, Any] | None = None

    @field_validator("balance", "available_balance", mode="before")
    @classmethod
    def _balance_not_float(cls, v: Any) -> Any:
        return _reject_float(v)


class ErrorEntry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    code: str | None = None
    msg: str | None = None
    conn_id: str | None = None


class AccountSet(BaseModel):
    """The top-level response body of `GET /accounts`."""

    model_config = ConfigDict(extra="ignore")

    errlist: list[ErrorEntry] = Field(default_factory=list)
    connections: list[Connection] = Field(default_factory=list)
    accounts: list[Account] = Field(default_factory=list)
    # Not in the protocol doc. Live-observed (2026-07-30): a >90-day
    # start-date/end-date window comes back as a *soft* error here --
    # HTTP 200, with data still attached -- rather than as an errlist
    # entry or a failed request. Must not be silently dropped.
    errors: list[str] = Field(default_factory=list)
