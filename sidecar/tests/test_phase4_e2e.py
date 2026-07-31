"""Phase 4 exit criteria, measured (PLAN.md §6).

    a full natural-language sync -> review -> correct loop with no
    hand-editing of ledger files; corrections demonstrably change the next
    run's predictions; approving 40 transactions makes zero additional LLM
    calls.

Three claims, of which the third is the one that cannot be argued. "Zero
LLM calls" is a negative: reading the code only ever tells you that the
paths *you thought of* don't call a model. So it is measured on the wire,
with `scripts/ollama_call_counter.py` standing between the process under
test and Ollama, counting every request that arrives. The counter records
calls on arrival rather than on success, so a call still counts when
Ollama is not running -- an attempted LLM call is an LLM call.

Scope, stated plainly: this file measures the **sidecar** half of the
loop. `POST /review/confirm` is the endpoint the UI's Accept buttons hit
(§5.3 rule 2), and everything downstream of it lives here. It cannot, by
itself, prove that the *browser* makes no model call while a human clicks
forty Accept buttons -- Next.js is a separate process with its own
provider. `scripts/phase4_measure_confirm.py` measures that half against
the running stack, and `docs/phase4-verification.md` reports both numbers
separately rather than letting one stand in for the other.

Every test here runs against a generated fixture tree under `tmp_path`
via `BOOKKEEPER_ROOT`. The real `ledger/` is never written to.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from bookkeeper.categorize.apply import run_categorize
from bookkeeper.categorize.memory import MemoryCategorizer, memory_path
from bookkeeper.categorize.normalize import normalize_description
from bookkeeper.categorize.review import Confirmation, confirm_categorizations, review_queue

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_counting_proxy():
    """Import `scripts/ollama_call_counter.py` by path.

    `scripts/` is a directory of executables, not an installed package, and
    making it one purely so a test could import from it would be tail
    wagging dog. The counter is deliberately dependency-free stdlib so this
    is the only awkwardness it costs.
    """
    script = REPO_ROOT / "scripts" / "ollama_call_counter.py"
    spec = importlib.util.spec_from_file_location("ollama_call_counter", script)
    assert spec is not None and spec.loader is not None, f"cannot load {script}"
    module = importlib.util.module_from_spec(spec)
    sys.modules["ollama_call_counter"] = module
    spec.loader.exec_module(module)
    return module


counter_module = _load_counting_proxy()


# ---------------------------------------------------------------------------
# The fixture tree
# ---------------------------------------------------------------------------

#: Forty transactions, as a bank feed would mangle them. Sized at exactly
#: the number the exit criterion names, so the measurement is of the case
#: the plan actually specifies and not a smaller stand-in.
#:
#: `(description, amount, mcc, true_account)`. The MCCs are real codes and
#: several are deliberately absent -- the mix is what makes the review
#: queue contain a spread of confident guesses, weak guesses and
#: abstentions, which is the state a human is actually approving from.
FORTY: tuple[tuple[str, str, str | None, str], ...] = (
    ("SQ *BLUE BOTTLE COFFEE 4471", "-6.75", "5812", "Expenses:Food:Dining"),
    ("SAFEWAY #1842", "-88.12", "5411", "Expenses:Food:Groceries"),
    ("CHEVRON 0093281", "-52.40", "5541", "Expenses:Transport:Gas"),
    ("POS DEBIT TRADER JOES #0188", "-61.09", "5411", "Expenses:Food:Groceries"),
    ("NETFLIX.COM 8663797", "-17.99", "4899", "Expenses:Subscriptions"),
    ("ACH DEBIT - PG&E WEB ONLINE", "-121.55", "4900", "Expenses:Utilities:Electric"),
    ("SQ *BLUE BOTTLE COFFEE 5120", "-4.50", "5812", "Expenses:Food:Dining"),
    ("BERKELEY BOWL WEST", "-43.77", None, "Expenses:Food:Groceries"),
    ("SHELL OIL 57444219", "-48.90", "5541", "Expenses:Transport:Gas"),
    ("SPOTIFY USA 8774031", "-11.99", "4899", "Expenses:Subscriptions"),
    ("CHECKCARD SAFEWAY #1842", "-32.18", "5411", "Expenses:Food:Groceries"),
    ("EAST BAY MUD AUTOPAY", "-58.20", "4900", "Expenses:Utilities:Water"),
    ("PURCHASE AUTHORIZED ON 07/03 CHIPOTLE 2214", "-14.35", "5812", "Expenses:Food:Dining"),
    ("COMCAST CABLE COMM 8009346", "-89.00", "4899", "Expenses:Utilities:Internet"),
    ("SQ *ARIZMENDI BAKERY 3310", "-9.25", "5812", "Expenses:Food:Dining"),
    ("WHOLEFDS BRK 10012", "-74.63", "5411", "Expenses:Food:Groceries"),
    ("76 - EL CERRITO 4432", "-41.02", "5541", "Expenses:Transport:Gas"),
    ("SUTTER HEALTH PALO ALTO", "-125.00", None, "Expenses:Health"),
    ("TST* CHEZ PANISSE 9921", "-96.40", "5812", "Expenses:Food:Dining"),
    ("SAFEWAY #1842", "-19.44", "5411", "Expenses:Food:Groceries"),
    ("RECURRING PAYMENT HULU 8776783", "-18.99", "4899", "Expenses:Subscriptions"),
    ("COSTCO GAS #0471", "-59.88", "5541", "Expenses:Transport:Gas"),
    ("MONTEREY MARKET 2201", "-37.55", "5411", "Expenses:Food:Groceries"),
    ("SQ *BLUE BOTTLE COFFEE 6602", "-5.25", "5812", "Expenses:Food:Dining"),
    ("PG&E WEB ONLINE", "-118.02", "4900", "Expenses:Utilities:Electric"),
    ("CVS/PHARMACY #09912", "-24.10", None, "Expenses:Health"),
    ("ACH DEBIT - EBMUD AUTOPAY", "-61.75", "4900", "Expenses:Utilities:Water"),
    ("PARAMOUNT+ 8779021", "-11.99", None, "Expenses:Subscriptions"),
    ("TRADER JOES #0188", "-52.31", "5411", "Expenses:Food:Groceries"),
    ("SQ *CHEESE BOARD PIZZA 1180", "-27.00", "5812", "Expenses:Food:Dining"),
    ("VALERO 4471102", "-44.16", "5541", "Expenses:Transport:Gas"),
    ("SONIC.NET RECURRING", "-49.99", "4899", "Expenses:Utilities:Internet"),
    ("KAISER PERMANENTE PMT", "-210.00", None, "Expenses:Health"),
    ("SAFEWAY FUEL #1842", "-46.70", "5541", "Expenses:Transport:Gas"),
    ("POS DEBIT BERKELEY BOWL 8891", "-55.19", "5411", "Expenses:Food:Groceries"),
    ("SQ *PHILZ COFFEE 2278", "-7.10", "5812", "Expenses:Food:Dining"),
    ("YOUTUBEPREMIUM 8558362", "-13.99", "4899", "Expenses:Subscriptions"),
    ("ACH DEBIT - PG&E WEB ONLINE", "-99.31", "4900", "Expenses:Utilities:Electric"),
    ("GREAT CLIPS #4402", "-28.00", None, "Expenses:Health"),
    ("TST* SIDEBAR OAKLAND 7712", "-63.85", "5812", "Expenses:Food:Dining"),
)

_ACCOUNTS = """\
;; Generated by tests/test_phase4_e2e.py. Mirrors ledger/accounts.beancount:
;; Expenses:Unknown is deliberately left unmapped to an envelope, which is
;; what makes an uncategorized transaction a verify failure rather than a
;; number that quietly vanishes from the budget view.

2026-01-01 open Equity:Opening-Balances
2026-01-01 open Assets:SimpleFIN:Checking            USD

2026-01-01 open Expenses:Unknown                     USD
2026-01-01 open Income:Unknown                       USD

2026-01-01 open Expenses:Food:Groceries              USD
2026-01-01 open Expenses:Food:Dining                 USD
2026-01-01 open Expenses:Transport:Gas               USD
2026-01-01 open Expenses:Utilities:Electric          USD
2026-01-01 open Expenses:Utilities:Water             USD
2026-01-01 open Expenses:Utilities:Internet          USD
2026-01-01 open Expenses:Subscriptions               USD
2026-01-01 open Expenses:Health                      USD
2026-01-01 open Expenses:Books                       USD

2026-01-01 custom "envelope" "map" "Expenses:Food:Groceries"      "Groceries"
2026-01-01 custom "envelope" "map" "Expenses:Food:Dining"         "Dining Out"
2026-01-01 custom "envelope" "map" "Expenses:Transport:Gas"       "Transport"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Electric"  "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Water"     "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Utilities:Internet"  "Utilities"
2026-01-01 custom "envelope" "map" "Expenses:Subscriptions"       "Subscriptions"
2026-01-01 custom "envelope" "map" "Expenses:Health"              "Health"
2026-01-01 custom "envelope" "map" "Expenses:Books"               "Books"
"""

_BUDGET = """\
2026-07-01 custom "envelope" "allocate" "Groceries"      700.00 USD
2026-07-01 custom "envelope" "allocate" "Dining Out"     300.00 USD
2026-07-01 custom "envelope" "allocate" "Transport"      300.00 USD
2026-07-01 custom "envelope" "allocate" "Utilities"      600.00 USD
2026-07-01 custom "envelope" "allocate" "Subscriptions"  100.00 USD
2026-07-01 custom "envelope" "allocate" "Health"         500.00 USD
2026-07-01 custom "envelope" "allocate" "Books"           50.00 USD
"""

_MAIN = """\
option "title" "Phase 4 verification fixture"
option "operating_currency" "USD"

include "accounts.beancount"
include "budget.beancount"
include "transactions/*.beancount"
include "balances.beancount"
"""

OPENING_BALANCE = Decimal("5000.00")
OPENING_DATE = date(2026, 7, 1)
ASSET_ACCOUNT = "Assets:SimpleFIN:Checking"


def _render_uncategorized(
    simplefin_id: str,
    posted: date,
    description: str,
    amount: str,
    mcc: str | None,
) -> str:
    """One transaction in exactly the shape `ingest.render` emits.

    Written out here rather than imported from `ingest.render` on purpose:
    a verification harness that generates its fixtures with the same code
    it is verifying would pass a rename that broke the real file format.
    `test_fixture_matches_ingest_render_output` pins the two together, so
    the duplication is checked rather than merely tolerated.
    """
    lines = [
        f'{posted.isoformat()} * "{description}"',
        f'  simplefin-id: "{simplefin_id}"',
        f'  simplefin-account: "{ASSET_ACCOUNT}"',
    ]
    if mcc is not None:
        lines.append(f'  simplefin-mcc: "{mcc}"')
    lines.append(f"  {ASSET_ACCOUNT}   {amount} USD")
    lines.append("  Expenses:Unknown")
    lines.append("")
    return "\n".join(lines) + "\n"


def build_fixture_tree(
    root: Path, transactions: tuple[tuple[str, str, str | None, str], ...] = FORTY
) -> Path:
    """A complete BOOKKEEPER_ROOT with `transactions` awaiting review.

    Includes a `balance` assertion derived from the opening balance and the
    transaction amounts, so `bean-check` failing is a real signal about the
    ledger rather than an artifact of a fixture that never balanced.
    """
    ledger = root / "ledger"
    (ledger / "transactions").mkdir(parents=True, exist_ok=True)
    (root / "data").mkdir(parents=True, exist_ok=True)

    (ledger / "main.beancount").write_text(_MAIN, encoding="utf-8")
    (ledger / "accounts.beancount").write_text(_ACCOUNTS, encoding="utf-8")
    (ledger / "budget.beancount").write_text(_BUDGET, encoding="utf-8")

    blocks = [
        (
            f'{OPENING_DATE.isoformat()} * "Opening balance (SimpleFIN backfill)"\n'
            f'  simplefin-account: "{ASSET_ACCOUNT}"\n'
            f'  simplefin-opening: "true"\n'
            f"  {ASSET_ACCOUNT}   {OPENING_BALANCE} USD\n"
            f"  Equity:Opening-Balances\n\n"
        )
    ]
    total = OPENING_BALANCE
    last = OPENING_DATE
    for index, (description, amount, mcc, _account) in enumerate(transactions):
        posted = OPENING_DATE + timedelta(days=1 + index % 27)
        last = max(last, posted)
        total += Decimal(amount)
        blocks.append(_render_uncategorized(f"txn-{index:04d}", posted, description, amount, mcc))
    (ledger / "transactions" / "2026.beancount").write_text("".join(blocks), encoding="utf-8")

    assertion_date = last + timedelta(days=1)
    (ledger / "balances.beancount").write_text(
        f"; Cash truth: double-entry must reproduce the bank's own balance.\n"
        f"{assertion_date.isoformat()} balance {ASSET_ACCOUNT}   {total} USD\n",
        encoding="utf-8",
    )
    return root


def git_init(root: Path) -> None:
    """A real repo in the fixture tree, so auto-commit is exercised, not stubbed.

    `commit_ledger` treats a missing repo as a warning and carries on, so a
    fixture without a repo would let "one commit per batch" pass without a
    single commit ever being made. Identity is set locally because CI and
    fresh machines have no global git identity.
    """
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "harness@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Phase 4 harness"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)


def git_log(root: Path) -> list[str]:
    out = subprocess.run(
        ["git", "log", "--format=%s"], cwd=root, check=True, capture_output=True, text=True
    )
    return [line for line in out.stdout.splitlines() if line]


def git_is_clean(root: Path) -> bool:
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True
    )
    return out.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def phase4_root(tmp_path, monkeypatch) -> Path:
    root = build_fixture_tree(tmp_path)
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
    git_init(root)
    return root


@pytest.fixture
def call_counter(monkeypatch):
    """A live counting proxy, with every Ollama base URL pointed at it.

    Both env vars are set: `BOOKKEEPER_OLLAMA_URL` is the sidecar's LLM
    tier (`categorize/llm.py`) and `OLLAMA_BASE_URL` is the web app's
    provider (`web/lib/ai/providers.ts`). Only the first can take effect
    in-process, but setting both means a test that ever shells out to
    something web-side still routes through the instrument instead of
    silently reaching the real Ollama.

    The proxy is deliberately left pointing at the real upstream rather
    than a stub: if something *does* call a model, it should behave
    normally and be counted, not fail in a way that could be mistaken for
    a different bug.
    """
    proxy = counter_module.CountingProxy(port=0).start()
    monkeypatch.setenv("BOOKKEEPER_OLLAMA_URL", proxy.base_url)
    monkeypatch.setenv("OLLAMA_BASE_URL", proxy.api_url)
    try:
        yield proxy
    finally:
        proxy.stop()


def confirmations_for(queue) -> list[Confirmation]:
    """Accept every entry in `queue` at its true account.

    The true account comes from `FORTY`, not from the entry's suggestion:
    the criterion is about what happens when a *human* approves a batch,
    and a human corrects the wrong guesses rather than rubber-stamping
    them. Confirming the suggestion would also make the memory-tier
    assertions circular.
    """
    truth = {description: account for description, _amt, _mcc, account in FORTY}
    return [
        Confirmation(
            asset_account=entry.asset_account,
            simplefin_id=entry.simplefin_id,
            account=truth[entry.description],
        )
        for entry in queue.entries
    ]


# ---------------------------------------------------------------------------
# The instrument itself
# ---------------------------------------------------------------------------


def test_the_counter_counts_inference_and_ignores_metadata():
    """The measuring device, measured.

    A counter that silently reported zero would make every criterion-3
    result meaningless, and it would look exactly like a pass. So its
    classification is pinned before it is trusted.
    """
    classify = counter_module.classify
    assert classify("/api/chat") == "inference"
    assert classify("/api/generate") == "inference"
    assert classify("/api/embeddings") == "inference"
    assert classify("/v1/chat/completions") == "inference"
    assert classify("/v1/chat/completions?stream=true") == "inference"
    assert classify("/api/tags") == "metadata"
    assert classify("/api/ps") == "metadata"
    assert classify("/") == "other"


def test_the_counter_counts_a_call_that_never_reaches_ollama(monkeypatch):
    """An attempted LLM call counts even when upstream is dead.

    This is the property that makes a zero trustworthy. If the counter only
    recorded successful round trips, a batch that tried forty times against
    a stopped Ollama would report zero -- a false pass in exactly the
    situation where the harness is most likely to be run.
    """
    import urllib.error
    import urllib.request

    with counter_module.CountingProxy(upstream="http://127.0.0.1:1", port=0) as proxy:
        request = urllib.request.Request(
            f"{proxy.base_url}/api/chat",
            data=b'{"model":"qwen3:8b","messages":[]}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        assert caught.value.code == 502
        assert proxy.llm_calls == 1


def test_the_counter_sees_a_real_llm_tier_call(phase4_root, call_counter):
    """A positive control: the instrument is in the sidecar's path.

    Without this, a measured zero is ambiguous between "nothing called a
    model" and "the proxy was never wired in". Here the LLM tier is invoked
    directly and the count must move. The call's *outcome* is irrelevant --
    Ollama may be running, stopped, or missing the model -- because the
    counter records arrival, not success.
    """
    from bookkeeper.categorize.llm import LlmCategorizer
    from bookkeeper.categorize.models import CategorizationInput, LedgerContext

    categorizer = LlmCategorizer()
    assert categorizer.base_url == call_counter.base_url, (
        "LLM tier did not pick up BOOKKEEPER_OLLAMA_URL; the measurement below "
        "would be measuring nothing"
    )
    try:
        categorizer.predict(
            CategorizationInput(
                description="SQ *PEGASUS BOOKS 4471",
                amount=Decimal("-24.00"),
                posted_date=date(2026, 7, 10),
                asset_account=ASSET_ACCOUNT,
                simplefin_id="probe",
            ),
            LedgerContext(accounts=("Expenses:Books", "Expenses:Food:Dining")),
        )
    except Exception:  # noqa: BLE001, S110 - the count is the assertion, not the answer
        pass
    assert call_counter.llm_calls >= 1, "the proxy is not in the LLM tier's path"


def test_fixture_matches_ingest_render_output():
    """The harness's hand-rolled renderer still matches the real one.

    `_render_uncategorized` intentionally duplicates `ingest.render`, so
    that a format change breaks the fixture loudly here instead of quietly
    producing a tree that the categorizer can no longer read.
    """
    from bookkeeper.ingest.normalize import NormalizedTransaction
    from bookkeeper.ingest.render import render_transaction

    real = render_transaction(
        NormalizedTransaction(
            simplefin_id="txn-0000",
            asset_account=ASSET_ACCOUNT,
            description="SQ *BLUE BOTTLE COFFEE 4471",
            amount=Decimal("-6.75"),
            currency="USD",
            posted_date=date(2026, 7, 2),
            mcc="5812",
        )
    )
    mine = _render_uncategorized(
        "txn-0000", date(2026, 7, 2), "SQ *BLUE BOTTLE COFFEE 4471", "-6.75", "5812"
    )
    assert mine == real


# ---------------------------------------------------------------------------
# Criterion 3 -- approving 40 transactions makes zero additional LLM calls
# ---------------------------------------------------------------------------


def test_the_review_queue_holds_forty_transactions(phase4_root):
    queue = review_queue()
    assert queue.ok, queue.errors
    assert queue.total == 40


def test_approving_forty_transactions_makes_zero_llm_calls(phase4_root, call_counter):
    """§5.3 rule 2, measured on the wire rather than argued from the code.

    The full batch is approved through `confirm_categorizations` -- the
    function `POST /review/confirm` calls, and therefore what an Accept
    click reaches once it leaves the browser. Listing the queue first is
    part of the measurement window on purpose: rendering forty review cards
    is also part of "approving 40 transactions" as a user experiences it,
    and §5.3 rule 3 says that path must not run a model either.
    """
    queue = review_queue()
    assert queue.total == 40

    before = call_counter.llm_calls
    result = confirm_categorizations(confirmations_for(queue))
    after = call_counter.llm_calls

    assert result.ok, result.errors
    assert result.confirmed == 40
    assert after - before == 0, (
        f"{after - before} LLM call(s) during a 40-transaction approval: "
        f"{json.dumps(call_counter.log.stats()['by_path'])}"
    )
    assert call_counter.llm_calls == 0, "a model was called while listing the review queue"


def test_approving_forty_transactions_makes_one_commit(phase4_root):
    """One ledger pass and one revertible commit, not forty of each.

    `review.py` promises this and §5.3 rule 2 depends on it: forty commits
    would make `git revert` useless as the undo for a bad batch, which is
    the whole reason the ledger is in git.
    """
    queue = review_queue()
    before = len(git_log(phase4_root))
    result = confirm_categorizations(confirmations_for(queue))

    assert result.confirmed == 40
    assert result.commit is not None and result.commit.committed, result.commit
    assert len(git_log(phase4_root)) - before == 1
    assert result.files_written == ("2026.beancount",)


def test_approving_forty_transactions_teaches_tier_one_forty_times(phase4_root):
    """The learning half of the confirm, which criterion 2 rests on."""
    queue = review_queue()
    result = confirm_categorizations(confirmations_for(queue))

    assert result.learned == 40
    table = json.loads(memory_path().read_text(encoding="utf-8"))
    # Three surface forms of Blue Bottle collapse to one key with three
    # confirmations -- 40 confirmations, fewer than 40 distinct keys.
    assert table["blue bottle coffee"] == {"Expenses:Food:Dining": 3}
    assert len(table) < 40


def test_a_second_approval_pass_finds_nothing_left_and_calls_no_model(phase4_root, call_counter):
    """Idempotency, and the re-render that follows an approval.

    The UI reloads the queue after a batch. That reload is still inside the
    user-visible "approve 40" action, so it is measured too.
    """
    confirm_categorizations(confirmations_for(review_queue()))
    queue = review_queue()
    assert queue.total == 0
    assert call_counter.llm_calls == 0


# ---------------------------------------------------------------------------
# Criterion 2 -- corrections change the next run's predictions
# ---------------------------------------------------------------------------

#: The same merchant, mangled two different ways by the bank. Not a
#: byte-identical repeat: a repeat would only prove that a cache works.
#: These differ in payment-rail prefix, store number, and punctuation, and
#: `normalize_description` collapses both to `pegasus books`.
CORRECTION_SEEN = "SQ *PEGASUS BOOKS 4471"
CORRECTION_NOVEL = "PURCHASE AUTHORIZED ON 07/22 PEGASUS BOOKS #8823"
CORRECTION_ACCOUNT = "Expenses:Books"

#: A third form, with a city token the bank appended. It normalizes to a
#: *different* key, so tier 1 cannot generalize to it. Pinned by
#: `test_the_generalization_stops_where_normalization_stops` so the
#: criterion-2 claim is reported with its real boundary rather than as an
#: unqualified "memory generalizes".
CORRECTION_OUT_OF_REACH = "POS DEBIT PEGASUS BOOKS OAKLAND 8823"

TWO_FORMS: tuple[tuple[str, str, str | None, str], ...] = (
    (CORRECTION_SEEN, "-24.00", None, CORRECTION_ACCOUNT),
    (CORRECTION_NOVEL, "-31.50", None, CORRECTION_ACCOUNT),
)


@pytest.fixture
def correction_root(tmp_path, monkeypatch) -> Path:
    root = build_fixture_tree(tmp_path, transactions=TWO_FORMS)
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
    git_init(root)
    return root


def _suggestion_for(description: str) -> tuple[str | None, str | None]:
    """`(suggested_account, tier)` for one queued transaction, or `(None, None)`."""
    for entry in review_queue().entries:
        if entry.description == description:
            return entry.suggested_account, entry.tier
    return None, None


def test_normalization_collapses_the_two_surface_forms():
    """The premise of the test below, checked separately from the claim.

    If these ever stop collapsing, the criterion-2 test would fail for a
    reason that has nothing to do with the learning loop, and this
    assertion is what says so.
    """
    assert normalize_description(CORRECTION_SEEN) == "pegasus books"
    assert normalize_description(CORRECTION_NOVEL) == "pegasus books"
    assert CORRECTION_SEEN != CORRECTION_NOVEL


def test_a_correction_changes_the_next_runs_prediction(correction_root, call_counter):
    """Criterion 2, as a before/after on a *different* surface form.

    Before: the cascade has never seen this merchant, under either
    spelling. After confirming one form, the *other* form -- differently
    prefixed, differently numbered -- must come back predicted, from the
    memory tier, at the account the human chose. That is the loop closing:
    a human decision generalizing across the bank's mangling, not a cache
    hit on an identical string.
    """
    before_account, before_tier = _suggestion_for(CORRECTION_NOVEL)
    assert before_account != CORRECTION_ACCOUNT, (
        f"the cascade already predicts {CORRECTION_ACCOUNT} for the novel form "
        f"(tier {before_tier}) before any correction -- this test would prove nothing"
    )

    seen = next(e for e in review_queue().entries if e.description == CORRECTION_SEEN)
    result = confirm_categorizations(
        [
            Confirmation(
                asset_account=seen.asset_account,
                simplefin_id=seen.simplefin_id,
                account=CORRECTION_ACCOUNT,
            )
        ]
    )
    assert result.ok and result.confirmed == 1 and result.learned == 1, result.errors

    after_account, after_tier = _suggestion_for(CORRECTION_NOVEL)
    assert after_account == CORRECTION_ACCOUNT
    assert after_tier == "memory"
    assert call_counter.llm_calls == 0, "the correction loop reached for a model"


def test_the_correction_is_recorded_where_the_next_run_reads_it(correction_root):
    """The mechanism behind the test above, pinned separately.

    A passing before/after does not by itself say *why* it passed. This
    asserts the actual artifact -- one confirmation, under the normalized
    key, in `data/memory.json` -- so a future regression can be localized
    to the write or to the read.
    """
    seen = next(e for e in review_queue().entries if e.description == CORRECTION_SEEN)
    confirm_categorizations(
        [
            Confirmation(
                asset_account=seen.asset_account,
                simplefin_id=seen.simplefin_id,
                account=CORRECTION_ACCOUNT,
            )
        ]
    )
    table = json.loads(memory_path().read_text(encoding="utf-8"))
    assert table == {"pegasus books": {CORRECTION_ACCOUNT: 1}}


def test_the_generalization_stops_where_normalization_stops(correction_root):
    """The boundary of the criterion-2 claim, stated as a test.

    `data/memory.json` is keyed on the normalized description, so tier 1
    generalizes exactly as far as `normalize_description` does -- across
    rail prefixes and trailing store numbers, and no further. A bank that
    appends a city token produces a different key and a memory miss. This
    is a real limit of the shipped design, and reporting criterion 2 as
    passing without naming it would overstate what was measured.
    """
    memory = MemoryCategorizer()
    memory.record_confirmation(normalize_description(CORRECTION_SEEN), CORRECTION_ACCOUNT)

    from bookkeeper.categorize.models import CategorizationInput, LedgerContext

    ctx = LedgerContext(accounts=(CORRECTION_ACCOUNT, "Expenses:Food:Dining"))

    def probe(description: str):
        return MemoryCategorizer().predict(
            CategorizationInput(
                description=description,
                amount=Decimal("-31.50"),
                posted_date=date(2026, 7, 22),
                asset_account=ASSET_ACCOUNT,
                simplefin_id="probe",
            ),
            ctx,
        )

    assert probe(CORRECTION_NOVEL) is not None
    assert probe(CORRECTION_OUT_OF_REACH) is None


# ---------------------------------------------------------------------------
# Criterion 1 -- the full loop, with no hand-editing of ledger files
# ---------------------------------------------------------------------------


def bean_check(root: Path) -> tuple[bool, str]:
    """`bean-check` on the fixture ledger. Balance assertions included.

    Run as the real binary rather than through the loader API, because the
    exit criterion is about the ledger being valid to the tool a user would
    reach for, and `bean-check` is what enforces `balance` assertions.
    """
    out = subprocess.run(
        [sys.executable, "-m", "beancount.scripts.check", str(root / "ledger" / "main.beancount")],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode == 2 and "No module named" in out.stderr:
        out = subprocess.run(
            ["bean-check", str(root / "ledger" / "main.beancount")],
            capture_output=True,
            text=True,
            check=False,
        )
    return out.returncode == 0, (out.stdout + out.stderr).strip()


def test_the_fixture_ledger_is_valid_before_the_loop_runs(phase4_root):
    """A control. A clean bean-check after the loop means nothing if the
    ledger was already broken -- or already trivially clean for a reason
    unrelated to the app's writes."""
    ok, output = bean_check(phase4_root)
    assert ok, output


def test_the_full_loop_changes_the_ledger_only_through_the_app(phase4_root, call_counter):
    """Criterion 1: review -> correct, with the ledger edited only by the app.

    "No hand-editing" is evidenced structurally, not asserted: the working
    tree is clean before and after, so every byte that changed is inside a
    commit the app made. The harness writes no ledger file itself after
    `git_init`, and if it did, the clean-tree assertion at the end would
    fail.

    The `sync` leg of the loop is not exercised here -- it needs a live
    SimpleFIN fetch or the demo server, which does not belong in a
    hermetic test. `scripts/phase4_measure_confirm.py` drives sync against
    the running stack, and `docs/phase4-verification.md` reports that leg
    separately.
    """
    assert git_is_clean(phase4_root)
    commits_before = len(git_log(phase4_root))

    queue = review_queue()
    assert queue.total == 40
    result = confirm_categorizations(confirmations_for(queue))
    assert result.ok, result.errors
    assert result.confirmed == 40

    assert git_is_clean(phase4_root), (
        "something outside the app left the working tree dirty; the "
        "no-hand-editing claim cannot be made from this run"
    )
    log = git_log(phase4_root)
    assert len(log) - commits_before == 1
    assert log[0] == "Categorize 40 transactions (confirmed by hand)"

    ok, output = bean_check(phase4_root)
    assert ok, f"ledger invalid after the loop:\n{output}"
    assert call_counter.llm_calls == 0


def test_nothing_still_posts_to_unknown_after_the_loop(phase4_root):
    """The loop's actual product: an empty review queue and no catch-all.

    A clean bean-check is necessary but not sufficient -- a ledger where
    all forty transactions still post to `Expenses:Unknown` also parses
    cleanly. This asserts the transactions actually moved.
    """
    confirm_categorizations(confirmations_for(review_queue()))

    text = (phase4_root / "ledger" / "transactions" / "2026.beancount").read_text(encoding="utf-8")
    assert "Expenses:Unknown" not in text
    assert text.count('bookkeeper-decision: "confirmed"') == 40
    assert review_queue().total == 0

    after = run_categorize(apply=False, use_llm=False)
    assert after.ok
    assert after.considered == 0


def test_the_real_ledger_was_never_touched():
    """A guard on the harness, not on the app.

    Every test above runs under a `BOOKKEEPER_ROOT` fixture, but a missing
    or mistyped `monkeypatch.setenv` would silently redirect writes at the
    developer's real `ledger/`. This asserts the repo's own tree is
    untouched by the time the module finishes.
    """
    out = subprocess.run(
        ["git", "status", "--porcelain", "--", "ledger", "data/memory.json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "", f"the harness modified the real ledger:\n{out.stdout}"
