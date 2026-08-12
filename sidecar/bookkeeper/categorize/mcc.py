"""Tier 3: merchant category code lookup (PLAN.md §3.1 amendment).

The live SimpleFIN bridge sends an undocumented `mcc` field the protocol
spec never mentions. Where present it is a high-precision deterministic
signal -- but it is a *category* signal ("this is a grocery store"), not an
account name, and the ledger's actual chart of accounts is user-authored
and unpredictable. So this table maps MCC -> intent (a set of keywords), and
resolution against `ctx.accounts` happens at predict time: if the ledger has
no account matching that intent -- or more than one, ambiguously -- this
abstains rather than inventing or guessing an account.

Confidence is deliberately below 1.0 and varies by code: MCC itself is
undocumented and unguaranteed by any real institution (§3.1), and the demo
bridge has been observed mislabeling merchants outright -- a bait-and-tackle
shop tagged 5812, *Eating Places* (observed live, 2026-07-30) -- so restaurant
codes in particular are trusted less than e.g. gas or pharmacy, which tend
to be assigned more consistently in real-world MCC data.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from bookkeeper import paths
from bookkeeper.categorize.models import CategorizationInput, LedgerContext, Prediction, Tier


def mcc_map_path() -> Path:
    return paths.data_dir() / "mcc-map.yaml"


@dataclass(frozen=True)
class _MccIntent:
    label: str
    keywords: tuple[str, ...]
    confidence: float


# MCC -> category intent. Not exhaustive -- covers the categories PLAN.md
# §3.1 calls out by name (groceries, restaurants, gas, utilities, pharmacy)
# plus a handful of adjacent codes in the same families that are common on
# real bank feeds and cheap to justify.
_MCC_TABLE: dict[str, _MccIntent] = {
    "5411": _MccIntent("groceries", ("grocery", "groceries", "supermarket"), 0.85),
    "5422": _MccIntent("groceries (meat/butcher)", ("grocery", "groceries"), 0.8),
    "5812": _MccIntent("restaurants", ("dining", "restaurant"), 0.65),
    "5813": _MccIntent("bars/drinking places", ("dining", "restaurant", "bar"), 0.6),
    "5814": _MccIntent("fast food", ("dining", "restaurant"), 0.7),
    "5541": _MccIntent("gas stations", ("gas", "fuel", "transport"), 0.85),
    "5542": _MccIntent("automated fuel dispensers", ("gas", "fuel", "transport"), 0.85),
    "4900": _MccIntent("utilities", ("utilit",), 0.8),
    "4814": _MccIntent("telecom services", ("internet", "telecom", "phone"), 0.75),
    "5912": _MccIntent("pharmacy/drug stores", ("pharmacy", "drug", "health"), 0.8),
    "8011": _MccIntent("doctors", ("health", "medical"), 0.75),
    "8062": _MccIntent("hospitals", ("health", "medical"), 0.75),
}

# User-override confidence is higher than any built-in intent (it's an
# explicit statement, like a rule) but still shy of 1.0 -- the underlying
# MCC field remains an undocumented, unguaranteed signal no matter who
# configured the mapping.
_OVERRIDE_CONFIDENCE = 0.9


def _resolve(keywords: tuple[str, ...], accounts: tuple[str, ...]) -> str | None:
    """The one account whose name contains any of `keywords`, else `None`.

    `None` covers two different situations alike: no matching account
    exists, or more than one does (e.g. this project's own ledger has no
    single "Utilities" account, only three siblings -- Electric, Water,
    Internet -- under `Expenses:Utilities:`). Either way there is no single
    correct answer to hand back, so abstaining is the only honest move.
    """
    matches = [account for account in accounts if any(kw in account.lower() for kw in keywords)]
    return matches[0] if len(matches) == 1 else None


def _load_overrides(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {str(mcc): account for mcc, account in raw.items()}


class MccCategorizer:
    """Tier 3. Optional `data/mcc-map.yaml` (`{mcc: account}`) overrides the
    built-in table entirely for that code -- for a merchant category the
    user knows better than the generic keyword guess, or one missing here.
    """

    tier = Tier.MCC

    def __init__(self, overrides_path: Path | None = None) -> None:
        self._overrides_path = overrides_path if overrides_path is not None else mcc_map_path()
        self._overrides = _load_overrides(self._overrides_path)

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        if txn.mcc is None:
            return None

        override_account = self._overrides.get(txn.mcc)
        if override_account is not None:
            if override_account not in ctx.accounts:
                return None  # override names an account no longer open -- don't invent it
            return Prediction(
                account=override_account,
                confidence=_OVERRIDE_CONFIDENCE,
                tier=Tier.MCC,
                rationale=f'MCC {txn.mcc} (user override) -> {override_account}',
            )

        intent = _MCC_TABLE.get(txn.mcc)
        if intent is None:
            return None
        account = _resolve(intent.keywords, ctx.accounts)
        if account is None:
            return None
        return Prediction(
            account=account,
            confidence=intent.confidence,
            tier=Tier.MCC,
            rationale=f"MCC {txn.mcc} ({intent.label}) -> {account}",
        )
