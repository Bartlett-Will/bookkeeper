"""Tier 2: user-authored rules (PLAN.md §5.4).

Reads `data/rules.yaml` -- a small, hand-edited, git-tracked list of
"if it looks like X, it's account Y" statements the user states explicitly
once instead of confirming the same merchant over and over. First matching
rule wins, in file order; confidence is always 1.0, because a rule is the
user telling the system a fact, not a guess.

Two things are never allowed to fail silently: a regex that doesn't
compile, and a rule that targets an account not open in the ledger. Both
are configuration bugs that would otherwise misfile transactions quietly,
so both raise `RuleError` naming the offending rule.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import yaml

from bookkeeper import paths
from bookkeeper.categorize.models import CategorizationInput, LedgerContext, Prediction, Tier


class RuleError(ValueError):
    """A rule in `rules.yaml` is malformed or targets an unknown account."""


def rules_path() -> Path:
    return paths.data_dir() / "rules.yaml"


@dataclass(frozen=True)
class _CompiledRule:
    name: str
    pattern: re.Pattern[str]
    account: str
    sign: str | None  # "negative" (spending), "positive" (income/refund), or None
    amount_min: Decimal | None
    amount_max: Decimal | None
    asset_account: str | None


def _compile_rule(raw: dict, index: int) -> _CompiledRule:
    name = raw.get("name") or raw.get("pattern") or f"rule #{index}"

    if "pattern" not in raw:
        raise RuleError(f'rule "{name}": missing required "pattern"')
    if "account" not in raw:
        raise RuleError(f'rule "{name}": missing required "account"')

    try:
        pattern = re.compile(raw["pattern"], re.IGNORECASE)
    except re.error as exc:
        raise RuleError(f'rule "{name}": invalid regex {raw["pattern"]!r}: {exc}') from exc

    sign = raw.get("sign")
    if sign not in (None, "negative", "positive"):
        raise RuleError(f'rule "{name}": "sign" must be "negative" or "positive", got {sign!r}')

    def _decimal(field: str) -> Decimal | None:
        return Decimal(str(raw[field])) if field in raw else None

    return _CompiledRule(
        name=name,
        pattern=pattern,
        account=raw["account"],
        sign=sign,
        amount_min=_decimal("amount_min"),
        amount_max=_decimal("amount_max"),
        asset_account=raw.get("asset_account"),
    )


def _load_rules(path: Path) -> list[_CompiledRule]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or []
    if not isinstance(raw, list):
        raise RuleError(f'{path}: expected a YAML list of rules, got {type(raw).__name__}')
    return [_compile_rule(entry, index) for index, entry in enumerate(raw)]


def _matches(rule: _CompiledRule, txn: CategorizationInput) -> bool:
    text_matches = rule.pattern.search(txn.description) is not None
    payee_matches = txn.payee is not None and rule.pattern.search(txn.payee) is not None
    if not (text_matches or payee_matches):
        return False
    if rule.sign == "negative" and txn.amount >= 0:
        return False
    if rule.sign == "positive" and txn.amount < 0:
        return False
    if rule.amount_min is not None and txn.amount < rule.amount_min:
        return False
    if rule.amount_max is not None and txn.amount > rule.amount_max:
        return False
    return rule.asset_account is None or rule.asset_account == txn.asset_account


class RuleCategorizer:
    tier = Tier.RULE

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else rules_path()
        self._rules = _load_rules(self._path)

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        for rule in self._rules:
            if rule.account not in ctx.accounts:
                raise RuleError(
                    f'rule "{rule.name}" targets account "{rule.account}", which is '
                    "not open in the ledger -- fix rules.yaml or open the account"
                )
            if _matches(rule, txn):
                return Prediction(
                    account=rule.account,
                    confidence=1.0,
                    tier=Tier.RULE,
                    rationale=f'matched rule "{rule.name}"',
                )
        return None
