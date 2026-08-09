"""Rule suggestions from real spending (PLAN.md §5.4, §6 Phase 6).

After a backfill the review queue is hundreds of transactions dominated by
a few recurring merchants. §5.4's premise is that a memory of past decisions
resolves those exactly; a *rule* earns its place on top of that memory in
one specific case — when the bank mangles one merchant into several
normalized keys. Tier 1 is an exact index, so `SAFEWAY #1234`,
`SAFEWAY STORE 4471` and `SAFEWAY.COM` are three separate confirmations. One
regex is one statement. That is what this module looks for and nothing else.

**It suggests; it never applies.** Review-everything is the shipped default
(decision 5) and §5.5 raises autonomy only on measured evidence. Every
return value carries `applied=False` as a field rather than as a promise in
a docstring, and `data/rules.yaml` is opened for reading only.

## What it ranks on, and why not the other thing

**Occurrences.** Count and total amount genuinely disagree, and the
disagreement is not close: an annual $2,000 insurance premium seen once
outranks a $4 coffee seen sixty times on money and is worth 1/60th as much
as a rule. A rule is a labour-saving device. In review-everything mode every
uncategorized transaction costs exactly one human decision regardless of its
size, so the transactions a rule *removes from the queue* is the thing being
maximised.

Amount is not discarded — `total_amount` and `median_amount` come back on
every suggestion — because it answers a different question: not "is this
worth automating" but "how much does getting it wrong cost". A reader who
wants to sort by money has the numbers to do it and will be sorting on a
different question than the one this ranking answers. Ties break on total
amount, then on the pattern, so the order is total and stable.

## Where the proposed account comes from

Never from a classifier. A rule matches at confidence 1.0 — it is the user
stating a fact (`rules.py`) — so proposing one from a fitted estimate would
launder a guess into a certainty. Two evidence sources only, in order:

1. **Confirmed memory** — what tier 1 would say for that key, asked by
   calling tier 1. A human confirmed it.
2. **Ledger history** — the account non-`Unknown` postings with the same
   normalized description were actually booked to.

A merchant with neither is *not* given an invented account. It comes back in
`undecided`, ranked the same way, which is the more useful answer anyway:
"categorize one of these and 40 transactions follow".

## What a suggestion says it would have done

Every suggestion is evaluated by compiling the exact YAML block it prints
through `categorize.rules`' own `_compile_rule` and running the tier's own
`_matches` over the real uncategorized set. The count reported is therefore
not a model of what the rule would do; it is what the rule does. Three
conflicts are surfaced, and the first two matter more than the match count:

- **`confirmed_memory`** — the pattern matches transactions whose normalized
  key tier 1 already resolves to a *different* account. Those particular
  transactions are safe (memory runs first and wins), which is exactly why
  this is dangerous rather than harmless: the rule fires on every *other*
  mangling of that merchant and silently contradicts a decision the user
  made by hand. A rule that disagrees with a confirmation is worse than no
  rule.
- **`ledger_history`** — the pattern matches transactions already booked to
  a different account. The same disagreement, sourced from the ledger.
- **`shadowed`** — an existing rule in `rules.yaml` already matches. First
  match wins in file order (`rules.py`), and a suggestion is appended, so a
  fully-shadowed suggestion would never fire at all.

## What is deliberately not claimed

Nothing here is measured accuracy. `docs/phase3-accuracy.md` is explicit
that its 90% is a synthetic-corpus figure that says nothing about real
data, and this module has not been measured against real data either — its
suggestions are recall over patterns present in whatever ledger it was
pointed at, not a rate at which suggestions turn out to be right. The one
claim it does make is checkable by the reader: the match counts are
reproduced by running the printed YAML.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from bookkeeper.categorize.context import build_ledger_context, uncategorized_transactions
from bookkeeper.categorize.memory import MemoryCategorizer
from bookkeeper.categorize.models import CategorizationInput, LedgerContext
from bookkeeper.categorize.normalize import normalize_description

# Imported rather than reimplemented, and the private names are the point.
# A suggestion's headline claim is "this is what the rule would do", and the
# only way that claim cannot drift from tier 2 is for tier 2's own compiler
# and matcher to produce it. A second matcher here would be a second set of
# semantics for `sign`, `payee` and amount bounds, and the first time they
# disagreed the suggestion would be confidently wrong about its own effect.
from bookkeeper.categorize.rules import (
    RuleCategorizer,
    RuleError,
    _compile_rule,
    _matches,
)

#: Fewer uncategorized transactions than this behind a merchant and no rule
#: is suggested for it. This is an editorial threshold, not a statistical
#: one, and it is echoed in the response so a reader can disagree with it.
#: The reasoning: tier 1 already resolves an exactly-repeating merchant after
#: a single confirmation, at zero maintenance cost. A rule is strictly more
#: machinery — a regex the user now owns, maintains and can get wrong — so it
#: has to clear a bar that one confirmation does not.
MIN_OCCURRENCES = 5

#: Raw bank strings shown per suggestion, so a reader can see what the
#: pattern was derived from without the output becoming a transaction dump.
MAX_SAMPLES = 3

#: Joins the tokens of a normalized key back into a regex. Normalization
#: collapses punctuation to spaces (`PG&E` -> `pg e`), so a pattern built by
#: re-joining those tokens with a literal space would not match the raw
#: description it came from. This tolerates whatever separator the bank used,
#: including none at all, which is what makes `pg e` match both `PG&E` and
#: `PGE`.
_SEPARATOR = "[^A-Za-z0-9]*"

_CENTS = Decimal("0.01")

#: Evidence sources for a proposed account, strongest first.
EVIDENCE_CONFIRMED_MEMORY = "confirmed_memory"
EVIDENCE_LEDGER_HISTORY = "ledger_history"

#: Conflict kinds. See the module docstring for what each one means.
CONFLICT_CONFIRMED_MEMORY = "confirmed_memory"
CONFLICT_LEDGER_HISTORY = "ledger_history"
CONFLICT_SHADOWED = "shadowed"

#: How suggestions are ordered, stated in the response rather than left to
#: the reader to infer from the order.
RANKED_BY = "occurrences"


def _median(values: list[Decimal]) -> Decimal:
    """Median of a sortable list; the mean of the middle two when even."""
    ordered = sorted(values)
    n = len(ordered)
    middle = n // 2
    if n % 2 == 1:
        return ordered[middle]
    return ((ordered[middle - 1] + ordered[middle]) / 2).quantize(_CENTS)


@dataclass(frozen=True)
class RuleConflict:
    """One reason a proposed rule disagrees with something already decided.

    `count` is transactions, not merchants, so a conflict's size is
    comparable with the suggestion's own `occurrences` — "matches 40, of
    which 12 contradict a confirmation" is a judgement a reader can make;
    "matches 40, has a conflict" is not.
    """

    kind: str
    account: str
    count: int
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "account": self.account,
            "count": self.count,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RuleSuggestion:
    """One proposed `rules.yaml` entry and its measured effect on this ledger.

    `yaml` is the block a human would paste, and it is also the block that
    produced every count on this object -- it was compiled through tier 2's
    own compiler and run through tier 2's own matcher. That is why the
    numbers are checkable rather than modelled.
    """

    name: str
    pattern: str
    account: str
    sign: str | None
    evidence: str
    evidence_detail: str
    normalized_keys: tuple[str, ...]
    keys_with_evidence: tuple[str, ...]
    occurrences: int
    total_amount: Decimal
    median_amount: Decimal
    first_seen: date
    last_seen: date
    already_categorized_matches: int
    conflicts: tuple[RuleConflict, ...]
    sample_descriptions: tuple[str, ...]
    yaml: str

    @property
    def conflict_free(self) -> bool:
        """True when nothing already decided disagrees with this rule.

        Named for the fact rather than for a verdict: `recommended` would be
        this module telling a user what to do, which is the one thing a
        suggestion is not allowed to do.
        """
        return not self.conflicts

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "pattern": self.pattern,
            "account": self.account,
            "sign": self.sign,
            "evidence": self.evidence,
            "evidence_detail": self.evidence_detail,
            "normalized_keys": list(self.normalized_keys),
            "keys_with_evidence": list(self.keys_with_evidence),
            "occurrences": self.occurrences,
            "total_amount": str(self.total_amount),
            "median_amount": str(self.median_amount),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "already_categorized_matches": self.already_categorized_matches,
            "conflicts": [c.to_dict() for c in self.conflicts],
            "conflict_free": self.conflict_free,
            "sample_descriptions": list(self.sample_descriptions),
            "yaml": self.yaml,
        }


@dataclass(frozen=True)
class UndecidedMerchant:
    """A merchant big enough to matter that nothing has decided an account for.

    Returned rather than guessed at. It is also the most actionable line in
    the report: one confirmation here teaches tier 1 the whole group, and
    asking again afterwards may turn it into a suggestion.
    """

    #: Every normalized key in the merchant's cluster. Plural because a
    #: merchant the bank mangles three ways is one merchant to decide about
    #: and three keys to teach -- confirming one teaches that one only,
    #: which is precisely the case a rule exists to collapse.
    normalized_keys: tuple[str, ...]
    occurrences: int
    total_amount: Decimal
    median_amount: Decimal
    first_seen: date
    last_seen: date
    sample_descriptions: tuple[str, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "normalized_keys": list(self.normalized_keys),
            "occurrences": self.occurrences,
            "total_amount": str(self.total_amount),
            "median_amount": str(self.median_amount),
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "sample_descriptions": list(self.sample_descriptions),
            "reason": self.reason,
        }


@dataclass
class RuleSuggestions:
    ok: bool
    #: Always False. A field rather than a docstring promise, so a client can
    #: assert on it and a future change that starts writing has to lie in
    #: the response to go unnoticed.
    applied: bool = False
    suggestions: tuple[RuleSuggestion, ...] = ()
    undecided: tuple[UndecidedMerchant, ...] = ()
    uncategorized_total: int = 0
    merchants_seen: int = 0
    covered_by_suggestions: int = 0
    min_occurrences: int = MIN_OCCURRENCES
    ranked_by: str = RANKED_BY
    shown: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "suggestions": [s.to_dict() for s in self.suggestions],
            "undecided": [u.to_dict() for u in self.undecided],
            "uncategorized_total": self.uncategorized_total,
            "merchants_seen": self.merchants_seen,
            "covered_by_suggestions": self.covered_by_suggestions,
            "min_occurrences": self.min_occurrences,
            "ranked_by": self.ranked_by,
            "shown": self.shown,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["rule suggestions failed:", *(f"  - {e}" for e in self.errors)])

        lines = [
            (
                f"{self.uncategorized_total} uncategorized transaction(s) across "
                f"{self.merchants_seen} merchant(s), ranked by {self.ranked_by} "
                f"(at least {self.min_occurrences} to qualify)."
            ),
            "Nothing was written. Paste a block below into data/rules.yaml to accept it.",
            "",
        ]

        if not self.suggestions:
            lines.append("No merchant clears the bar for a rule.")
        else:
            lines.append(
                f"{len(self.suggestions)} rule suggestion(s) covering "
                f"{self.covered_by_suggestions} transaction(s):"
            )
            lines.append("")
            for s in self.suggestions:
                lines.append(
                    f"  {s.account}  <- {s.occurrences} txn(s), "
                    f"{s.total_amount:.2f} total, {s.median_amount:.2f} median"
                )
                lines.append(f"      pattern: {s.pattern}")
                lines.append(f"      because: {s.evidence_detail}")
                lines.append(
                    f"      seen {s.first_seen.isoformat()}..{s.last_seen.isoformat()} as: "
                    + "; ".join(s.sample_descriptions)
                )
                if s.already_categorized_matches:
                    lines.append(
                        f"      also matches {s.already_categorized_matches} "
                        "already-categorized transaction(s)"
                    )
                for c in s.conflicts:
                    lines.append(f"      CONFLICT ({c.kind}): {c.detail}")
                lines.append("")
                lines.extend(f"      {line}" for line in s.yaml.splitlines())
                lines.append("")

        if self.undecided:
            lines.append("")
            lines.append(
                f"{len(self.undecided)} merchant(s) big enough to matter that nothing has "
                "decided an account for. Categorize one of each and tier 1 learns the rest:"
            )
            for u in self.undecided:
                lines.append(
                    f"  {', '.join(u.normalized_keys)}  -- {u.occurrences} txn(s), "
                    f"{u.total_amount:.2f} total"
                )
                lines.append("      seen as: " + "; ".join(u.sample_descriptions))

        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines).rstrip("\n")


@dataclass(frozen=True)
class _Group:
    """Every uncategorized transaction sharing one normalized description."""

    key: str
    transactions: tuple[CategorizationInput, ...]

    @property
    def occurrences(self) -> int:
        return len(self.transactions)


def _group_by_normalized_key(
    transactions: list[CategorizationInput],
) -> tuple[list[_Group], int]:
    """Group by `normalize_description`, returning the groups and the empty count.

    Keyed on tier 1's own normalizer, deliberately and not incidentally: a
    suggestion grouped any other way would propose rules for merchants the
    memory tier does not consider the same merchant, and the two would then
    disagree about which transactions a confirmation covers.
    """
    buckets: dict[str, list[CategorizationInput]] = defaultdict(list)
    empty = 0
    for txn in transactions:
        key = normalize_description(txn.description)
        if not key:
            # A description that normalizes to nothing (punctuation, digits)
            # cannot found a pattern -- there is no token to match on.
            empty += 1
            continue
        buckets[key].append(txn)
    groups = [_Group(key=key, transactions=tuple(txns)) for key, txns in buckets.items()]
    return groups, empty


def _common_token_prefix(keys: list[str]) -> list[str]:
    """The leading tokens every key shares. At least one by construction."""
    token_lists = [key.split() for key in keys]
    prefix: list[str] = []
    for position in zip(*token_lists, strict=False):
        if len(set(position)) != 1:
            break
        prefix.append(position[0])
    return prefix


def _history_evidence(ctx: LedgerContext) -> dict[str, tuple[str, int, int]]:
    """`normalized key -> (account, count for it, total examples)` from the ledger.

    A tie between the top accounts abstains, matching `MemoryCategorizer`:
    two accounts equally attested is "the ledger does not actually say", and
    picking one arbitrarily would put a coin flip behind a confidence-1.0
    rule.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for example in ctx.examples:
        counts[example.normalized_description][example.account] += 1

    evidence: dict[str, tuple[str, int, int]] = {}
    for key, by_account in counts.items():
        total = sum(by_account.values())
        best = max(by_account.values())
        leaders = [account for account, count in by_account.items() if count == best]
        if len(leaders) != 1 or leaders[0] not in ctx.accounts:
            continue
        evidence[key] = (leaders[0], best, total)
    return evidence


def _memory_evidence(
    groups: list[_Group], ctx: LedgerContext, memory: MemoryCategorizer
) -> dict[str, tuple[str, str]]:
    """`normalized key -> (account, tier 1's own rationale)`.

    Asked by calling tier 1 rather than by reading `memory.json` directly, so
    the tie-abstention and the closed-label-set check are the tier's, not a
    second copy of them here. "What memory says" must mean what memory would
    actually say at categorize time.
    """
    out: dict[str, tuple[str, str]] = {}
    for group in groups:
        prediction = memory.predict(group.transactions[0], ctx)
        if prediction is not None:
            out[group.key] = (prediction.account, prediction.rationale)
    return out


@dataclass(frozen=True)
class _Candidate:
    """A pattern, the keys it was derived from, and the account proposed for it."""

    tokens: tuple[str, ...]
    keys: tuple[str, ...]
    account: str
    evidence: str
    evidence_detail: str
    keys_with_evidence: tuple[str, ...]


def _candidates(
    groups: list[_Group],
    memory_evidence: dict[str, tuple[str, str]],
    history_evidence: dict[str, tuple[str, int, int]],
) -> tuple[list[_Candidate], list[tuple[str, ...]]]:
    """Cluster keys into candidate rules, plus the clusters nothing decided.

    Keys are clustered by their first token, because that is where a bank's
    manglings of one merchant agree (`safeway`, `safeway store`,
    `safeway com`) and the pattern becomes their common token prefix -- the
    generalization a rule buys over tier 1's exact index.

    **Clustering happens before any size threshold, and that ordering is the
    whole point.** `SAFEWAY` split four ways across two keys is four and two
    occurrences, both under the bar, while the merchant behind them is six
    and clears it comfortably. Thresholding keys first would reject exactly
    the case a rule is for -- one merchant that tier 1 sees as several -- and
    keep only the cases tier 1 already handles by itself.

    A cluster whose evidenced members name **different** accounts is not a
    merchant, it is a collision (`amazon` and `amazon web services`), and it
    is split back into one candidate per key rather than resolved. Splitting
    costs a broader rule; resolving would file one merchant under another's
    account on the strength of a shared first word.
    """
    by_first_token: dict[str, list[_Group]] = defaultdict(list)
    for group in groups:
        by_first_token[group.key.split()[0]].append(group)

    candidates: list[_Candidate] = []
    undecided: list[tuple[str, ...]] = []
    for members in sorted(by_first_token.values(), key=lambda ms: ms[0].key):
        keys = sorted(g.key for g in members)
        evidenced: dict[str, tuple[str, str, str]] = {}
        for key in keys:
            found = _pick_evidence(key, memory_evidence, history_evidence)
            if found is not None:
                evidenced[key] = found

        if not evidenced:
            undecided.append(tuple(keys))
            continue

        if len({found[0] for found in evidenced.values()}) > 1:
            # Collision, not a merchant. Fall back to one candidate per key,
            # and the keys nothing decided go back to being undecided singly
            # -- there is no merchant here to speak for them.
            candidates.extend(
                _single_key_candidate(key, found) for key, found in evidenced.items()
            )
            undecided.extend((key,) for key in keys if key not in evidenced)
            continue

        # Memory outranks the ledger, so a cluster attributes itself to the
        # strongest evidence any member carries rather than to whichever
        # member happened to sort first.
        account, evidence, detail = min(
            evidenced.values(), key=lambda found: found[1] != EVIDENCE_CONFIRMED_MEMORY
        )
        tokens = _common_token_prefix(keys)
        if not tokens:  # pragma: no cover - keys share a first token by construction
            continue
        candidates.append(
            _Candidate(
                tokens=tuple(tokens),
                keys=tuple(keys),
                account=account,
                evidence=evidence,
                evidence_detail=_cluster_detail(detail, keys, sorted(evidenced)),
                keys_with_evidence=tuple(sorted(evidenced)),
            )
        )
    return candidates, undecided


def _single_key_candidate(key: str, found: tuple[str, str, str]) -> _Candidate:
    account, evidence, detail = found
    return _Candidate(
        tokens=tuple(key.split()),
        keys=(key,),
        account=account,
        evidence=evidence,
        evidence_detail=detail,
        keys_with_evidence=(key,),
    )


def _pick_evidence(
    key: str,
    memory_evidence: dict[str, tuple[str, str]],
    history_evidence: dict[str, tuple[str, int, int]],
) -> tuple[str, str, str] | None:
    """`(account, evidence kind, human detail)` for one key, or None.

    Memory outranks the ledger because a memory entry is a human saying so
    (`memory.py`), while a ledger posting may itself have come from a tier's
    prediction that nobody has reviewed.
    """
    remembered = memory_evidence.get(key)
    if remembered is not None:
        account, rationale = remembered
        return (account, EVIDENCE_CONFIRMED_MEMORY, f"tier 1 has {rationale}")
    historical = history_evidence.get(key)
    if historical is not None:
        account, count, total = historical
        return (
            account,
            EVIDENCE_LEDGER_HISTORY,
            f'{count}/{total} existing posting(s) for "{key}" are booked here',
        )
    return None


def _cluster_detail(detail: str, keys: list[str], evidenced: list[str]) -> str:
    """Say plainly when a pattern reaches keys that no evidence covers."""
    if len(keys) == len(evidenced):
        return detail
    uncovered = [key for key in keys if key not in set(evidenced)]
    return (
        f"{detail}; the pattern also reaches {len(uncovered)} key(s) nothing has "
        f"decided on ({', '.join(uncovered)}), which is the generalization a rule "
        "buys and the risk it carries"
    )


def _regex_for(tokens: tuple[str, ...]) -> str:
    return _SEPARATOR.join(re.escape(token) for token in tokens)


def _sign_for(matched: list[CategorizationInput]) -> str | None:
    """`negative`, `positive` or None, read off the transactions themselves.

    `CategorizationInput.amount` is the signed asset-side amount, so spending
    is negative. A merchant that is only ever spending gets `sign: negative`,
    which stops the rule from also claiming a refund from that merchant. A
    mixed merchant gets no `sign` -- inventing one would narrow the rule on
    an assumption the data contradicts.
    """
    if all(txn.amount < 0 for txn in matched):
        return "negative"
    if all(txn.amount >= 0 for txn in matched):
        return "positive"
    return None


def _yaml_block(name: str, pattern: str, account: str, sign: str | None) -> str:
    """The rule as `rules.yaml` would hold it.

    Values go through `json.dumps` rather than being wrapped in quotes by
    hand: a regex carries `[`, `*` and `\\`, and a JSON string is a valid
    YAML double-quoted scalar, so this cannot emit a block that YAML parses
    differently from what was measured.
    """
    lines = [
        f"- name: {json.dumps(name)}",
        f"  pattern: {json.dumps(pattern)}",
        f"  account: {json.dumps(account)}",
    ]
    if sign is not None:
        lines.append(f"  sign: {sign}")
    return "\n".join(lines)


def _rule_dict(name: str, pattern: str, account: str, sign: str | None) -> dict[str, Any]:
    rule: dict[str, Any] = {"name": name, "pattern": pattern, "account": account}
    if sign is not None:
        rule["sign"] = sign
    return rule


def _conflicts(
    matched: list[CategorizationInput],
    account: str,
    memory_evidence: dict[str, tuple[str, str]],
    history_evidence: dict[str, tuple[str, int, int]],
    compiled_pattern: re.Pattern[str],
    ctx: LedgerContext,
    shadowing: dict[str, int],
) -> list[RuleConflict]:
    """Everything already decided that this rule would contradict."""
    conflicts: list[RuleConflict] = []

    disagreeing_memory: dict[str, int] = defaultdict(int)
    for txn in matched:
        key = normalize_description(txn.description)
        remembered = memory_evidence.get(key)
        if remembered is not None and remembered[0] != account:
            disagreeing_memory[remembered[0]] += 1
    for other, count in sorted(disagreeing_memory.items()):
        conflicts.append(
            RuleConflict(
                kind=CONFLICT_CONFIRMED_MEMORY,
                account=other,
                count=count,
                detail=(
                    f"{count} matching transaction(s) have a confirmed memory of "
                    f'"{other}", not "{account}". Tier 1 runs first so those stay '
                    "correct, which is why this is dangerous rather than harmless: "
                    "the rule fires on every other mangling of that merchant and "
                    "silently contradicts a decision made by hand"
                ),
            )
        )

    disagreeing_history: dict[str, int] = defaultdict(int)
    for example in ctx.examples:
        if example.account == account:
            continue
        text_hit = compiled_pattern.search(example.description) is not None
        payee_hit = example.payee is not None and compiled_pattern.search(example.payee)
        if text_hit or payee_hit:
            disagreeing_history[example.account] += 1
    for other, count in sorted(disagreeing_history.items()):
        conflicts.append(
            RuleConflict(
                kind=CONFLICT_LEDGER_HISTORY,
                account=other,
                count=count,
                detail=(
                    f"{count} transaction(s) already in the ledger match this pattern "
                    f'but are booked to "{other}". The pattern is broader than the '
                    "merchant, or the merchant belongs somewhere else"
                ),
            )
        )

    for rule_name, count in sorted(shadowing.items()):
        detail = (
            f"{count} of {len(matched)} matching transaction(s) are already handled by "
            f"the existing rule {rule_name}. First match wins in file order, and a new "
            "rule is appended"
        )
        if count == len(matched):
            detail += ", so this rule would never fire at all"
        conflicts.append(
            RuleConflict(
                kind=CONFLICT_SHADOWED, account=account, count=count, detail=detail
            )
        )
    return conflicts


_RULE_NAME_RE = re.compile(r'matched rule "(.*)"$')


def _shadowing(
    matched: list[CategorizationInput],
    existing: RuleCategorizer | None,
    ctx: LedgerContext,
) -> dict[str, int]:
    """Existing rule name -> how many of `matched` it already claims.

    Asked by running tier 2 rather than by re-reading `rules.yaml`, for the
    same reason the match counts are: shadowing is a property of the tier's
    evaluation order, not of the file.
    """
    if existing is None:
        return {}
    counts: dict[str, int] = defaultdict(int)
    for txn in matched:
        prediction = existing.predict(txn, ctx)
        if prediction is None:
            continue
        found = _RULE_NAME_RE.search(prediction.rationale)
        counts[found.group(1) if found else prediction.rationale] += 1
    return dict(counts)


def _build_suggestion(
    candidate: _Candidate,
    transactions: list[CategorizationInput],
    ctx: LedgerContext,
    memory_evidence: dict[str, tuple[str, str]],
    history_evidence: dict[str, tuple[str, int, int]],
    existing: RuleCategorizer | None,
) -> RuleSuggestion | None:
    """Compile the candidate and measure it, or return None if it matches nothing."""
    pattern = _regex_for(candidate.tokens)
    name = " ".join(candidate.tokens).title()

    # Matched twice on purpose. The first pass is sign-blind, because the
    # sign is a fact read off the matches; the second uses the rule exactly
    # as printed, so the reported count belongs to the block a human pastes
    # and not to an intermediate form of it.
    probe = _compile_rule(_rule_dict(name, pattern, candidate.account, None), 0)
    sign = _sign_for([txn for txn in transactions if _matches(probe, txn)] or list(transactions))

    rule_dict = _rule_dict(name, pattern, candidate.account, sign)
    compiled = _compile_rule(rule_dict, 0)
    matched = [txn for txn in transactions if _matches(compiled, txn)]
    if not matched:  # pragma: no cover - a candidate is built from matching keys
        return None

    amounts = [abs(txn.amount) for txn in matched]
    compiled_pattern = re.compile(pattern, re.IGNORECASE)
    already = sum(
        1
        for example in ctx.examples
        if compiled_pattern.search(example.description) is not None
        or (example.payee is not None and compiled_pattern.search(example.payee) is not None)
    )
    conflicts = _conflicts(
        matched,
        candidate.account,
        memory_evidence,
        history_evidence,
        compiled_pattern,
        ctx,
        _shadowing(matched, existing, ctx),
    )

    seen: list[str] = []
    for txn in matched:
        if txn.description not in seen:
            seen.append(txn.description)
        if len(seen) == MAX_SAMPLES:
            break

    return RuleSuggestion(
        name=name,
        pattern=pattern,
        account=candidate.account,
        sign=sign,
        evidence=candidate.evidence,
        evidence_detail=candidate.evidence_detail,
        normalized_keys=candidate.keys,
        keys_with_evidence=candidate.keys_with_evidence,
        occurrences=len(matched),
        total_amount=sum(amounts, Decimal(0)).quantize(_CENTS),
        median_amount=_median(amounts).quantize(_CENTS),
        first_seen=min(txn.posted_date for txn in matched),
        last_seen=max(txn.posted_date for txn in matched),
        already_categorized_matches=already,
        conflicts=tuple(conflicts),
        sample_descriptions=tuple(seen),
        yaml=_yaml_block(name, pattern, candidate.account, sign),
    )


def _undecided(keys: tuple[str, ...], by_key: dict[str, _Group]) -> UndecidedMerchant:
    transactions = [txn for key in keys for txn in by_key[key].transactions]
    amounts = [abs(txn.amount) for txn in transactions]
    seen: list[str] = []
    for txn in transactions:
        if txn.description not in seen:
            seen.append(txn.description)
        if len(seen) == MAX_SAMPLES:
            break
    plural = (
        "one of each and tier 1 learns that key"
        if len(keys) == 1
        else f"one of each of its {len(keys)} keys, or write one rule covering all of them"
    )
    return UndecidedMerchant(
        normalized_keys=keys,
        occurrences=len(transactions),
        total_amount=sum(amounts, Decimal(0)).quantize(_CENTS),
        median_amount=_median(amounts).quantize(_CENTS),
        first_seen=min(txn.posted_date for txn in transactions),
        last_seen=max(txn.posted_date for txn in transactions),
        sample_descriptions=tuple(seen),
        reason=(
            "no confirmed memory and no existing ledger posting names an account for "
            f"this merchant, so there is nothing to propose a rule from -- categorize "
            f"{plural}"
        ),
    )


def suggest_rules(
    limit: int | None = None,
    *,
    entries: list[Any] | None = None,
    min_occurrences: int | None = None,
    rules_path: Path | None = None,
    memory_path: Path | None = None,
) -> RuleSuggestions:
    """Propose `rules.yaml` entries for the recurring merchants in the backlog.

    Read-only. `rules.yaml`, `memory.json` and the ledger are inputs; nothing
    on disk changes, and `RuleSuggestions.applied` is False on every path.

    `min_occurrences` is overridable rather than fixed so a user with a short
    history can lower the bar deliberately -- and `None` resolves to
    `MIN_OCCURRENCES` here rather than in the signature so a caller that has
    not imported the constant (the CLI, which would otherwise pay for
    importing beancount to build its parser) can still say "the default".
    The value actually used comes back in the response either way.
    """
    threshold = MIN_OCCURRENCES if min_occurrences is None else min_occurrences
    if entries is None:
        from bookkeeper.envelope.compute import load_ledger

        entries, _errors, _options = load_ledger()

    ctx = build_ledger_context(entries)
    transactions = uncategorized_transactions(entries)

    # A broken `rules.yaml` is reported, never worked around. Suggesting
    # rules against a file that does not parse would produce shadow counts
    # of zero and quietly claim no existing rule conflicts.
    try:
        existing: RuleCategorizer | None = RuleCategorizer(path=rules_path)
    except RuleError as exc:
        return RuleSuggestions(ok=False, errors=[f"existing rules.yaml is unusable: {exc}"])

    groups, empty_keys = _group_by_normalized_key(transactions)
    warnings: list[str] = []
    if empty_keys:
        warnings.append(
            f"{empty_keys} uncategorized transaction(s) have a description that "
            "normalizes to nothing (punctuation or digits only); no pattern can be "
            "derived from them and they are excluded from every figure here"
        )

    memory = MemoryCategorizer(path=memory_path)
    memory_evidence = _memory_evidence(groups, ctx, memory)
    history_evidence = _history_evidence(ctx)

    by_key = {g.key: g for g in groups}
    try:
        candidates, undecided_clusters = _candidates(groups, memory_evidence, history_evidence)
        built = [
            _build_suggestion(
                candidate,
                [txn for key in candidate.keys for txn in by_key[key].transactions],
                ctx,
                memory_evidence,
                history_evidence,
                existing,
            )
            for candidate in candidates
        ]
    except RuleError as exc:
        # Raised by tier 2 when an *existing* rule targets an account that is
        # not open -- a configuration bug that would misfile transactions
        # quietly, and one this module must not paper over.
        return RuleSuggestions(ok=False, errors=[f"existing rules.yaml is unusable: {exc}"])

    # The threshold applies to what the rule would actually resolve, which is
    # only knowable after the pattern has been run. See `_candidates`.
    suggestions = [s for s in built if s is not None and s.occurrences >= threshold]

    # Occurrences first; see the module docstring for why not amount. Total
    # amount and then the pattern break ties, so the order is total.
    suggestions.sort(key=lambda s: (-s.occurrences, -s.total_amount, s.pattern))

    undecided = [
        merchant
        for keys in undecided_clusters
        if (merchant := _undecided(keys, by_key)).occurrences >= threshold
    ]
    undecided.sort(key=lambda u: (-u.occurrences, -u.total_amount, u.normalized_keys))

    shown = suggestions if limit is None else suggestions[:limit]
    return RuleSuggestions(
        ok=True,
        applied=False,
        suggestions=tuple(shown),
        undecided=tuple(undecided if limit is None else undecided[:limit]),
        uncategorized_total=len(transactions),
        merchants_seen=len(groups),
        covered_by_suggestions=sum(s.occurrences for s in shown),
        min_occurrences=threshold,
        ranked_by=RANKED_BY,
        shown=len(shown),
        warnings=warnings,
    )
