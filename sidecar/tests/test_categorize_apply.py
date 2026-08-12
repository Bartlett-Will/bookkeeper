"""`categorize --apply` is the only code in Phase 3 that writes to the user's
financial records, and §7 rates "auto-apply silently corrupts the ledger" as
its highest-severity risk. These tests pin the three properties that answer
it:

1. **Nothing is written unless it was asked for and permitted.** Dry run is
   the default, and with no configured threshold even `--apply` writes
   nothing (decision 5: review-everything is the shipped default).
2. **A rewrite changes only what it meant to.** Every byte outside the
   retargeted posting and the stamped metadata is preserved, verified
   against fixtures built with `ingest.render.render_transaction` -- the
   real on-disk format -- and against the real committed ledger file.
3. **Reruns are byte-identical**, proven by hashing across two `--apply`
   runs exactly as `tests/test_ingest_sync.py` proves it for sync.

Fixtures live under `tmp_path` via `BOOKKEEPER_ROOT`. The real `ledger/` is
only ever read, never written.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path

import pytest

from bookkeeper import paths
from bookkeeper.categorize.apply import (
    DISPOSITION_ABSTAINED,
    DISPOSITION_AUTO_APPLY_OFF,
    DISPOSITION_BELOW_THRESHOLD,
    DISPOSITION_DRY_RUN,
    POLICY_ENV_VAR,
    POLICY_FILE_NAME,
    POLICY_KEY,
    LedgerEdit,
    _split_entries,
    apply_edits,
    load_auto_apply_policy,
    run_categorize,
)
from bookkeeper.categorize.models import (
    CategorizationInput,
    LedgerContext,
    Prediction,
    Tier,
)
from bookkeeper.ingest.normalize import NormalizedTransaction
from bookkeeper.ingest.render import render_transaction

REPO_ROOT = Path(__file__).resolve().parents[2]

ACCOUNTS = (
    "Expenses:Food:Groceries",
    "Expenses:Food:Dining",
    "Expenses:Home:Utilities",
    "Income:Salary",
)
CONTEXT = LedgerContext(accounts=ACCOUNTS)

CHECKING = "Assets:SimpleFIN:Checking"
SAVINGS = "Assets:SimpleFIN:Savings"


class FakeCascade:
    """Stands in for the cascade: a fixed answer per transaction id."""

    def __init__(self, predictions: dict[str, Prediction | None]) -> None:
        self._predictions = predictions

    def predict(self, txn: CategorizationInput, ctx: LedgerContext) -> Prediction | None:
        return self._predictions.get(txn.simplefin_id)


def _input(
    simplefin_id: str,
    description: str,
    amount: str,
    asset_account: str = CHECKING,
    mcc: str | None = None,
) -> CategorizationInput:
    return CategorizationInput(
        description=description,
        amount=Decimal(amount),
        posted_date=date(2026, 5, 3),
        asset_account=asset_account,
        simplefin_id=simplefin_id,
        mcc=mcc,
    )


def _rendered(txn: CategorizationInput) -> str:
    """The exact bytes `bookkeeper sync` would have written for `txn`.

    Built through the real renderer rather than a hand-typed string so a
    change to the on-disk format breaks these tests loudly instead of
    letting the rewriter silently stop matching real ledger files.
    """
    return render_transaction(
        NormalizedTransaction(
            simplefin_id=txn.simplefin_id,
            posted_date=txn.posted_date,
            amount=txn.amount,
            currency=txn.currency,
            description=txn.description,
            asset_account=txn.asset_account,
            mcc=txn.mcc,
            payee=txn.payee,
            memo=txn.memo,
        )
    )


HEADER = "; hand-written header that must survive every rewrite\n\n"


@pytest.fixture
def ledger(bookkeeper_root):
    """A transactions file in the real format, plus the inputs that describe it."""
    transactions = [
        _input("TXN-GROC", "Grocery store", "-163.36", mcc="5411"),
        _input("TXN-BAIT", "Fishing bait", "-19.96", mcc="5812"),
        _input("TXN-PAY", "Pay day!", "2500.00"),
        _input("TXN-GROC", "Grocery store", "-11.11", asset_account=SAVINGS, mcc="5411"),
    ]
    text = HEADER + "".join(_rendered(t) for t in transactions)
    path = paths.transactions_dir() / "2026.beancount"
    path.write_text(text, encoding="utf-8")
    return transactions


def _ledger_text() -> str:
    return (paths.transactions_dir() / "2026.beancount").read_text(encoding="utf-8")


def _hash_transactions() -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths.transactions_dir().glob("*.beancount"))
    }


def _line_diff(before: str, after: str) -> tuple[list[str], list[str]]:
    """The lines that left and the lines that arrived, ignoring everything equal."""
    a, b = before.splitlines(), after.splitlines()
    removed: list[str] = []
    added: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
        if tag in ("delete", "replace"):
            removed.extend(a[i1:i2])
        if tag in ("insert", "replace"):
            added.extend(b[j1:j2])
    return removed, added


def _confident(account: str, tier: Tier = Tier.MEMORY, confidence: float = 0.99) -> Prediction:
    return Prediction(
        account=account, confidence=confidence, tier=tier, rationale="3 prior confirmations"
    )


# --------------------------------------------------------------------------
# Splitting is lossless
# --------------------------------------------------------------------------


def test_split_entries_round_trips_the_real_committed_ledger():
    # Read-only against the real generated ledger (~338 transactions). The
    # rewriter's "preserve every byte we did not deliberately change"
    # guarantee is exactly this identity, so it is checked against real
    # data and not only against fixtures.
    real = REPO_ROOT / "ledger" / "transactions" / "2026.beancount"
    text = real.read_text(encoding="utf-8")

    blocks = _split_entries(text)

    assert len(blocks) > 100
    assert "".join(blocks) == text


@pytest.mark.parametrize(
    "text",
    [
        "",
        "; only a comment\n",
        "2026-01-01 * \"x\"\n  Assets:A  1 USD\n  Expenses:Unknown\n",
        "; header\n\n2026-01-01 * \"x\"\n  Expenses:Unknown\n\n2026-01-02 * \"y\"\n",
        "2026-01-01 * \"x\"\n  Expenses:Unknown\n\n\n\n",
    ],
)
def test_split_entries_is_lossless(text):
    assert "".join(_split_entries(text)) == text


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def test_dry_run_is_the_default_and_writes_nothing(ledger):
    before = _ledger_text()
    cascade = FakeCascade({t.simplefin_id: _confident("Expenses:Food:Groceries") for t in ledger})

    result = run_categorize(
        transactions=ledger, context=CONTEXT, cascade=cascade, threshold=0.5, commit=False
    )

    assert result.ok
    assert result.auto_applied == 0
    assert result.queued_for_review == len(ledger)
    assert all(d.disposition == DISPOSITION_DRY_RUN for d in result.decisions)
    assert _ledger_text() == before
    assert "dry run" in result.render()


def test_apply_with_no_configured_threshold_writes_nothing(ledger, monkeypatch):
    # The load-bearing test for decision 5. Even with --apply and a cascade
    # answering 1.0 on every transaction, an unconfigured policy must leave
    # the ledger untouched and route everything to review. A default
    # threshold invented here would be the silent-corruption risk of §7.
    monkeypatch.delenv(POLICY_ENV_VAR, raising=False)
    before = _ledger_text()
    cascade = FakeCascade(
        {t.simplefin_id: _confident("Expenses:Food:Groceries", confidence=1.0) for t in ledger}
    )

    result = run_categorize(
        apply=True, transactions=ledger, context=CONTEXT, cascade=cascade, commit=False
    )

    assert result.ok
    assert result.predicted == len(ledger)
    assert result.auto_applied == 0
    assert result.queued_for_review == len(ledger)
    assert all(d.disposition == DISPOSITION_AUTO_APPLY_OFF for d in result.decisions)
    assert _ledger_text() == before
    rendered = result.render()
    assert "auto-apply: OFF" in rendered
    assert "review-everything" in rendered


def test_apply_writes_only_predictions_at_or_above_the_threshold(ledger):
    cascade = FakeCascade(
        {
            "TXN-GROC": _confident("Expenses:Food:Groceries", confidence=0.96),
            "TXN-BAIT": _confident("Expenses:Food:Dining", tier=Tier.MCC, confidence=0.80),
            "TXN-PAY": _confident("Income:Salary", tier=Tier.RULE, confidence=0.95),
        }
    )

    result = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.95,
        commit=False,
    )

    assert result.ok
    # TXN-GROC appears twice (two accounts, same id) and both are >= 0.95.
    assert result.auto_applied == 3
    below = [d for d in result.decisions if d.disposition == DISPOSITION_BELOW_THRESHOLD]
    assert [d.simplefin_id for d in below] == ["TXN-BAIT"]

    text = _ledger_text()
    assert text.count("Expenses:Unknown") == 1  # only the below-threshold one remains
    assert "Expenses:Food:Groceries" in text
    assert "Income:Salary" in text
    assert "Expenses:Food:Dining" not in text
    assert "auto-apply: ON" in result.render()


def test_a_tier_abstention_stays_unknown_and_goes_to_review(ledger):
    cascade = FakeCascade({"TXN-GROC": _confident("Expenses:Food:Groceries")})

    result = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )

    abstained = [d for d in result.decisions if d.disposition == DISPOSITION_ABSTAINED]
    assert {d.simplefin_id for d in abstained} == {"TXN-BAIT", "TXN-PAY"}
    assert _ledger_text().count("Expenses:Unknown") == 2


def test_a_prediction_of_unknown_is_treated_as_an_abstention(ledger):
    # models.py: predicting Expenses:Unknown would count as coverage while
    # meaning the opposite. It must never be written back as a decision.
    cascade = FakeCascade(
        {"TXN-PAY": Prediction(account="Expenses:Unknown", confidence=1.0, tier=Tier.LLM)}
    )

    result = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )

    assert result.auto_applied == 0
    pay = next(d for d in result.decisions if d.simplefin_id == "TXN-PAY")
    assert pay.disposition == DISPOSITION_ABSTAINED
    assert pay.suggested_account is None


def test_limit_bounds_how_many_transactions_are_considered(ledger):
    cascade = FakeCascade({t.simplefin_id: _confident("Expenses:Food:Groceries") for t in ledger})

    result = run_categorize(
        transactions=ledger, context=CONTEXT, cascade=cascade, limit=2, commit=False
    )

    assert result.considered == 2


# --------------------------------------------------------------------------
# The rewrite
# --------------------------------------------------------------------------


def test_rewrite_preserves_every_other_byte_in_the_file(ledger):
    before = _ledger_text()
    cascade = FakeCascade({"TXN-BAIT": _confident("Expenses:Food:Dining", tier=Tier.MCC)})

    run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )
    after = _ledger_text()

    assert after.startswith(HEADER)  # hand-written header untouched
    # Every untouched transaction's bytes survive verbatim.
    for txn in ledger:
        if txn.simplefin_id == "TXN-BAIT":
            continue
        assert _rendered(txn) in after
    # The rewritten entry keeps its narration, metadata and asset posting.
    assert '2026-05-03 * "Fishing bait"' in after
    assert 'simplefin-id: "TXN-BAIT"' in after
    assert 'simplefin-mcc: "5812"' in after
    assert f"{CHECKING}   -19.96 USD" in after

    # The only line-level difference is the retargeted posting plus the
    # four stamped metadata lines -- nothing else moved, reflowed, or was
    # reordered by the write.
    removed, added = _line_diff(before, after)
    assert removed == ["  Expenses:Unknown"]
    assert added == [
        '  bookkeeper-account: "Expenses:Food:Dining"',
        '  bookkeeper-tier: "mcc"',
        '  bookkeeper-confidence: "0.9900"',
        '  bookkeeper-decision: "auto"',
        "  Expenses:Food:Dining",
    ]


def test_rewrite_stamps_the_decision_as_metadata(ledger):
    cascade = FakeCascade(
        {"TXN-PAY": Prediction(account="Income:Salary", confidence=0.875, tier=Tier.RULE)}
    )

    run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )
    text = _ledger_text()

    assert 'bookkeeper-account: "Income:Salary"' in text
    assert 'bookkeeper-tier: "rule"' in text
    assert 'bookkeeper-confidence: "0.8750"' in text
    assert 'bookkeeper-decision: "auto"' in text
    # Beancount requires transaction metadata before the postings.
    lines = text.splitlines()
    meta_index = lines.index('  bookkeeper-account: "Income:Salary"')
    posting_index = lines.index(f"  {CHECKING}   2500.00 USD")
    assert meta_index < posting_index


def test_rewrite_targets_the_right_account_when_two_share_a_simplefin_id(ledger):
    # SimpleFIN ids are unique per account, not globally (see ingest/dedup).
    # Keying a rewrite on the bare id would retarget the wrong transaction.
    twins = [t for t in ledger if t.simplefin_id == "TXN-GROC"]
    assert {t.asset_account for t in twins} == {CHECKING, SAVINGS}, "fixture must have twins"

    report = apply_edits(
        [
            LedgerEdit(
                asset_account=CHECKING,
                simplefin_id="TXN-GROC",
                account="Expenses:Food:Groceries",
                tier="memory",
                confidence=0.99,
            )
        ]
    )
    assert report.edits_applied == 1

    text = _ledger_text()
    savings_block = next(
        b for b in _split_entries(text) if f'simplefin-account: "{SAVINGS}"' in b
    )
    assert "Expenses:Unknown" in savings_block, "the savings-side twin must be untouched"
    assert text.count("Expenses:Food:Groceries") == 2  # posting + metadata, on one entry only


def test_an_edit_matching_nothing_is_reported_not_crashed(ledger):
    report = apply_edits(
        [
            LedgerEdit(
                asset_account=CHECKING,
                simplefin_id="NO-SUCH-TXN",
                account="Expenses:Food:Groceries",
                tier="memory",
                confidence=1.0,
            )
        ]
    )

    assert report.edits_applied == 0
    assert report.files_written == ()
    assert report.unmatched == ((CHECKING, "NO-SUCH-TXN"),)


def test_an_already_categorized_transaction_is_never_reclassified(ledger):
    edit = LedgerEdit(
        asset_account=CHECKING,
        simplefin_id="TXN-BAIT",
        account="Expenses:Food:Dining",
        tier="mcc",
        confidence=0.9,
    )
    apply_edits([edit])
    after_first = _ledger_text()

    # A later run reaching the same transaction with a *different* answer
    # must not overwrite the decision already recorded.
    second = apply_edits(
        [
            LedgerEdit(
                asset_account=CHECKING,
                simplefin_id="TXN-BAIT",
                account="Expenses:Home:Utilities",
                tier="llm",
                confidence=1.0,
            )
        ]
    )

    assert second.edits_applied == 0
    assert _ledger_text() == after_first
    assert "Expenses:Home:Utilities" not in after_first


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_applying_twice_leaves_the_file_byte_identical(ledger):
    cascade = FakeCascade(
        {
            "TXN-GROC": _confident("Expenses:Food:Groceries"),
            "TXN-BAIT": _confident("Expenses:Food:Dining", tier=Tier.MCC),
            "TXN-PAY": _confident("Income:Salary", tier=Tier.RULE),
        }
    )

    first = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )
    assert first.auto_applied == 4
    hashes_after_first = _hash_transactions()
    assert hashes_after_first

    # A second run sees the same cascade, but the ledger no longer offers
    # these transactions as uncategorized -- which is what makes the rerun
    # structurally a no-op rather than a formatting coincidence.
    second = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )

    assert second.ok
    assert _hash_transactions() == hashes_after_first, (
        "ledger changed between two identical categorize --apply runs -- idempotency broken"
    )
    assert second.files_written == ()


def test_a_no_op_rerun_never_opens_a_file_for_writing(ledger, monkeypatch):
    # Same discipline as ingest/sync.py: not "writes the same bytes", but
    # "does not open the file at all". Proven by making any write explode.
    cascade = FakeCascade({t.simplefin_id: _confident("Expenses:Food:Groceries") for t in ledger})
    run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )

    def explode(*args, **kwargs):
        raise AssertionError("a no-op rerun opened a ledger file for writing")

    monkeypatch.setattr(Path, "write_text", explode)

    result = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=cascade,
        threshold=0.5,
        commit=False,
    )

    assert result.ok


# --------------------------------------------------------------------------
# Policy resolution
# --------------------------------------------------------------------------


def test_policy_defaults_to_off(bookkeeper_root, monkeypatch):
    monkeypatch.delenv(POLICY_ENV_VAR, raising=False)

    policy, warnings = load_auto_apply_policy()

    assert policy.threshold is None
    assert not policy.enabled
    assert warnings == []
    assert not policy.permits(_confident("Expenses:Food:Groceries", confidence=1.0))


def test_policy_reads_the_config_file(bookkeeper_root, monkeypatch):
    monkeypatch.delenv(POLICY_ENV_VAR, raising=False)
    (paths.data_dir() / POLICY_FILE_NAME).write_text(json.dumps({POLICY_KEY: 0.97}))

    policy, warnings = load_auto_apply_policy()

    assert policy.threshold == 0.97
    assert policy.enabled
    assert warnings == []
    assert policy.permits(_confident("Expenses:Food:Groceries", confidence=0.97))
    assert not policy.permits(_confident("Expenses:Food:Groceries", confidence=0.96))


def test_env_var_overrides_the_config_file(bookkeeper_root, monkeypatch):
    (paths.data_dir() / POLICY_FILE_NAME).write_text(json.dumps({POLICY_KEY: 0.97}))
    monkeypatch.setenv(POLICY_ENV_VAR, "0.80")

    policy, _ = load_auto_apply_policy()

    assert policy.threshold == 0.80
    assert POLICY_ENV_VAR in policy.source


@pytest.mark.parametrize("bad", ["banana", "-0.5", "0", "1.5"])
def test_an_unusable_threshold_fails_closed(bookkeeper_root, monkeypatch, bad):
    # A typo must never widen what gets written to a ledger unattended.
    monkeypatch.setenv(POLICY_ENV_VAR, bad)

    policy, warnings = load_auto_apply_policy()

    assert policy.threshold is None
    assert not policy.enabled
    assert warnings and POLICY_ENV_VAR in warnings[0]


def test_an_unreadable_policy_file_fails_closed(bookkeeper_root, monkeypatch):
    monkeypatch.delenv(POLICY_ENV_VAR, raising=False)
    (paths.data_dir() / POLICY_FILE_NAME).write_text("{not json")

    policy, warnings = load_auto_apply_policy()

    assert policy.threshold is None
    assert warnings


def test_a_broken_cascade_fails_the_command_without_touching_the_ledger(ledger):
    class Exploding:
        def predict(self, txn, ctx):
            raise RuntimeError("ollama is not running")

    before = _ledger_text()

    result = run_categorize(
        apply=True,
        transactions=ledger,
        context=CONTEXT,
        cascade=Exploding(),
        threshold=0.5,
        commit=False,
    )

    assert not result.ok
    assert "ollama is not running" in result.render()
    assert _ledger_text() == before


def test_result_is_json_serializable_for_the_phase_4_api(ledger):
    cascade = FakeCascade({"TXN-GROC": _confident("Expenses:Food:Groceries")})

    result = run_categorize(transactions=ledger, context=CONTEXT, cascade=cascade, commit=False)

    encoded = json.dumps(result.to_dict())
    assert "Expenses:Food:Groceries" in encoded
