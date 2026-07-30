"""Predict accounts, and — only when explicitly permitted — write them to the ledger.

This module is the one place in Phase 3 that modifies the user's financial
records, so it is built around the §7 risk it exists to avoid ("auto-apply
silently corrupts the ledger", severity High). Three properties carry that
weight:

**Dry run is the default.** `run_categorize()` with no arguments predicts
and reports and touches nothing. Writing is opt-in (`--apply`), matching
the CLI contract's own comment.

**Auto-apply is off until someone sets a number.** Decision 5 ships
review-everything, and §5.5 is explicit that the threshold comes from
measured per-bucket precision, "never guessed". So there is no default
threshold here -- not 0.95, not anything. With no configured policy,
`AutoApplyPolicy.threshold is None` and *every* prediction routes to
review even under `--apply`. Inventing a plausible-looking default would
be exactly the silent corruption the plan warns about, dressed up as a
sensible constant. The rendered output always states the effective policy
so the operator can never be confused about which mode they are in.

**Rewrites are surgical and idempotent.** The ledger is edited as text,
not reparsed and reprinted, because beancount's printer would reformat
every untouched transaction in the file and bury a two-line change in a
3000-line diff -- destroying the reviewable-diff property that makes git
a usable undo. `_split_entries` chops the file at top-level date lines and
`"".join(...)` reproduces it byte-for-byte, so every byte outside a
deliberately rewritten posting is preserved by construction.

Idempotency follows structurally, the same way `ingest/sync.py` gets it: a
rewritten transaction no longer posts to `Expenses:Unknown`, so the next
run does not select it, so there is nothing to change and the file is
never opened for writing. `tests/test_categorize_apply.py` proves it by
hashing across two `--apply` runs, as `tests/test_ingest_sync.py` does.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bookkeeper import paths
from bookkeeper.categorize.gitcommit import CommitResult, commit_ledger, describe_batch
from bookkeeper.categorize.models import (
    UNKNOWN_ACCOUNTS,
    CategorizationInput,
    LedgerContext,
    Prediction,
)

#: One-off override for a single run, e.g. an operator who has just read an
#: eval report and wants to try a threshold before committing to it.
POLICY_ENV_VAR = "BOOKKEEPER_AUTO_APPLY_THRESHOLD"

#: Durable policy, in `data/` next to `memory.json` and git-tracked for the
#: same reason: raising autonomy over the user's ledger is a decision that
#: should appear in the history with a date and a diff, not live in a shell
#: profile where nobody can see it.
POLICY_FILE_NAME = "categorize-policy.json"
POLICY_KEY = "auto_apply_threshold"

#: Metadata stamped on every transaction this module rewrites, so a human
#: (and the next run) can tell machine-assigned postings from hand-written
#: ones, and see how sure the machine was. `bookkeeper-decision` separates
#: an unattended auto-apply from a human confirmation -- they carry very
#: different trust, and after the fact only this distinguishes them.
META_ACCOUNT = "bookkeeper-account"
META_TIER = "bookkeeper-tier"
META_CONFIDENCE = "bookkeeper-confidence"
META_DECISION = "bookkeeper-decision"

DECISION_AUTO = "auto"
DECISION_CONFIRMED = "confirmed"

_ENTRY_START_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\s")
_META_LINE_RE = re.compile(r'^(\s+)([a-z][a-z0-9-]*):\s')
_SIMPLEFIN_ID_RE = re.compile(r'^\s+simplefin-id:\s*"([^"]*)"\s*$', re.MULTILINE)
_SIMPLEFIN_ACCOUNT_RE = re.compile(r'^\s+simplefin-account:\s*"([^"]*)"\s*$', re.MULTILINE)
_DEFAULT_INDENT = "  "


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AutoApplyPolicy:
    """The gate on unattended ledger writes.

    `threshold is None` means review-everything: the shipped default, and
    the only honest one until §5.5's calibration has produced a bucket that
    clears the precision target. §5.5 explicitly allows the outcome "if no
    bucket clears it, auto-apply stays off" -- so off is a supported
    terminal state, not a placeholder waiting to be filled in.
    """

    threshold: float | None = None
    source: str = "default"

    @property
    def enabled(self) -> bool:
        return self.threshold is not None

    def permits(self, prediction: Prediction | None) -> bool:
        if prediction is None or self.threshold is None:
            return False
        if prediction.account in UNKNOWN_ACCOUNTS:
            return False
        return prediction.confidence >= self.threshold

    def describe(self) -> str:
        if not self.enabled:
            return (
                "auto-apply: OFF — review-everything (PLAN.md decision 5). No threshold is "
                "configured, so nothing is written automatically and every prediction goes "
                f"to the review queue. Set {POLICY_KEY!r} in data/{POLICY_FILE_NAME} (or "
                f"${POLICY_ENV_VAR}) from a measured eval report to enable it."
            )
        return (
            f"auto-apply: ON — confidence >= {self.threshold:.4f} (source: {self.source}). "
            "Predictions below this stay Expenses:Unknown and go to the review queue."
        )


def _policy_file() -> Path:
    return paths.data_dir() / POLICY_FILE_NAME


def _coerce_threshold(raw: Any, source: str) -> tuple[float | None, str | None]:
    """Parse a threshold, or explain why it is unusable.

    Anything unparseable falls back to review-everything rather than to a
    guess. Failing *closed* is the only safe direction: a typo'd config
    must never widen what gets written to a ledger unattended.
    """
    if raw is None:
        return None, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None, f"{source} is not a number ({raw!r}); auto-apply stays off"
    if not 0.0 < value <= 1.0:
        return None, f"{source} must be in (0, 1]; got {value}. Auto-apply stays off"
    return value, None


def load_auto_apply_policy(threshold: float | None = None) -> tuple[AutoApplyPolicy, list[str]]:
    """Resolve the effective policy: explicit argument > env var > file > off."""
    warnings: list[str] = []

    if threshold is not None:
        value, problem = _coerce_threshold(threshold, "the supplied threshold")
        if problem:
            warnings.append(problem)
        return AutoApplyPolicy(threshold=value, source="explicit argument"), warnings

    env_raw = os.environ.get(POLICY_ENV_VAR)
    if env_raw is not None and env_raw.strip():
        value, problem = _coerce_threshold(env_raw.strip(), f"${POLICY_ENV_VAR}")
        if problem:
            warnings.append(problem)
        return AutoApplyPolicy(threshold=value, source=f"${POLICY_ENV_VAR}"), warnings

    path = _policy_file()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            warnings.append(f"could not read {path} ({exc}); auto-apply stays off")
            return AutoApplyPolicy(), warnings
        if not isinstance(payload, dict):
            warnings.append(f"{path} is not a JSON object; auto-apply stays off")
            return AutoApplyPolicy(), warnings
        value, problem = _coerce_threshold(payload.get(POLICY_KEY), f"{POLICY_KEY} in {path}")
        if problem:
            warnings.append(problem)
        return AutoApplyPolicy(threshold=value, source=f"data/{POLICY_FILE_NAME}"), warnings

    return AutoApplyPolicy(), warnings


# --------------------------------------------------------------------------
# In-place ledger rewriting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerEdit:
    """One transaction's `Expenses:Unknown` posting, retargeted.

    Keyed on `(asset_account, simplefin_id)` — the same pair
    `ingest/dedup.py` uses, and for the same reason: SimpleFIN ids are
    unique per account, not globally (confirmed live), so the id alone
    would address two different transactions.
    """

    asset_account: str
    simplefin_id: str
    account: str
    tier: str
    confidence: float
    decision: str = DECISION_AUTO

    @property
    def key(self) -> tuple[str, str]:
        return (self.asset_account, self.simplefin_id)


@dataclass(frozen=True)
class ApplyReport:
    edits_applied: int = 0
    files_written: tuple[str, ...] = ()
    unmatched: tuple[tuple[str, str], ...] = ()


def _split_entries(text: str) -> list[str]:
    """Chop a transactions file at top-level date lines, losslessly.

    `"".join(_split_entries(t)) == t` for any input, including a leading
    comment header and any trailing whitespace. That identity is the whole
    point: it is what makes "preserve every byte we did not deliberately
    change" a property of the code rather than a hope.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines(keepends=True):
        if _ENTRY_START_RE.match(line) and current:
            blocks.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("".join(current))
    return blocks


def _entry_key(block: str) -> tuple[str, str] | None:
    id_match = _SIMPLEFIN_ID_RE.search(block)
    account_match = _SIMPLEFIN_ACCOUNT_RE.search(block)
    if id_match is None or account_match is None:
        return None
    return (account_match.group(1), id_match.group(1))


def _format_confidence(confidence: float) -> str:
    """Fixed-width, so rerunning never produces a different byte for the same number."""
    return f"{confidence:.4f}"


def _rewrite_entry(block: str, edit: LedgerEdit) -> str | None:
    """Retarget the Unknown posting and stamp decision metadata, or return None.

    Returns None when the block has no Unknown posting left to change --
    already categorized, by a previous run or by hand. Skipping rather than
    overwriting is what keeps a second `--apply` a genuine no-op, and means
    this can never quietly reclassify something a human already decided.
    """
    lines = block.splitlines(keepends=True)
    target_index: int | None = None
    indent = _DEFAULT_INDENT
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped in UNKNOWN_ACCOUNTS and line[: len(line) - len(line.lstrip())]:
            target_index = index
            indent = line[: len(line) - len(line.lstrip())]
            break
    if target_index is None:
        return None

    newline = "\n" if lines[target_index].endswith("\n") else ""
    lines[target_index] = f"{indent}{edit.account}{newline}"

    # Beancount requires transaction metadata to sit between the header and
    # the postings, so the insertion point is after the last existing
    # metadata line (or immediately after the header when there is none).
    insert_at = 1
    for index, line in enumerate(lines[1:target_index], start=1):
        if _META_LINE_RE.match(line):
            insert_at = index + 1
    stamped = [
        f'{indent}{META_ACCOUNT}: "{edit.account}"\n',
        f'{indent}{META_TIER}: "{edit.tier}"\n',
        f'{indent}{META_CONFIDENCE}: "{_format_confidence(edit.confidence)}"\n',
        f'{indent}{META_DECISION}: "{edit.decision}"\n',
    ]
    lines[insert_at:insert_at] = stamped
    return "".join(lines)


def apply_edits(
    edits: Sequence[LedgerEdit], transactions_dir: Path | None = None
) -> ApplyReport:
    """Write `edits` into `ledger/transactions/*.beancount`, in place.

    A file is opened for writing only when its text actually changed --
    the same structural idempotency discipline `ingest/sync.py` documents.
    An unchanged rerun does not touch a single file, so "byte-identical" is
    not something the formatting has to be careful enough to achieve.
    """
    if not edits:
        return ApplyReport()

    by_key = {edit.key: edit for edit in edits}
    matched: set[tuple[str, str]] = set()
    written: list[str] = []
    applied = 0

    directory = transactions_dir or paths.transactions_dir()
    if not directory.exists():
        return ApplyReport(unmatched=tuple(sorted(by_key)))

    for path in sorted(directory.glob("*.beancount")):
        original = path.read_text(encoding="utf-8")
        blocks = _split_entries(original)
        changed = False
        for index, block in enumerate(blocks):
            key = _entry_key(block)
            if key is None or key not in by_key or key in matched:
                continue
            rewritten = _rewrite_entry(block, by_key[key])
            if rewritten is None:
                continue
            blocks[index] = rewritten
            matched.add(key)
            changed = True
            applied += 1
        if not changed:
            continue
        path.write_text("".join(blocks), encoding="utf-8")
        written.append(path.name)

    return ApplyReport(
        edits_applied=applied,
        files_written=tuple(written),
        unmatched=tuple(sorted(set(by_key) - matched)),
    )


# --------------------------------------------------------------------------
# Predict-and-report
# --------------------------------------------------------------------------

#: Why a prediction did not reach the ledger. Carried on every decision so
#: the review queue can explain itself to a human instead of just listing
#: what it failed to place.
DISPOSITION_APPLIED = "applied"
DISPOSITION_DRY_RUN = "dry-run"
DISPOSITION_BELOW_THRESHOLD = "below-threshold"
DISPOSITION_AUTO_APPLY_OFF = "auto-apply-off"
DISPOSITION_ABSTAINED = "abstained"


@dataclass(frozen=True)
class Decision:
    """One transaction, its best guess, and what happened to it.

    Primitives only -- ISO date strings and decimal strings rather than
    `date`/`Decimal`, no beancount objects -- so `to_dict()` is directly
    JSON-serializable for the Phase 4 review cards without a custom
    encoder standing between the sidecar and the browser.
    """

    simplefin_id: str
    asset_account: str
    posted_date: str
    description: str
    amount: str
    currency: str
    disposition: str
    suggested_account: str | None = None
    confidence: float | None = None
    tier: str | None = None
    rationale: str = ""
    mcc: str | None = None
    payee: str | None = None

    @property
    def applied(self) -> bool:
        return self.disposition == DISPOSITION_APPLIED

    @property
    def needs_review(self) -> bool:
        return self.disposition != DISPOSITION_APPLIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "simplefin_id": self.simplefin_id,
            "asset_account": self.asset_account,
            "posted_date": self.posted_date,
            "description": self.description,
            "amount": self.amount,
            "currency": self.currency,
            "disposition": self.disposition,
            "suggested_account": self.suggested_account,
            "confidence": self.confidence,
            "tier": self.tier,
            "rationale": self.rationale,
            "mcc": self.mcc,
            "payee": self.payee,
        }


@dataclass
class CategorizeResult:
    ok: bool
    policy: AutoApplyPolicy = field(default_factory=AutoApplyPolicy)
    applied: bool = False
    decisions: list[Decision] = field(default_factory=list)
    files_written: tuple[str, ...] = ()
    commit: CommitResult | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def considered(self) -> int:
        return len(self.decisions)

    @property
    def predicted(self) -> int:
        return sum(1 for d in self.decisions if d.suggested_account is not None)

    @property
    def auto_applied(self) -> int:
        return sum(1 for d in self.decisions if d.applied)

    @property
    def queued_for_review(self) -> int:
        return sum(1 for d in self.decisions if d.needs_review)

    def tier_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for decision in self.decisions:
            if decision.tier is None:
                continue
            counts[decision.tier] = counts.get(decision.tier, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "auto_apply_threshold": self.policy.threshold,
            "auto_apply_source": self.policy.source,
            "considered": self.considered,
            "predicted": self.predicted,
            "auto_applied": self.auto_applied,
            "queued_for_review": self.queued_for_review,
            "decisions": [d.to_dict() for d in self.decisions],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }

    def render(self) -> str:
        if not self.ok:
            return "\n".join(["categorize failed:", *(f"  - {e}" for e in self.errors)])

        mode = (
            "mode: APPLY — predictions at or above the threshold are written to the ledger"
            if self.applied
            else "mode: dry run — nothing is written (pass --apply to write)"
        )
        lines = [self.policy.describe(), mode, ""]
        lines.append(f"uncategorized transactions considered: {self.considered}")
        lines.append(f"predictions offered: {self.predicted}")
        lines.append(f"abstained (no tier answered): {self.considered - self.predicted}")
        lines.append(f"auto-applied to the ledger: {self.auto_applied}")
        lines.append(f"queued for review: {self.queued_for_review}")

        tiers = self.tier_counts()
        if tiers:
            lines.append("")
            lines.append("predictions by tier:")
            lines.extend(f"  {tier}: {count}" for tier, count in sorted(tiers.items()))

        if self.files_written:
            lines.append("")
            lines.append("files written:")
            lines.extend(f"  ledger/transactions/{name}" for name in self.files_written)
        if self.commit is not None:
            lines.append("")
            lines.append(self.commit.render())
        if self.warnings:
            lines.append("")
            lines.append("warnings:")
            lines.extend(f"  - {w}" for w in self.warnings)
        return "\n".join(lines)


def _decimal_str(amount: Decimal) -> str:
    return str(amount)


def _decision_for(
    txn: CategorizationInput, prediction: Prediction | None, disposition: str
) -> Decision:
    return Decision(
        simplefin_id=txn.simplefin_id,
        asset_account=txn.asset_account,
        posted_date=txn.posted_date.isoformat(),
        description=txn.description,
        amount=_decimal_str(txn.amount),
        currency=txn.currency,
        disposition=disposition,
        suggested_account=prediction.account if prediction else None,
        confidence=prediction.confidence if prediction else None,
        tier=prediction.tier.value if prediction else None,
        rationale=prediction.rationale if prediction else "",
        mcc=txn.mcc,
        payee=txn.payee,
    )


def _default_sources() -> tuple[Callable[[], list[CategorizationInput]], Callable[[], LedgerContext]]:
    from bookkeeper.categorize.context import build_ledger_context, uncategorized_transactions

    return uncategorized_transactions, build_ledger_context


def predict_all(
    transactions: Iterable[CategorizationInput],
    context: LedgerContext,
    cascade: Any,
    policy: AutoApplyPolicy,
    applying: bool,
) -> list[Decision]:
    """Run the cascade over `transactions` and classify each outcome.

    Kept separate from the I/O in `run_categorize` so the gating logic --
    the part that decides what is allowed to touch a ledger -- is testable
    with no filesystem, no git, and no real cascade.
    """
    decisions: list[Decision] = []
    for txn in transactions:
        prediction = cascade.predict(txn, context)
        if prediction is None or prediction.account in UNKNOWN_ACCOUNTS:
            decisions.append(_decision_for(txn, None, DISPOSITION_ABSTAINED))
            continue
        if not policy.enabled:
            decisions.append(_decision_for(txn, prediction, DISPOSITION_AUTO_APPLY_OFF))
        elif not policy.permits(prediction):
            decisions.append(_decision_for(txn, prediction, DISPOSITION_BELOW_THRESHOLD))
        elif not applying:
            decisions.append(_decision_for(txn, prediction, DISPOSITION_DRY_RUN))
        else:
            decisions.append(_decision_for(txn, prediction, DISPOSITION_APPLIED))
    return decisions


def run_categorize(
    apply: bool = False,
    limit: int | None = None,
    use_llm: bool = True,
    *,
    threshold: float | None = None,
    commit: bool = True,
    transactions: Sequence[CategorizationInput] | None = None,
    context: LedgerContext | None = None,
    cascade: Any = None,
    transactions_dir: Path | None = None,
) -> CategorizeResult:
    """Predict accounts for every uncategorized transaction; write only if permitted.

    The keyword-only parameters after `use_llm` are injection seams for
    tests and for the Phase 4 API; the CLI calls only the three positional
    ones, per the contract in `cli.py`.
    """
    policy, warnings = load_auto_apply_policy(threshold)

    try:
        if transactions is None or context is None:
            load_transactions, load_context = _default_sources()
            if transactions is None:
                transactions = load_transactions()
            if context is None:
                context = load_context()
        if cascade is None:
            from bookkeeper.categorize.cascade import build_default_cascade

            cascade = build_default_cascade(use_llm=use_llm)
    except Exception as exc:  # noqa: BLE001 - surfaced via the result, never raised at the CLI
        return CategorizeResult(ok=False, policy=policy, errors=[str(exc)])

    selected = list(transactions)[:limit] if limit is not None else list(transactions)

    try:
        decisions = predict_all(selected, context, cascade, policy, applying=apply)
    except Exception as exc:  # noqa: BLE001
        return CategorizeResult(ok=False, policy=policy, errors=[str(exc)])

    result = CategorizeResult(
        ok=True, policy=policy, applied=apply, decisions=decisions, warnings=warnings
    )
    if not apply:
        return result

    edits = [
        LedgerEdit(
            asset_account=d.asset_account,
            simplefin_id=d.simplefin_id,
            account=d.suggested_account or "",
            tier=d.tier or "",
            confidence=d.confidence or 0.0,
            decision=DECISION_AUTO,
        )
        for d in decisions
        if d.applied
    ]
    report = apply_edits(edits, transactions_dir=transactions_dir)
    result.files_written = report.files_written
    if report.unmatched:
        result.warnings.append(
            f"{len(report.unmatched)} prediction(s) matched no Expenses:Unknown posting in the "
            "ledger and were skipped (already categorized, or the ledger changed mid-run)"
        )
    # Rewrite the decisions that did not actually land, so the reported
    # counts describe the ledger rather than the intent.
    if report.unmatched:
        skipped = set(report.unmatched)
        result.decisions = [
            _replace_disposition(d, DISPOSITION_ABSTAINED)
            if d.applied and (d.asset_account, d.simplefin_id) in skipped
            else d
            for d in result.decisions
        ]

    if commit and report.edits_applied:
        result.commit = commit_ledger(
            describe_batch(report.edits_applied, f"auto-applied at confidence >= {policy.threshold}")
        )
    return result


def _replace_disposition(decision: Decision, disposition: str) -> Decision:
    return Decision(
        simplefin_id=decision.simplefin_id,
        asset_account=decision.asset_account,
        posted_date=decision.posted_date,
        description=decision.description,
        amount=decision.amount,
        currency=decision.currency,
        disposition=disposition,
        suggested_account=decision.suggested_account,
        confidence=decision.confidence,
        tier=decision.tier,
        rationale=decision.rationale,
        mcc=decision.mcc,
        payee=decision.payee,
    )
