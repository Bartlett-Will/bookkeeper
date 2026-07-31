"""Free-text search over ledger transactions (PLAN.md §5.3, `search_transactions`).

Reached from a chat message, so `q` is untrusted text that a model or a user
typed. Two separate injection surfaces have to be closed, and closing one does
not close the other:

- **BQL injection.** `q` is bound as a *query parameter* (`%(q)s`, beanquery's
  pyformat paramstyle) and never formatted into the query string. Nothing the
  user types can add a clause, and the query text is a constant this module
  owns.
- **Regex injection.** BQL's `~` is a regular-expression match, so a bound
  parameter is still compiled as a pattern. `(a+)+$` is a valid parameter and a
  catastrophic backtracker; `[` is a valid parameter and a `re.error` 500. So
  the parameter is `re.escape`d and prefixed with `(?i)`, which makes the whole
  search a case-insensitive *literal substring* match -- the thing a person
  typing into a chat box means anyway.

One row per transaction *leg on a funding account*, not one row per posting:
a spend has two postings and returning both would show every result twice.
The funding leg is the useful one -- it carries the signed amount the bank
reported -- and the categorized account is recovered from the same row's
`other_accounts`, so a match still reports where the money was filed.

Matching spans narration, payee, and any account on the transaction
(`has_account`), so "groceries" finds both the merchant and the envelope
account it was filed under.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import beanquery

from bookkeeper.envelope.compute import load_ledger
from bookkeeper.envelope.directives import (
    DirectiveError,
    build_account_map,
    parse_envelope_directives,
)

#: How many matches to return when the caller does not say. Small on purpose:
#: this feeds a chat surface that renders cards, not a spreadsheet.
DEFAULT_LIMIT = 50

#: Hard ceiling regardless of what the caller asks for. A tool argument comes
#: from an 8B model (§3.3) and `limit=100000` is a plausible thing for one to
#: emit; the ledger read is cheap but the JSON is not.
MAX_LIMIT = 500

#: The accounts a transaction is funded from. Liabilities included so a credit
#: card charge is found the same way a debit is.
_FUNDING_ACCOUNT_PATTERN = "^(Assets|Liabilities):"

#: Constant query text. The only variable parts are bound parameters.
_SEARCH_QUERY = """
SELECT
    date,
    narration,
    payee,
    account,
    other_accounts,
    number,
    currency,
    entry_meta("simplefin-id") AS simplefin_id,
    entry_meta("simplefin-payee") AS simplefin_payee,
    entry_meta("simplefin-memo") AS simplefin_memo
FROM #postings
WHERE account ~ %(funding)s
  AND (narration ~ %(q)s OR payee ~ %(q)s OR has_account(%(q)s))
ORDER BY date DESC, narration
"""


@dataclass(frozen=True)
class TransactionMatch:
    """One matching transaction, with enough detail to identify it by eye.

    Primitives and `Decimal`/`date` only -- no beancount objects -- for the
    same reason `categorize.review.ReviewEntry` is: this is rendered in a
    browser, and a beancount object leaking into the JSON would be discovered
    there rather than here.
    """

    posted_date: date
    description: str
    amount: Decimal
    currency: str
    account: str
    categorized_account: str | None = None
    envelope: str | None = None
    simplefin_id: str | None = None
    payee: str | None = None
    memo: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "posted_date": self.posted_date.isoformat(),
            "description": self.description,
            "amount": str(self.amount),
            "currency": self.currency,
            "account": self.account,
            "categorized_account": self.categorized_account,
            "envelope": self.envelope,
            "simplefin_id": self.simplefin_id,
            "payee": self.payee,
            "memo": self.memo,
        }


@dataclass
class TransactionSearch:
    ok: bool
    query: str = ""
    matches: list[TransactionMatch] = field(default_factory=list)
    total: int = 0
    limit: int = DEFAULT_LIMIT
    truncated: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "query": self.query,
            "total": self.total,
            "shown": len(self.matches),
            "limit": self.limit,
            "truncated": self.truncated,
            "matches": [m.to_dict() for m in self.matches],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["search failed:", *(f"  - {e}" for e in self.errors)])
        if not self.matches:
            return f"no transactions match {self.query!r}"

        lines = [f"{self.total} transaction(s) match {self.query!r}; showing {len(self.matches)}."]
        if self.truncated:
            lines.append(f"(limited to {self.limit} — narrow the search or raise --limit)")
        lines.append("")
        for m in self.matches:
            lines.append(
                f"{m.posted_date.isoformat()}  {m.amount:>12} {m.currency}  {m.description}"
            )
            filed = m.categorized_account or "—"
            envelope = f"  [{m.envelope}]" if m.envelope else ""
            lines.append(f"    {m.account} -> {filed}{envelope}")
            if m.simplefin_id:
                lines.append(f"    id: {m.simplefin_id}")
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines).rstrip("\n")


def literal_pattern(q: str) -> str:
    """`q` as a regex that can only ever match itself, case-insensitively.

    Public because it is the guard, and a guard that cannot be tested directly
    is a guard nobody checks. See the module docstring for why binding `q` as
    a parameter is necessary but not sufficient.
    """
    return f"(?i){re.escape(q)}"


def _coerce_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(int(limit), MAX_LIMIT))


def _envelope_map(entries: list[Any]) -> tuple[dict[str, str], list[str]]:
    """Account -> envelope, or an empty map plus a warning.

    A malformed or ambiguous mapping must not fail a *search*: the envelope
    label is decoration on a result row, while the transactions themselves are
    exactly what someone debugging a broken mapping is trying to look at.
    `verify` is the endpoint that judges the mapping.
    """
    try:
        parsed = parse_envelope_directives(entries)
        return build_account_map(parsed.maps), []
    except DirectiveError as exc:
        return {}, [f"envelope labels omitted: {exc}"]


def _first_other_account(value: Any) -> str | None:
    """The counter-account from a row's `other_accounts` set.

    Sorted rather than arbitrary so repeated searches over an unchanged ledger
    return byte-identical results; a split transaction reports the whole set
    joined, because dropping legs would misrepresent where the money went.
    """
    if not value:
        return None
    accounts = sorted(value) if isinstance(value, (set, frozenset, list, tuple)) else [str(value)]
    return ", ".join(accounts)


def search_transactions(
    q: str,
    limit: int | None = None,
    *,
    entries: list[Any] | None = None,
    errors: list[Any] | None = None,
    options: dict[str, Any] | None = None,
) -> TransactionSearch:
    """Transactions whose narration, payee, or accounts contain `q`.

    `entries`/`errors`/`options` let the sidecar feed its mtime-cached ledger
    in rather than paying `loader.load_file` per keystroke (§5.1); omitting
    them loads the ledger from disk, which is what the CLI wants.
    """
    text = q.strip() if q else ""
    if not text:
        return TransactionSearch(ok=False, query=q or "", errors=["search text is empty"])

    resolved_limit = _coerce_limit(limit)
    if entries is None:
        entries, errors, options = load_ledger()

    account_to_envelope, warnings = _envelope_map(entries)

    connection = beanquery.connect(
        "beancount:", entries=entries, errors=list(errors or []), options=dict(options or {})
    )
    cursor = connection.execute(
        _SEARCH_QUERY, {"q": literal_pattern(text), "funding": _FUNDING_ACCOUNT_PATTERN}
    )
    columns = [d.name for d in cursor.description]
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]

    matches = []
    for row in rows[:resolved_limit]:
        categorized = _first_other_account(row["other_accounts"])
        matches.append(
            TransactionMatch(
                posted_date=row["date"],
                description=row["narration"] or "",
                amount=row["number"],
                currency=row["currency"] or "",
                account=row["account"],
                categorized_account=categorized,
                envelope=account_to_envelope.get(categorized or ""),
                simplefin_id=row["simplefin_id"],
                payee=row["payee"] or row["simplefin_payee"],
                memo=row["simplefin_memo"],
            )
        )

    return TransactionSearch(
        ok=True,
        query=text,
        matches=matches,
        total=len(rows),
        limit=resolved_limit,
        truncated=len(rows) > resolved_limit,
        warnings=warnings,
    )
