"""Review-everything is the shipped default (decision 5), so the queue is the
primary categorization surface and its confirmation path is what makes
Phase 4's exit criterion -- "corrections demonstrably change the next run's
predictions" -- true.

Two things are pinned hardest here: the entry shape stays cleanly
JSON-serializable (Phase 4 renders these as generative-UI review cards, and
a leaked beancount object or `Decimal` would surface at the browser rather
than here), and a confirmation lands in *both* places it has to -- the
ledger and `data/memory.json` -- because one without the other silently
breaks the learning loop.

Fixtures live under `tmp_path` via `BOOKKEEPER_ROOT`; the real `ledger/` is
never written.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest

from bookkeeper import paths
from bookkeeper.categorize.apply import run_categorize
from bookkeeper.categorize.models import (
    CategorizationInput,
    LedgerContext,
    Prediction,
    Tier,
)
from bookkeeper.categorize.review import (
    HUMAN_TIER,
    Confirmation,
    ReviewQueue,
    confirm_categorization,
    confirm_categorizations,
    review_queue,
)
from bookkeeper.ingest.normalize import NormalizedTransaction
from bookkeeper.ingest.render import render_transaction

ACCOUNTS = (
    "Expenses:Food:Groceries",
    "Expenses:Food:Dining",
    "Income:Salary",
)
CONTEXT = LedgerContext(accounts=ACCOUNTS)
CHECKING = "Assets:SimpleFIN:Checking"


class FakeCascade:
    def __init__(self, predictions: dict[str, Prediction | None]) -> None:
        self._predictions = predictions

    def predict(self, txn, ctx):
        return self._predictions.get(txn.simplefin_id)


def _input(simplefin_id: str, description: str, amount: str) -> CategorizationInput:
    return CategorizationInput(
        description=description,
        amount=Decimal(amount),
        posted_date=date(2026, 5, 3),
        asset_account=CHECKING,
        simplefin_id=simplefin_id,
        mcc="5411",
    )


@pytest.fixture
def ledger(bookkeeper_root):
    transactions = [
        _input("TXN-GROC", "LOCAL GROCER STORE #1133", "-163.36"),
        _input("TXN-BAIT", "JOHNS FISHIN SHACK BAIT", "-19.96"),
        _input("TXN-PAY", "PAYROLL DEPOSIT", "2500.00"),
    ]
    text = "".join(
        render_transaction(
            NormalizedTransaction(
                simplefin_id=t.simplefin_id,
                posted_date=t.posted_date,
                amount=t.amount,
                currency=t.currency,
                description=t.description,
                asset_account=t.asset_account,
                mcc=t.mcc,
            )
        )
        for t in transactions
    )
    (paths.transactions_dir() / "2026.beancount").write_text(text, encoding="utf-8")
    return transactions


def _ledger_text() -> str:
    return (paths.transactions_dir() / "2026.beancount").read_text(encoding="utf-8")


def _queue(ledger, predictions: dict[str, Prediction | None], **kwargs) -> ReviewQueue:
    cascade = FakeCascade(predictions)

    def runner(**call_kwargs):
        return run_categorize(
            transactions=ledger, context=CONTEXT, cascade=cascade, **call_kwargs
        )

    return review_queue(categorize=runner, **kwargs)


# --------------------------------------------------------------------------
# The queue
# --------------------------------------------------------------------------


def test_queue_lists_every_transaction_still_on_unknown(ledger):
    queue = _queue(ledger, {})

    assert queue.ok
    assert queue.total == 3
    assert {e.simplefin_id for e in queue.entries} == {"TXN-GROC", "TXN-BAIT", "TXN-PAY"}
    assert all(e.current_account == "Expenses:Unknown" for e in queue.entries)


def test_queue_carries_the_best_guess_with_its_tier_and_confidence(ledger):
    queue = _queue(
        ledger,
        {
            "TXN-GROC": Prediction(
                account="Expenses:Food:Groceries",
                confidence=0.91,
                tier=Tier.MCC,
                rationale="mcc 5411 is Grocery Stores",
            )
        },
    )

    entry = next(e for e in queue.entries if e.simplefin_id == "TXN-GROC")
    assert entry.suggested_account == "Expenses:Food:Groceries"
    assert entry.confidence == pytest.approx(0.91)
    assert entry.tier == "mcc"
    assert entry.rationale == "mcc 5411 is Grocery Stores"
    assert queue.with_suggestion == 1


def test_queue_carries_enough_detail_for_a_human_to_decide(ledger):
    queue = _queue(ledger, {})

    entry = next(e for e in queue.entries if e.simplefin_id == "TXN-GROC")
    assert entry.posted_date == "2026-05-03"
    assert entry.description == "LOCAL GROCER STORE #1133"
    assert entry.amount == "-163.36"
    assert entry.currency == "USD"
    assert entry.asset_account == CHECKING
    assert entry.mcc == "5411"


def test_entries_are_json_serializable_for_phase_4_review_cards(ledger):
    queue = _queue(
        ledger,
        {"TXN-GROC": Prediction(account="Expenses:Food:Groceries", confidence=0.91, tier=Tier.MCC)},
    )

    encoded = json.dumps(queue.to_dict())
    decoded = json.loads(encoded)

    assert decoded["total"] == 3
    entry = next(e for e in decoded["entries"] if e["simplefin_id"] == "TXN-GROC")
    assert entry["suggested_account"] == "Expenses:Food:Groceries"
    # No Decimal, no date, no beancount object anywhere in the payload.
    assert isinstance(entry["amount"], str)
    assert isinstance(entry["posted_date"], str)


def test_listing_the_queue_never_writes_to_the_ledger(ledger):
    before = _ledger_text()

    _queue(
        ledger,
        {"TXN-GROC": Prediction(account="Expenses:Food:Groceries", confidence=1.0, tier=Tier.MEMORY)},
    )

    assert _ledger_text() == before


def test_queue_honours_limit(ledger):
    queue = _queue(ledger, {}, limit=2)

    assert len(queue.entries) == 2


def test_empty_queue_says_so(bookkeeper_root):
    def runner(**kwargs):
        return run_categorize(transactions=[], context=CONTEXT, cascade=FakeCascade({}), **kwargs)

    queue = review_queue(categorize=runner)

    assert queue.ok
    assert queue.entries == []
    assert "empty" in queue.render()


def test_queue_renders_the_identifying_detail(ledger):
    queue = _queue(
        ledger,
        {"TXN-GROC": Prediction(account="Expenses:Food:Groceries", confidence=0.91, tier=Tier.MCC)},
    )

    rendered = queue.render()

    assert "LOCAL GROCER STORE #1133" in rendered
    assert "-163.36" in rendered
    assert "Expenses:Food:Groceries" in rendered
    assert "no tier answered" in rendered  # the two abstentions


# --------------------------------------------------------------------------
# Confirmation
# --------------------------------------------------------------------------


def test_confirmation_writes_the_ledger_and_teaches_tier_one(ledger):
    learned: list[tuple[str, str]] = []

    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda normalized, account: learned.append((normalized, account)),
    )

    assert result.ok
    assert result.confirmed == 1
    assert result.learned == 1
    # (a) the ledger
    text = _ledger_text()
    assert "Expenses:Food:Groceries" in text
    assert text.count("Expenses:Unknown") == 2
    # (b) memory, so the next run's tier 1 resolves this merchant exactly
    assert learned == [("local grocer store #1133", "Expenses:Food:Groceries")]


def test_confirmation_records_a_human_decision_not_a_model_one(ledger):
    confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    text = _ledger_text()
    assert f'bookkeeper-tier: "{HUMAN_TIER}"' in text
    assert 'bookkeeper-decision: "confirmed"' in text
    assert 'bookkeeper-confidence: "1.0000"' in text


def test_confirmed_transaction_leaves_the_queue(ledger):
    confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    # The queue is derived from what still posts to Expenses:Unknown, so
    # rebuilding it from the rewritten ledger must no longer offer it.
    remaining = [t for t in ledger if t.simplefin_id != "TXN-GROC"]
    queue = _queue(remaining, {})

    assert {e.simplefin_id for e in queue.entries} == {"TXN-BAIT", "TXN-PAY"}


def test_a_batch_of_confirmations_is_one_ledger_pass(ledger):
    learned: list[tuple[str, str]] = []

    result = confirm_categorizations(
        [
            Confirmation(CHECKING, "TXN-GROC", "Expenses:Food:Groceries"),
            Confirmation(CHECKING, "TXN-BAIT", "Expenses:Food:Dining"),
            Confirmation(CHECKING, "TXN-PAY", "Income:Salary"),
        ],
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda n, a: learned.append((n, a)),
    )

    assert result.ok
    assert result.confirmed == 3
    assert result.learned == 3
    assert result.files_written == ("2026.beancount",)  # one file, written once
    assert "Expenses:Unknown" not in _ledger_text()


def test_a_batch_produces_exactly_one_commit(ledger, monkeypatch):
    # §5.3 rule 2: approving 40 review cards is 40 direct API calls but
    # must not be 40 commits -- the undo unit is the batch.
    commits: list[str] = []
    monkeypatch.setattr(
        "bookkeeper.categorize.review.commit_ledger",
        lambda message: commits.append(message) or _FakeCommit(),
    )

    confirm_categorizations(
        [
            Confirmation(CHECKING, "TXN-GROC", "Expenses:Food:Groceries"),
            Confirmation(CHECKING, "TXN-BAIT", "Expenses:Food:Dining"),
        ],
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    assert len(commits) == 1
    assert "Categorize 2 transactions (confirmed by hand)" == commits[0]


class _FakeCommit:
    ok = True
    committed = True

    def render(self) -> str:
        return "committed abc1234"


def test_confirming_to_unknown_is_refused(ledger):
    before = _ledger_text()

    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Unknown",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    assert not result.ok
    assert "not a categorization" in result.render()
    assert _ledger_text() == before


def test_confirming_to_an_account_not_open_in_the_ledger_is_refused(ledger):
    before = _ledger_text()

    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Coffee:Speciality",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    assert not result.ok
    assert "not an account open in the ledger" in result.render()
    assert _ledger_text() == before


def test_confirming_an_unknown_transaction_warns_and_changes_nothing(ledger):
    before = _ledger_text()

    result = confirm_categorization(
        "NO-SUCH-TXN",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    assert result.ok  # not a crash
    assert result.confirmed == 0
    assert result.learned == 0
    assert any("matched no Expenses:Unknown posting" in w for w in result.warnings)
    assert _ledger_text() == before


def test_a_memory_failure_leaves_the_ledger_correct_and_warns(ledger):
    # Order matters: the ledger write lands first. Teaching a decision that
    # never reached the ledger is the worse inconsistency -- the queue would
    # stop offering a transaction that still posts to Unknown.
    def broken(normalized, account):
        raise OSError("data/memory.json is read-only")

    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=broken,
    )

    assert result.ok
    assert result.confirmed == 1
    assert result.learned == 0
    assert "Expenses:Food:Groceries" in _ledger_text()
    assert any("memory was not" in w for w in result.warnings)


def test_confirming_nothing_is_a_clean_no_op(ledger):
    before = _ledger_text()

    result = confirm_categorizations([], commit=False)

    assert result.ok
    assert result.confirmed == 0
    assert _ledger_text() == before


def test_confirmation_works_through_the_real_memory_hooks(ledger):
    """Confirm with no injected normalizer/recorder, exercising the real ones.

    Every other test in this file injects both seams, which is good for
    isolation but left `_default_memory_hooks` -- the code path the CLI and
    the API actually take -- with no coverage at all. It shipped importing
    two names that do not exist (`normalize_description` lives in
    `normalize`, not `memory`, and `record_confirmation` is a method rather
    than a module function), and a green suite said nothing. So this test
    deliberately takes the default path end to end.
    """
    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
    )

    assert result.ok
    assert result.learned == 1
    # The real normalizer strips the trailing store number, so the key that
    # lands in memory is the one a differently-numbered visit to the same
    # store will also produce.
    remembered = json.loads((paths.data_dir() / "memory.json").read_text(encoding="utf-8"))
    assert remembered == {"local grocer store": {"Expenses:Food:Groceries": 1}}


def test_confirmation_feeds_the_real_memory_tier_end_to_end(ledger):
    """The seam most likely to rot: no stubbed normalizer, no stubbed recorder.

    Every other confirmation test injects fakes to stay isolated from
    worker-1's modules. This one wires the real `normalize_description` and
    the real `MemoryCategorizer` together and checks the loop actually
    closes -- that after confirming, tier 1 predicts the same account for
    the same merchant string. That is PLAN.md §6 Phase 4's exit criterion
    ("corrections demonstrably change the next run's predictions") reduced
    to its smallest testable form.
    """
    from bookkeeper.categorize.memory import MemoryCategorizer, memory_path

    txn = next(t for t in ledger if t.simplefin_id == "TXN-GROC")
    before = MemoryCategorizer().predict(txn, CONTEXT)
    assert before is None, "tier 1 must know nothing about this merchant yet"

    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
    )

    assert result.ok
    assert result.confirmed == 1
    assert result.learned == 1
    assert memory_path().exists()

    # A fresh categorizer, reading memory.json off disk, now resolves it.
    after = MemoryCategorizer().predict(txn, CONTEXT)
    assert after is not None
    assert after.account == "Expenses:Food:Groceries"
    assert after.tier is Tier.MEMORY


def test_confirm_result_is_json_serializable(ledger):
    result = confirm_categorization(
        "TXN-GROC",
        CHECKING,
        "Expenses:Food:Groceries",
        commit=False,
        context=CONTEXT,
        transactions=ledger,
        normalizer=str.lower,
        recorder=lambda *a: None,
    )

    assert json.loads(json.dumps(result.to_dict()))["confirmed"] == 1
