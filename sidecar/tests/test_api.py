"""API-shape tests.

The categorize/review endpoints are covered against mocked module functions
rather than a live cascade: what is being asserted here is the HTTP contract
(status codes, defaults, cache invalidation), not whether the categorizer is
any good -- that is measured by the eval harness.

The fakes below mirror the *real* dataclasses field for field, because the
responses are declared pydantic models: a stand-in that returns a shape the
model does not accept now fails as a 500 rather than passing silently, which
is exactly the drift the models exist to catch. Endpoints that only read the
ledger (search, spending, the categorizable account set) are driven against
the committed fixtures instead, since there is nothing worth mocking.
"""

from __future__ import annotations

import os
import shutil
import sys
import types
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import beancount
import pytest
from fastapi.testclient import TestClient

import bookkeeper.api as api_module
from bookkeeper import paths
from bookkeeper.api import app

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_health_reports_status_and_beancount_version():
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["beancount_version"] == beancount.__version__


def test_sync_endpoint_fails_cleanly_without_access_url(bookkeeper_root):
    client = TestClient(app)
    resp = client.post("/sync", json={"demo": False})
    assert resp.status_code == 502
    assert "claim" in resp.json()["detail"].lower()


def test_sync_endpoint_runs_full_pipeline(bookkeeper_root, httpx_mock):
    access_url = "https://demo:demopass@bridge.example.com/simplefin"
    dest = paths.access_url_file()
    dest.write_text(access_url, encoding="utf-8")
    os.chmod(dest, 0o600)

    httpx_mock.add_response(
        url=access_url + "/accounts",
        method="GET",
        json={
            "errlist": [],
            "accounts": [
                {
                    "id": "ACT-1",
                    "name": "Checking",
                    "currency": "USD",
                    "balance": "100.00",
                    "balance-date": 1_700_000_000,
                    "transactions": [
                        {
                            "id": "TXN-1",
                            "posted": 1_699_900_000,
                            "amount": "-5.00",
                            "description": "x",
                        }
                    ],
                }
            ],
        },
    )

    client = TestClient(app)
    resp = client.post("/sync", json={"demo": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["transactions_added"] == 1


def test_accounts_endpoint_reports_missing_main_ledger_gracefully(bookkeeper_root):
    client = TestClient(app)
    resp = client.get("/accounts")
    assert resp.status_code == 200
    body = resp.json()
    assert body["accounts"] == []
    assert "note" in body


# --- envelopes / verify ---------------------------------------------------


def test_envelopes_endpoint_returns_report_with_overspend(fixture_root):
    fixture_root("basic")
    client = TestClient(app)
    resp = client.get("/envelopes", params={"asof": "2026-01-31"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["asof"] == "2026-01-31"
    assert Decimal(body["available"]) == Decimal("3300.00")
    assert Decimal(body["total_overspend"]) == Decimal("30.00")
    assert Decimal(body["available"]) <= Decimal(body["budgeted_cash"])

    utilities = next(e for e in body["envelopes"] if e["name"] == "Utilities")
    assert utilities["overspent"] is True
    assert Decimal(utilities["overspend"]) == Decimal("30.00")

    groceries = next(e for e in body["envelopes"] if e["name"] == "Groceries")
    assert groceries["overspent"] is False
    assert "Available to budget" in body["summary"]


def test_envelopes_endpoint_rejects_a_non_date_asof(fixture_root):
    fixture_root("basic")
    client = TestClient(app)
    resp = client.get("/envelopes", params={"asof": "last tuesday"})
    assert resp.status_code == 422
    assert "ISO date" in resp.json()["detail"]


def test_verify_endpoint_reports_a_failing_ledger_as_200(fixture_root):
    """A ledger that fails its checks is a successful request with findings --
    5xx stays reserved for the sidecar itself being broken."""
    fixture_root("unmapped_account")
    client = TestClient(app)
    resp = client.get("/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert any("Expenses:Misc:Other" in e for e in body["errors"])
    assert body["summary"].startswith("verify: FAILED")


def test_verify_endpoint_surfaces_overspend_as_a_note_not_an_error(fixture_root):
    fixture_root("overspent_envelope")
    client = TestClient(app)
    resp = client.get("/verify")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["errors"] == []
    assert any("overspent by 30.00" in n for n in body["notes"])


# --- categorize / review-queue -------------------------------------------


@dataclass(frozen=True)
class _FakeEntry:
    """Mirrors `categorize.review.ReviewEntry`, primitives and all."""

    simplefin_id: str
    asset_account: str = "Assets:SimpleFIN:Checking"
    posted_date: str = "2026-07-02"
    description: str = "SQ *COFFEE"
    amount: str = "-4.50"
    currency: str = "USD"
    current_account: str = "Expenses:Unknown"
    suggested_account: str | None = None
    confidence: float | None = None
    tier: str | None = None
    rationale: str = ""
    mcc: str | None = None
    payee: str | None = None


@dataclass
class _FakeQueue:
    ok: bool = True
    entries: list[_FakeEntry] = field(default_factory=list)
    total: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        return f"{len(self.entries)} transaction(s) awaiting review"


@dataclass(frozen=True)
class _FakePolicy:
    threshold: float | None = None
    source: str = "default"


@dataclass(frozen=True)
class _FakeDecision:
    simplefin_id: str
    asset_account: str = "Assets:SimpleFIN:Checking"
    posted_date: str = "2026-07-02"
    description: str = "SQ *COFFEE"
    amount: str = "-4.50"
    currency: str = "USD"
    disposition: str = "auto-apply-off"
    suggested_account: str | None = "Expenses:Food:Dining"
    confidence: float | None = 0.82
    tier: str | None = "memory"
    rationale: str = ""
    mcc: str | None = None
    payee: str | None = None


@dataclass
class _FakeCategorizeResult:
    ok: bool = True
    policy: _FakePolicy = field(default_factory=_FakePolicy)
    applied: bool = False
    decisions: list[_FakeDecision] = field(default_factory=list)
    files_written: tuple[str, ...] = ()
    commit: None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        return f"predicted {len(self.decisions)}, applied {int(self.applied)}"


@pytest.fixture
def fake_categorize(monkeypatch):
    """Install a stand-in for a `bookkeeper.categorize.*` module.

    The endpoints import those modules at request time precisely so they can
    be absent; injecting into `sys.modules` exercises that seam and keeps
    these tests independent of the real cascade's state.
    """

    def _install(module: str, **attrs):
        name = f"bookkeeper.categorize.{module}"
        stand_in = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(stand_in, key, value)
        monkeypatch.setitem(sys.modules, name, stand_in)
        return stand_in

    return _install


def test_review_queue_endpoint_passes_the_queue_through(fake_categorize):
    captured = {}

    def review_queue(limit=None):
        captured["limit"] = limit
        entry = _FakeEntry(simplefin_id="TXN-1", suggested_account="Expenses:Food:Dining")
        return _FakeQueue(entries=[entry], total=1)

    fake_categorize("review", review_queue=review_queue)

    client = TestClient(app)
    resp = client.get("/review-queue", params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()

    assert captured["limit"] == 5
    assert body["ok"] is True
    assert body["summary"] == "1 transaction(s) awaiting review"
    # Structural pass-through: the queue's own fields, JSON-encoded.
    assert body["queue"]["total"] == 1
    entry = body["queue"]["entries"][0]
    assert entry["description"] == "SQ *COFFEE"
    # The field `POST /review/confirm` batches on. Typed, not `unknown`.
    assert entry["simplefin_id"] == "TXN-1"
    assert entry["suggested_account"] == "Expenses:Food:Dining"
    # Amounts stay strings all the way to the browser -- never a JSON double.
    assert entry["amount"] == "-4.50"


def test_review_queue_response_is_typed_not_a_bare_dict():
    """The `queue` field must generate as a real TypeScript type.

    `worker-2` derives the web layer's types from `/openapi.json`. A
    `dict[str, Any]` here becomes `{[key: string]: unknown}` there, and a
    typo in `simplefin_id` then confirms nothing while the UI reports
    success -- against the user's own financial records.
    """
    schema = app.openapi()["components"]["schemas"]
    queue_ref = schema["ReviewQueueResponse"]["properties"]["queue"]
    assert "$ref" in queue_ref, queue_ref
    entry_properties = schema["ReviewEntryModel"]["properties"]
    assert "simplefin_id" in entry_properties
    assert set(schema["ReviewQueueModel"]["required"]) == {
        "ok",
        "entries",
        "total",
        "errors",
        "warnings",
    }


def test_review_queue_endpoint_defaults_limit_to_none(fake_categorize):
    captured = {}

    def review_queue(limit=None):
        captured["limit"] = limit
        return _FakeQueue()

    fake_categorize("review", review_queue=review_queue)

    client = TestClient(app)
    assert client.get("/review-queue").status_code == 200
    assert captured["limit"] is None


def test_categorize_defaults_to_a_dry_run(fake_categorize):
    """The single most important assertion in this file: an unqualified POST
    must not write the ledger (PLAN.md §7)."""
    captured = {}

    def run_categorize(apply=True, limit=None, use_llm=True):
        captured.update(apply=apply, limit=limit, use_llm=use_llm)
        return _FakeCategorizeResult(
            decisions=[_FakeDecision(simplefin_id=f"TXN-{n}") for n in range(3)]
        )

    fake_categorize("apply", run_categorize=run_categorize)

    client = TestClient(app)
    resp = client.post("/categorize", json={})
    assert resp.status_code == 200
    assert captured["apply"] is False
    assert captured["limit"] is None
    assert captured["use_llm"] is True

    body = resp.json()
    assert body["applied"] is False
    assert len(body["result"]["decisions"]) == 3
    # "auto-apply is OFF" is the headline fact about a dry run, not a footnote.
    assert body["result"]["policy"]["threshold"] is None
    assert body["summary"] == "predicted 3, applied 0"


def test_categorize_forwards_its_options(fake_categorize):
    captured = {}

    def run_categorize(apply=False, limit=None, use_llm=True):
        captured.update(apply=apply, limit=limit, use_llm=use_llm)
        return _FakeCategorizeResult(
            applied=True,
            decisions=[_FakeDecision(simplefin_id=f"TXN-{n}") for n in range(2)],
            files_written=("2026.beancount",),
        )

    fake_categorize("apply", run_categorize=run_categorize)

    client = TestClient(app)
    resp = client.post("/categorize", json={"apply": True, "limit": 2, "use_llm": False})
    assert resp.status_code == 200
    assert captured == {"apply": True, "limit": 2, "use_llm": False}
    body = resp.json()
    assert body["applied"] is True
    assert body["result"]["files_written"] == ["2026.beancount"]


def test_categorize_invalidates_the_ledger_cache_only_when_applying(
    fake_categorize, monkeypatch
):
    import bookkeeper.api as api_module

    invalidations = []
    monkeypatch.setattr(
        api_module._ledger_cache, "invalidate", lambda: invalidations.append(1)
    )
    fake_categorize("apply", run_categorize=lambda **kw: _FakeCategorizeResult())

    client = TestClient(app)
    client.post("/categorize", json={"apply": False})
    assert invalidations == []

    client.post("/categorize", json={"apply": True})
    assert len(invalidations) == 1


def test_categorize_invalidates_the_cache_even_when_the_run_fails(
    fake_categorize, monkeypatch
):
    """A run that wrote some transactions and then raised has still changed
    the ledger, so the cache must not be left holding pre-write entries."""
    import bookkeeper.api as api_module

    invalidations = []
    monkeypatch.setattr(
        api_module._ledger_cache, "invalidate", lambda: invalidations.append(1)
    )

    def run_categorize(**kwargs):
        raise RuntimeError("ledger write failed halfway")

    fake_categorize("apply", run_categorize=run_categorize)

    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/categorize", json={"apply": True})
    assert resp.status_code == 500
    assert invalidations == [1]


@pytest.mark.parametrize(
    ("method", "path"),
    [("get", "/review-queue"), ("post", "/categorize")],
)
def test_categorize_endpoints_503_when_the_module_is_absent(method, path, monkeypatch):
    """A missing categorization module must degrade to one 503 on one
    endpoint, not take the app (and `/health`) down at import time."""
    for module in ("bookkeeper.categorize.review", "bookkeeper.categorize.apply"):
        monkeypatch.setitem(sys.modules, module, None)

    client = TestClient(app)
    resp = client.request(method, path, json={} if method == "post" else None)
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"]

    # The app itself is still up.
    assert client.get("/health").status_code == 200


def test_categorize_endpoint_503_when_the_function_is_not_defined_yet(fake_categorize):
    fake_categorize("review")  # module present, `review_queue` missing

    client = TestClient(app)
    resp = client.get("/review-queue")
    assert resp.status_code == 503
    assert "review_queue is not defined" in resp.json()["detail"]


# --- open account set (the review UI's correction dropdown) ---------------


def test_categorizable_accounts_are_the_set_the_cascade_predicts_into(fixture_root):
    """The dropdown's options and the categorizer's label set are one set.

    `/accounts` answers a different question and filters on `Assets:`, which
    is the wrong list for a correction dropdown. If these two definitions of
    "a valid account" could drift, the UI could offer an account the cascade
    would never suggest -- and `POST /review/confirm` rejects the *whole*
    batch over one such account.
    """
    fixture_root("basic")
    client = TestClient(app)
    resp = client.get("/accounts/categorizable")
    assert resp.status_code == 200

    accounts = resp.json()["accounts"]
    assert accounts == [
        "Expenses:Food:Dining",
        "Expenses:Food:Groceries",
        "Expenses:Housing:Rent",
        "Expenses:Transport:Gas",
        "Expenses:Utilities:Electric",
        "Expenses:Utilities:Water",
        "Income:Salary",
    ]
    assert not any(a.startswith(("Assets:", "Equity:")) for a in accounts)
    assert "Expenses:Unknown" not in accounts


def test_accounts_and_categorizable_accounts_answer_different_questions(fixture_root):
    fixture_root("basic")
    client = TestClient(app)
    asset_accounts = [a["account"] for a in client.get("/accounts").json()["accounts"]]
    assert "Assets:Checking" in asset_accounts
    assert set(asset_accounts).isdisjoint(client.get("/accounts/categorizable").json()["accounts"])


def test_accounts_balances_stay_strings(fixture_root):
    """A balance is money. It must never arrive as a JSON double."""
    fixture_root("basic")
    body = TestClient(app).get("/accounts").json()
    checking = next(a for a in body["accounts"] if a["account"] == "Assets:Checking")
    assert checking["balance"][0]["number"] == "4877.00"


# --- review/confirm -------------------------------------------------------


@dataclass(frozen=True)
class _FakeConfirmation:
    asset_account: str
    simplefin_id: str
    account: str


@dataclass
class _FakeConfirmResult:
    ok: bool = True
    confirmed: int = 0
    learned: int = 0
    files_written: tuple[str, ...] = ()
    commit: None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        return f"confirmed {self.confirmed} transaction(s)"


@dataclass(frozen=True)
class _FakeContext:
    accounts: tuple[str, ...] = ()


@pytest.fixture
def fake_confirm(fake_categorize):
    """Install a `confirm_categorizations` stand-in and capture its arguments.

    The `context` argument is captured deliberately: without it
    `confirm_categorizations` skips its open-account check entirely, so an
    endpoint that forgot to pass it would look identical in every other
    assertion while quietly letting a typo'd account into the ledger.
    """
    captured: dict = {}

    def _install(result: _FakeConfirmResult):
        def confirm_categorizations(confirmations, **kwargs):
            captured["confirmations"] = list(confirmations)
            captured.update(kwargs)
            return result

        fake_categorize(
            "review",
            confirm_categorizations=confirm_categorizations,
            Confirmation=_FakeConfirmation,
        )
        fake_categorize("context", build_ledger_context=lambda entries: _FakeContext())
        return captured

    return _install


def test_review_confirm_sends_the_whole_batch_in_one_call(fake_confirm, bookkeeper_root):
    """§5.3 rule 2: forty Accept clicks are one ledger pass and one commit.

    A per-confirmation loop here would produce forty commits and make
    `git revert` useless as the undo for a bad batch.
    """
    captured = fake_confirm(_FakeConfirmResult(confirmed=3, learned=3))

    client = TestClient(app)
    resp = client.post(
        "/review/confirm",
        json={
            "confirmations": [
                {
                    "asset_account": "Assets:SimpleFIN:Checking",
                    "simplefin_id": f"TXN-{n}",
                    "account": "Expenses:Food:Dining",
                }
                for n in range(3)
            ]
        },
    )
    assert resp.status_code == 200
    assert len(captured["confirmations"]) == 3
    assert [c.simplefin_id for c in captured["confirmations"]] == ["TXN-0", "TXN-1", "TXN-2"]

    body = resp.json()
    assert body["ok"] is True
    assert body["confirmed"] == 3
    assert body["learned"] == 3


def test_review_confirm_passes_the_ledger_context_so_the_open_check_runs(
    fake_confirm, bookkeeper_root
):
    captured = fake_confirm(_FakeConfirmResult(confirmed=1, learned=1))

    TestClient(app).post(
        "/review/confirm",
        json={
            "confirmations": [
                {
                    "asset_account": "Assets:SimpleFIN:Checking",
                    "simplefin_id": "TXN-1",
                    "account": "Expenses:Food:Dining",
                }
            ]
        },
    )
    assert isinstance(captured.get("context"), _FakeContext), (
        "no context was passed; confirm_categorizations' open-account check is inert "
        "and a typo'd account would reach the ledger"
    )


def test_review_confirm_reports_a_rejected_batch_as_200_with_reasons(
    fake_confirm, bookkeeper_root
):
    """A refused correction is a finding, not a server failure.

    The UI has to tell the user *which* account it refused. Flattening that
    into a 500 would make "your correction is invalid" indistinguishable
    from "the sidecar fell over".
    """
    fake_confirm(
        _FakeConfirmResult(
            ok=False, errors=["'Expenses:Groccerys' is not an account open in the ledger"]
        )
    )

    resp = TestClient(app).post(
        "/review/confirm",
        json={
            "confirmations": [
                {
                    "asset_account": "Assets:SimpleFIN:Checking",
                    "simplefin_id": "TXN-1",
                    "account": "Expenses:Groccerys",
                }
            ]
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["confirmed"] == 0
    assert "Groccerys" in body["errors"][0]


def test_review_confirm_invalidates_the_ledger_cache(fake_confirm, bookkeeper_root, monkeypatch):
    fake_confirm(_FakeConfirmResult(confirmed=1))
    invalidations = []
    monkeypatch.setattr(api_module._ledger_cache, "invalidate", lambda: invalidations.append(1))

    TestClient(app).post(
        "/review/confirm",
        json={
            "confirmations": [
                {
                    "asset_account": "Assets:SimpleFIN:Checking",
                    "simplefin_id": "TXN-1",
                    "account": "Expenses:Food:Dining",
                }
            ]
        },
    )
    assert invalidations == [1]


def test_review_confirm_is_not_a_chat_tool_but_is_reachable_directly(fake_confirm, bookkeeper_root):
    """An empty batch is an honest no-op, not a 422.

    The endpoint is reached by button clicks, and a UI that submits nothing
    should be told nothing happened rather than handed an error to render.
    """
    fake_confirm(_FakeConfirmResult())
    resp = TestClient(app).post("/review/confirm", json={"confirmations": []})
    assert resp.status_code == 200
    assert resp.json()["confirmed"] == 0


# --- background sync ------------------------------------------------------


@pytest.fixture
def inline_jobs(monkeypatch):
    """Swap the module-level registry for one whose jobs run inline.

    Not a fake registry: the real `JobRegistry` with an injected `spawn`, so
    these tests exercise the actual locking, single-flight and terminal-state
    logic rather than a stand-in that agrees with them.
    """
    import bookkeeper.jobs as jobs_module

    def _install(spawn):
        registry = jobs_module.JobRegistry(spawn=spawn)
        monkeypatch.setattr(jobs_module, "registry", registry)
        return registry

    return _install


def _sync_result(**kwargs):
    from bookkeeper.ingest.sync import SyncResult

    return SyncResult(**kwargs)


def test_sync_start_returns_a_job_id_without_waiting(inline_jobs, monkeypatch, bookkeeper_root):
    """§5.3 rule 3: a sync must not run inside the request.

    The work is spawned but never executed here, and the POST still answers
    -- which is the property that keeps a chat turn from stalling for a
    minute behind a network fetch.
    """
    inline_jobs(spawn=lambda run: None)
    monkeypatch.setattr(api_module, "run_sync", lambda **kw: pytest.fail("ran inside the request"))

    resp = TestClient(app).post("/sync/start", json={"demo": True})
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"]
    assert body["kind"] == "sync"
    assert body["started"] is True
    assert body["state"] == "pending"


def test_sync_status_reports_a_finished_job_with_a_typed_result(
    inline_jobs, monkeypatch, bookkeeper_root
):
    inline_jobs(spawn=lambda run: run())
    captured = {}

    def run_sync(since=None, demo=False):
        captured.update(since=since, demo=demo)
        return _sync_result(
            ok=True,
            accounts_synced=2,
            transactions_seen=43,
            transactions_added=40,
            pending_skipped=3,
            balances_written=2,
            opening_balances_written=1,
        )

    monkeypatch.setattr(api_module, "run_sync", run_sync)

    client = TestClient(app)
    job_id = client.post("/sync/start", json={"since": "2026-07-01", "demo": True}).json()["job_id"]
    assert captured == {"since": "2026-07-01", "demo": True}

    resp = client.get(f"/sync/status/{job_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "succeeded"
    assert body["done"] is True
    assert body["progress"] == body["total"] == 2
    assert body["error"] is None
    # Typed, not a free dict: this is what the polling UI renders.
    assert body["result"]["transactions_added"] == 40
    assert body["result"]["pending_skipped"] == 3


def test_a_failed_sync_is_a_finished_job_not_a_disappearance(
    inline_jobs, monkeypatch, bookkeeper_root
):
    """A job that vanished on error would leave the UI polling forever."""
    inline_jobs(spawn=lambda run: run())
    monkeypatch.setattr(
        api_module,
        "run_sync",
        lambda **kw: _sync_result(ok=False, errors=["no Access URL on file; run `claim` first"]),
    )

    client = TestClient(app)
    job_id = client.post("/sync/start", json={}).json()["job_id"]
    body = client.get(f"/sync/status/{job_id}").json()

    assert body["state"] == "failed"
    assert body["done"] is True
    assert body["result"] is None
    assert "claim" in body["error"]


def test_a_second_sync_start_joins_the_running_job(inline_jobs, monkeypatch, bookkeeper_root):
    """SimpleFIN allows ~24 requests a day (§3.1).

    A double-clicked *Sync* is not a wasted thread, it is a meaningful slice
    of the daily budget, so the second call is handed the running job.
    """
    inline_jobs(spawn=lambda run: None)
    monkeypatch.setattr(api_module, "run_sync", lambda **kw: _sync_result(ok=True))

    client = TestClient(app)
    first = client.post("/sync/start", json={"demo": True}).json()
    second = client.post("/sync/start", json={"demo": True}).json()

    assert second["job_id"] == first["job_id"]
    assert second["started"] is False


def test_sync_status_404s_an_unknown_job(inline_jobs, bookkeeper_root):
    inline_jobs(spawn=lambda run: None)
    resp = TestClient(app).get("/sync/status/nope")
    assert resp.status_code == 404
    assert "nope" in resp.json()["detail"]


def test_sync_start_invalidates_the_ledger_cache_after_the_job_writes(
    inline_jobs, monkeypatch, bookkeeper_root
):
    inline_jobs(spawn=lambda run: run())
    monkeypatch.setattr(api_module, "run_sync", lambda **kw: _sync_result(ok=True))
    invalidations = []
    monkeypatch.setattr(api_module._ledger_cache, "invalidate", lambda: invalidations.append(1))

    TestClient(app).post("/sync/start", json={"demo": True})
    assert invalidations == [1]


# --- envelope allocation --------------------------------------------------


@pytest.fixture
def writable_fixture(tmp_path, monkeypatch):
    """A throwaway copy of a committed fixture tree.

    `/envelopes/allocate` appends to `budget.beancount`, and the fixtures
    under `tests/fixtures/` are checked in -- pointing a writing endpoint at
    one would leave the repo dirty and make the next run of every other test
    depend on this one.
    """

    def _use(name: str) -> Path:
        root = tmp_path / name
        shutil.copytree(FIXTURES_DIR / name, root)
        monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
        api_module._ledger_cache.invalidate()
        return root

    return _use


def test_allocate_appends_a_directive_and_reports_available(writable_fixture):
    root = writable_fixture("basic")
    resp = TestClient(app).post(
        "/envelopes/allocate",
        json={"envelope": "Groceries", "amount": "125.50", "allocated_on": "2026-03-01"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True, body["errors"]
    assert body["envelope"] == "Groceries"
    assert body["amount"] == "125.50"
    assert body["allocated_on"] == "2026-03-01"

    budget = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")
    assert '2026-03-01 custom "envelope" "allocate" "Groceries" 125.50 USD' in budget
    # Over-allocation is reported, never prevented -- `verify` judges budgets.
    assert isinstance(body["over_allocated"], bool)
    assert isinstance(body["available"], str)


def test_allocate_refuses_an_unknown_envelope_and_names_the_real_ones(writable_fixture):
    """A 200 with `ok: false`, because `known_envelopes` is the payload.

    An 8B model picks these arguments (§3.3). One that invented "Groccerys"
    needs the real list back to correct itself without another round trip,
    and a 4xx would throw that list away.
    """
    root = writable_fixture("basic")
    before = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")

    resp = TestClient(app).post(
        "/envelopes/allocate", json={"envelope": "Groccerys", "amount": "50.00"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Groceries" in body["known_envelopes"]
    assert (root / "ledger" / "budget.beancount").read_text(encoding="utf-8") == before


def test_allocate_refuses_a_negative_amount(writable_fixture):
    """Negative is refused, not treated as a de-allocation.

    "Take 600 back out of Groceries" is a plausible thing for a model to
    emit while summarizing, and the cost of honouring it is a budget that
    silently drains. `git revert` is the undo (§9).
    """
    root = writable_fixture("basic")
    before = (root / "ledger" / "budget.beancount").read_text(encoding="utf-8")

    body = TestClient(app).post(
        "/envelopes/allocate", json={"envelope": "Groceries", "amount": "-600.00"}
    ).json()
    assert body["ok"] is False
    assert any("positive" in e for e in body["errors"])
    assert (root / "ledger" / "budget.beancount").read_text(encoding="utf-8") == before


# --- transaction search ---------------------------------------------------


def test_search_finds_transactions_by_narration_and_account(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/transactions/search", params={"q": "groceries"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    assert body["total"] == 5
    # One row per funding leg, not per posting -- otherwise every spend
    # would be listed twice.
    assert {m["account"] for m in body["matches"]} == {"Assets:Checking"}
    assert {m["categorized_account"] for m in body["matches"]} == {"Expenses:Food:Groceries"}
    assert {m["envelope"] for m in body["matches"]} == {"Groceries"}
    assert body["matches"][0]["amount"] == "-90.00"


def test_search_treats_the_query_as_a_literal_not_a_regex(fixture_root):
    """`q` is untrusted text from a chat box or an 8B model.

    BQL's `~` compiles a bound parameter as a *pattern*, so `(a+)+$` is a
    catastrophic backtracker and `[` is a 500. Both must come back as an
    ordinary empty result.
    """
    fixture_root("basic")
    client = TestClient(app)
    for hostile in ("(a+)+$", "[", ".*"):
        body = client.get("/transactions/search", params={"q": hostile}).json()
        assert body["ok"] is True, hostile
        assert body["total"] == 0, hostile


def test_search_honours_limit_and_says_when_it_truncated(fixture_root):
    fixture_root("basic")
    body = TestClient(app).get(
        "/transactions/search", params={"q": "groceries", "limit": 2}
    ).json()
    assert len(body["matches"]) == 2
    assert body["total"] == 5
    assert body["truncated"] is True


def test_search_totals_the_matches_and_keeps_the_money_as_strings(fixture_root):
    """The Phase 4 gap: "how much did I spend at X" had no correct tool. The
    total rides in this response rather than becoming a seventh tool (§5.3)."""
    fixture_root("basic")
    body = TestClient(app).get("/transactions/search", params={"q": "groceries"}).json()

    usd = next(t for t in body["amount_totals"] if t["currency"] == "USD")
    assert usd["spent"] == "365.00"
    assert isinstance(usd["net"], str)
    assert isinstance(usd["received"], str)
    assert usd["accounts"] == ["Assets:Checking"]
    assert body["mixed_currency"] is False


def test_search_totals_keep_refunds_out_of_the_spent_figure(fixture_root):
    """The fixture's Dining Out has a 60.00 refund against a 60.00 charge.
    Netting silently would answer "how much did I spend on dining" with a
    number smaller than what was spent."""
    fixture_root("basic")
    body = TestClient(app).get("/transactions/search", params={"q": "dining"}).json()

    usd = next(t for t in body["amount_totals"] if t["currency"] == "USD")
    assert usd["spent"] == "185.00"
    assert usd["received"] == "60.00"
    assert usd["net"] == "125.00"


def test_search_totals_cover_every_match_not_just_the_page(fixture_root):
    """A total over the visible rows would understate the answer exactly when
    the result was large enough for someone to need one."""
    fixture_root("basic")
    client = TestClient(app)
    full = client.get("/transactions/search", params={"q": "groceries"}).json()
    page = client.get("/transactions/search", params={"q": "groceries", "limit": 2}).json()

    assert page["truncated"] is True
    assert len(page["matches"]) == 2
    assert page["amount_totals"] == full["amount_totals"]


def test_search_reports_an_empty_query_as_a_reason_not_an_error(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/transactions/search", params={"q": "   "})
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["errors"] == ["search text is empty"]


# --- spending report ------------------------------------------------------


def test_spending_report_groups_by_envelope_and_period(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get(
        "/reports/spending", params={"from": "2026-01-01", "to": "2026-02-28"}
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    assert body["period"] == "month"
    assert body["currency"] == "USD"
    # Every period in the window, including any with no activity, so a chart's
    # x-axis is the requested range.
    assert body["periods"] == ["2026-01", "2026-02"]

    groceries = next(e for e in body["envelopes"] if e["name"] == "Groceries")
    assert groceries["total"] == "365.00"
    assert [p["period"] for p in groceries["points"]] == ["2026-01", "2026-02"]
    assert groceries["points"][1]["amount"] == "90.00"


def test_spending_report_defaults_to_the_ledgers_own_date_range(fixture_root):
    """Not a wall-clock window: a report of a fixed ledger must not change
    meaning because a day passed."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/spending").json()
    assert body["from_date"] == "2026-01-01"
    assert body["to_date"] == "2026-02-14"


def test_spending_report_rejects_an_unknown_period(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/reports/spending", params={"period": "decade"})
    assert resp.status_code == 422
    assert "period must be one of" in resp.json()["detail"]


def test_spending_report_rejects_an_unparseable_date(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/reports/spending", params={"from": "last tuesday"})
    assert resp.status_code == 422


def test_spending_report_by_year(fixture_root):
    fixture_root("basic")
    body = TestClient(app).get("/reports/spending", params={"period": "year"}).json()
    assert body["periods"] == ["2026"]


# --- budget vs actual ------------------------------------------------------


def test_budget_report_pairs_allocations_with_actual_spending(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get(
        "/reports/budget", params={"from": "2026-01-01", "to": "2026-02-28"}
    )
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    assert body["currency"] == "USD"
    groceries = next(e for e in body["envelopes"] if e["name"] == "Groceries")
    assert groceries["allocated"] == "1200.00"
    assert groceries["spent"] == "365.00"
    assert groceries["remaining"] == "835.00"
    assert groceries["status"] == "within"


def test_budget_report_keeps_money_as_strings_and_the_ratio_as_a_number(fixture_root):
    """The deliberate type asymmetry. Pydantic renders `Decimal` as a JSON
    string and nothing downstream may coerce it; `consumed_ratio` is not
    money and is a real JSON number, so the UI can compare it without
    parsing."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/budget").json()
    groceries = next(e for e in body["envelopes"] if e["name"] == "Groceries")

    assert isinstance(groceries["allocated"], str)
    assert isinstance(groceries["overspend"], str)
    assert isinstance(body["total_overspend"], str)
    assert isinstance(groceries["consumed_ratio"], float)
    assert isinstance(groceries["overspent"], bool)


def test_budget_report_serves_overspent_as_a_flag_not_a_string_to_parse(fixture_root):
    """A browser cannot do `Decimal` arithmetic and must not try, so the
    comparison is made server-side and shipped as a boolean."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/budget").json()

    for line in body["envelopes"]:
        assert line["overspent"] == (Decimal(line["overspend"]) > 0), line["name"]


def test_budget_report_carries_an_undefined_percentage_as_null(fixture_root):
    """A window with no allocations in it: every percentage is undefined, and
    `null` is the only value a client cannot misread as 0% or 100%."""
    fixture_root("basic")
    body = TestClient(app).get(
        "/reports/budget", params={"from": "2026-02-02", "to": "2026-02-28"}
    ).json()

    for line in body["envelopes"]:
        assert line["allocated"] == "0.00" or line["allocated"] == "0"
        assert line["consumed_ratio"] is None, line["name"]


def test_budget_report_defaults_to_the_ledgers_own_date_range(fixture_root):
    fixture_root("basic")
    body = TestClient(app).get("/reports/budget").json()
    assert body["from_date"] == "2026-01-01"
    assert body["to_date"] == "2026-02-14"


def test_budget_report_balances_agree_with_the_envelopes_endpoint(fixture_root):
    """One definition of "what counts as Groceries", not two. A budget chart
    that disagreed with `/envelopes` would be the §5.2 drift in a new shape."""
    fixture_root("basic")
    client = TestClient(app)
    budget = client.get("/reports/budget", params={"to": "2026-02-14"}).json()
    envelopes = client.get("/envelopes", params={"asof": "2026-02-14"}).json()

    assert {e["name"]: e["balance"] for e in budget["envelopes"]} == {
        e["name"]: e["balance"] for e in envelopes["envelopes"]
    }


def test_budget_report_rejects_an_unparseable_date(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/reports/budget", params={"from": "last tuesday"})
    assert resp.status_code == 422


def test_budget_report_reports_a_backwards_window_as_200_with_a_reason(fixture_root):
    """The request was answered; the answer is that it describes no window."""
    fixture_root("basic")
    resp = TestClient(app).get(
        "/reports/budget", params={"from": "2026-03-01", "to": "2026-01-01"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "is after" in resp.json()["errors"][0]


# --- trends and outliers ---------------------------------------------------


def test_trends_report_abstains_rather_than_calling_two_periods_a_trend(fixture_root):
    """The fixture spans two months. `insufficient_data` with a reason, and never
    `flat` -- the distinction is the whole point of the field."""
    fixture_root("basic")
    resp = TestClient(app).get("/reports/trends")
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    for line in body["envelopes"]:
        assert line["direction"] == "insufficient_data", line["name"]
        assert "at least 3" in line["reason"]
        # The abstention is interrogable from the line, not just asserted.
        assert line["periods_observed"] == 2
        assert line["periods_required"] == 3


def test_trends_report_states_the_thresholds_it_applied(fixture_root):
    """The response describes its own method, so a chat summary built on it
    can be checked rather than taken on trust."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/trends").json()

    assert body["min_periods"] == 3
    assert body["min_transactions"] == 5
    assert body["outlier_threshold"] == "3.5"
    assert body["flat_band"] == "0.10"


def test_trends_report_flags_an_unusual_transaction_with_its_arithmetic(fixture_root):
    """The fixture's Dining Out refund is far from that envelope's median.
    Every number needed to recompute the score comes back with the flag."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/trends").json()

    flagged = next(o for o in body["outliers"] if o["envelope"] == "Dining Out")
    assert flagged["description"] == "Refund - overcharged dinner"
    assert flagged["amount"] == "-60.00"
    assert flagged["scale_method"] == "mad"
    assert (
        (Decimal(flagged["amount"]) - Decimal(flagged["median"]))
        / Decimal(flagged["scale"])
    ).quantize(Decimal("0.01")) == Decimal(flagged["score"])


def test_trends_report_says_which_envelopes_it_declined_to_judge(fixture_root):
    """"Nothing unusual" and "not enough data to look" are different answers,
    and a summary that conflated them would be unfalsifiable."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/trends").json()

    declined = {a["envelope"] for a in body["assessments"] if not a["judged"]}
    judged = {a["envelope"] for a in body["assessments"] if a["judged"]}
    assert "Rent" in declined
    assert "Groceries" in judged
    assert all(a["reason"] for a in body["assessments"])


def test_trends_report_keeps_every_statistic_as_a_string(fixture_root):
    """Statistics are computed in `Decimal` in the sidecar. None of them
    crosses to the browser as a float to be finished there."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/trends").json()
    line = body["envelopes"][0]

    assert isinstance(line["slope"], str)
    assert isinstance(line["mean"], str)
    assert isinstance(body["outliers"][0]["score"], str)


def test_trends_report_rejects_an_unparseable_date(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/reports/trends", params={"to": "next year"})
    assert resp.status_code == 422


def test_trends_report_reports_a_backwards_window_as_200_with_a_reason(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get(
        "/reports/trends", params={"from": "2026-03-01", "to": "2026-01-01"}
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert resp.json()["envelopes"] == []


# --- month-end report -----------------------------------------------------


def test_month_end_report_composes_envelopes_budget_and_coverage(fixture_root):
    fixture_root("basic")
    resp = TestClient(app).get("/reports/month-end", params={"month": "2026-01"})
    assert resp.status_code == 200
    body = resp.json()

    assert body["ok"] is True
    assert body["month"] == "2026-01"
    assert body["label"] == "January 2026"
    assert body["from_date"] == "2026-01-01"
    assert body["to_date"] == "2026-01-31"
    assert body["coverage"] == "complete"
    assert body["currency"] == "USD"

    utilities = next(e for e in body["envelopes"] if e["name"] == "Utilities")
    assert utilities["allocated"] == "100.00"
    assert utilities["spent"] == "130.00"
    # Over this month's budget *and* in the red overall. Two different
    # failures, both reported, neither inferred from the other.
    assert utilities["over_budget"] is True
    assert utilities["overspent"] is True


def test_month_end_money_is_strings_and_the_ratio_is_a_number(fixture_root):
    """Money must not round-trip through a float; a consumption ratio is not
    money and is a genuine JSON number."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-01"}).json()

    assert isinstance(body["spent_total"], str)
    assert isinstance(body["total_spend"], str)
    assert isinstance(body["unmapped_total"], str)
    assert isinstance(body["available"], str)
    groceries = next(e for e in body["envelopes"] if e["name"] == "Groceries")
    assert isinstance(groceries["allocated"], str)
    assert groceries["consumed_ratio"] is None or isinstance(
        groceries["consumed_ratio"], float
    )


def test_month_end_defaults_to_the_ledgers_last_month(fixture_root):
    """Not the wall-clock month: a report of a fixed ledger must not become an
    empty one because a day passed."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/month-end").json()
    assert body["month"] == "2026-02"


def test_month_end_rejects_a_month_that_is_not_yyyy_mm(fixture_root):
    """A 422 rather than a guess: unlike a refused allocation there is no
    payload to return, because there is no month to report on."""
    fixture_root("basic")
    resp = TestClient(app).get("/reports/month-end", params={"month": "january"})
    assert resp.status_code == 422
    assert "month must be YYYY-MM" in resp.json()["detail"]


def test_month_end_reports_uncategorized_spending_rather_than_omitting_it(fixture_root):
    """With auto-apply off, a client that rendered only the per-envelope table
    would show a month of real spending as a month of zeros."""
    fixture_root("unmapped_account")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-01"}).json()

    assert body["categorization"] == "partial"
    assert body["unmapped_total"] == "25.00"
    assert body["unmapped_accounts"] == ["Expenses:Misc:Other"]
    assert body["spent_total"] == "40.00"
    assert body["total_spend"] == "65.00"
    assert "PARTIALLY CATEGORIZED" in body["summary"]


def test_month_end_carries_the_coverage_fields_a_card_renders_from(fixture_root):
    """`month: "2026-01"` alone cannot distinguish a finished January from
    four days of it, and a card that renders them identically makes an
    incomplete month look authoritative."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-01"}).json()

    assert body["complete"] is True
    assert body["through"] == "2026-01-31"
    assert body["days_elapsed"] == 31
    assert body["days_in_month"] == 31


def test_month_end_marks_a_month_the_ledger_stops_inside_as_incomplete(fixture_root):
    """The `basic` fixture's last transaction is 2026-02-14, so February is
    over but only half recorded. `complete` must be False and `through` must
    say where the data actually stops."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-02"}).json()

    assert body["complete"] is False
    assert body["through"] == "2026-02-14"
    assert body["days_in_month"] == 28


def test_month_end_counts_uncategorized_transactions_not_just_their_value(fixture_root):
    """Counts are integers and amounts are strings; the two must not merge.
    An amount alone lets a table of zeros pass for a quiet month."""
    fixture_root("unmapped_account")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-01"}).json()

    assert body["uncategorized_count"] == 1
    assert body["categorized_count"] == 1
    assert isinstance(body["uncategorized_count"], int)
    assert isinstance(body["categorized_count"], int)
    # The amount stays a string beside it.
    assert isinstance(body["unmapped_total"], str)


def test_month_end_makes_a_trend_abstention_checkable(fixture_root):
    """`insufficient_data` alone is a verdict a caller must take on trust.
    The period counts are what turn it into a fact."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-02"}).json()

    for env in body["envelopes"]:
        assert env["direction"] in {"up", "down", "flat", "insufficient_data"}
        assert isinstance(env["periods_observed"], int)
        assert isinstance(env["periods_required"], int)
        if env["direction"] == "insufficient_data":
            assert env["direction_reason"]


def test_month_end_carries_the_trend_window_and_what_it_declined_to_judge(fixture_root):
    """"Nothing unusual" has to arrive with its own denominator, so the
    absence of outliers is checkable rather than merely reassuring."""
    fixture_root("basic")
    body = TestClient(app).get("/reports/month-end", params={"month": "2026-02"}).json()

    assert body["trend_from"] is not None
    assert body["trend_to"] is not None
    assert isinstance(body["outliers"], list)
    assert isinstance(body["unjudged"], list)


# --- degradation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "module"),
    [
        ("post", "/sync/start", "bookkeeper.jobs"),
        ("get", "/sync/status/x", "bookkeeper.jobs"),
        ("get", "/transactions/search?q=x", "bookkeeper.reports.search"),
        ("get", "/reports/spending", "bookkeeper.reports.spending"),
        ("get", "/reports/budget", "bookkeeper.reports.budget"),
        ("get", "/reports/trends", "bookkeeper.reports.trends"),
        ("get", "/reports/month-end", "bookkeeper.reports.monthend"),
        ("post", "/envelopes/allocate", "bookkeeper.envelope.allocate"),
        ("get", "/accounts/categorizable", "bookkeeper.categorize.context"),
    ],
)
def test_a_missing_module_degrades_to_one_503(method, path, module, monkeypatch):
    """One broken module must cost one endpoint, not `/health`.

    `/health` is precisely what you reach for when something is down, so a
    module-level import of a half-built layer would take out the one thing
    that could tell you so.
    """
    monkeypatch.setitem(sys.modules, module, None)
    client = TestClient(app)
    resp = client.request(method, path, json={} if method == "post" else None)
    assert resp.status_code in (422, 503)
    if resp.status_code == 503:
        assert "unavailable" in resp.json()["detail"]
    assert client.get("/health").status_code == 200


# --- request validation ---------------------------------------------------


def test_an_unknown_request_field_is_a_422_that_names_it(bookkeeper_root):
    """The bug this closes: `date` instead of `allocated_on` returned 200 and
    recorded *today*.

    Pydantic's default `extra="ignore"` dropped the field silently, so a
    caller asking to backdate an allocation got a wrong date written into a
    financial record with nothing anywhere to say so. An 8B model picks these
    arguments (§3.3) and a near-miss on a field name is exactly what one
    emits.

    The 422 must *name* the field. A bare "validation error" would leave a
    model no better off than the silent drop -- it would know something was
    wrong but not what to change.
    """
    resp = TestClient(app).post(
        "/envelopes/allocate",
        json={"envelope": "Groceries", "amount": "1.00", "date": "2026-07-22"},
    )
    assert resp.status_code == 422

    detail = resp.json()["detail"]
    offending = [d for d in detail if d["type"] == "extra_forbidden"]
    assert offending, detail
    assert offending[0]["loc"] == ["body", "date"], offending
    assert "date" in str(detail)


@pytest.mark.parametrize(
    ("path", "body", "unknown"),
    [
        (
            "/envelopes/allocate",
            {"envelope": "Groceries", "amount": "1.00", "when": "2026-07-22"},
            "when",
        ),
        (
            "/review/confirm",
            {"confirmations": [], "auto_commit": True},
            "auto_commit",
        ),
        ("/sync/start", {"demo": True, "force": True}, "force"),
        ("/categorize", {"apply": False, "dry_run": True}, "dry_run"),
    ],
)
def test_every_request_body_refuses_unknown_fields(bookkeeper_root, path, body, unknown):
    resp = TestClient(app).post(path, json=body)

    assert resp.status_code == 422, resp.json()
    assert any(d["loc"] == ["body", unknown] for d in resp.json()["detail"]), resp.json()


def test_a_typo_inside_a_confirmation_is_caught_too(bookkeeper_root):
    """The nested model matters most of all.

    `simplefin_id` is the key the batch is matched on. Misspelled and
    silently dropped, the confirmation would match nothing, and the user
    would read "I approved 40 transactions and nothing happened" against
    their own financial records.
    """
    resp = TestClient(app).post(
        "/review/confirm",
        json={
            "confirmations": [
                {
                    "asset_account": "Assets:SimpleFIN:Checking",
                    "simplefin_id": "TXN-1",
                    "account": "Expenses:Food:Dining",
                    "simplefin_ids": "TXN-2",
                }
            ]
        },
    )
    assert resp.status_code == 422
    assert any(
        d["loc"] == ["body", "confirmations", 0, "simplefin_ids"] for d in resp.json()["detail"]
    ), resp.json()


def test_a_missing_required_field_is_still_named(bookkeeper_root):
    resp = TestClient(app).post("/envelopes/allocate", json={"amount": "1.00"})

    assert resp.status_code == 422
    assert any(d["loc"] == ["body", "envelope"] for d in resp.json()["detail"]), resp.json()


def test_the_declared_fields_are_all_still_accepted(fake_confirm, bookkeeper_root):
    """The other half: forbidding extras must not reject a valid body.

    Every optional field is sent explicitly, so a rename on our side that
    silently narrowed the accepted set would fail here.
    """
    fake_confirm(_FakeConfirmResult(confirmed=1, learned=1))
    client = TestClient(app)

    confirm = client.post(
        "/review/confirm",
        json={
            "confirmations": [
                {
                    "asset_account": "Assets:SimpleFIN:Checking",
                    "simplefin_id": "TXN-1",
                    "account": "Expenses:Food:Dining",
                }
            ]
        },
    )
    assert confirm.status_code == 200, confirm.json()


def test_forbidden_extras_are_declared_in_the_openapi_schema():
    """So a generated client can refuse the field at compile time rather than
    discovering it as a 422 at runtime."""
    schemas = app.openapi()["components"]["schemas"]
    for name in (
        "AllocateRequest",
        "ConfirmRequest",
        "ConfirmationModel",
        "SyncStartRequest",
        "CategorizeRequest",
        "SyncRequest",
    ):
        assert schemas[name].get("additionalProperties") is False, name


def test_response_models_stay_permissive():
    """Requests only. Forbidding extras on a response would turn a new field
    in an upstream result dict into a 500 on an endpoint that could have
    answered."""
    schemas = app.openapi()["components"]["schemas"]
    for name in ("AllocateResponse", "SyncStatusResponse", "ReviewQueueResponse"):
        assert schemas[name].get("additionalProperties") is not False, name


# --- which ledger am I talking to? ----------------------------------------


def test_health_reports_the_resolved_ledger_root(bookkeeper_root):
    """The field exists because nothing else can answer this question.

    A copy of a ledger returns byte-identical balances, account lists and
    queue totals to the original, so no value-based probe distinguishes a
    throwaway sandbox from the user's real records -- which is precisely the
    distinction you need immediately before a write.
    """
    body = TestClient(app).get("/health").json()

    assert body["root"] == str(bookkeeper_root.resolve())
    assert body["root"] == str(paths.root().resolve())


def test_health_reports_the_real_root_when_the_env_var_is_unset(monkeypatch):
    """An unset `BOOKKEEPER_ROOT` must report the *computed* path, not an
    empty string or a null.

    The whole value of the field is that it answers without the caller
    knowing how the process was configured -- a null here would leave someone
    choosing a POST target exactly as blind as before.
    """
    monkeypatch.delenv("BOOKKEEPER_ROOT", raising=False)
    body = TestClient(app).get("/health").json()

    assert body["root"]
    assert body["root"] == str(paths.root().resolve())
    assert Path(body["root"]).is_absolute()


def test_health_distinguishes_two_ledgers_that_are_otherwise_identical(
    tmp_path, monkeypatch
):
    """The property that matters, stated as a test.

    A byte-for-byte copy of the real ledger is indistinguishable from it on
    every other endpoint. `/health` must separate them, or the endpoint has
    not earned its place.
    """
    client = TestClient(app)

    monkeypatch.delenv("BOOKKEEPER_ROOT", raising=False)
    real_root = client.get("/health").json()["root"]

    copy = tmp_path / "copy-of-the-real-ledger"
    shutil.copytree(paths.ledger_dir(), copy / "ledger")
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(copy))
    copy_root = client.get("/health").json()["root"]

    assert copy_root != real_root, (
        "/health cannot tell a ledger copy from the real one; the field is useless "
        "for the case it exists to serve"
    )
    assert copy_root == str(copy.resolve())
