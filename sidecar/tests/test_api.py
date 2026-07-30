"""API-shape tests.

The categorize/review endpoints are covered against mocked module functions
rather than a live cascade: what is being asserted here is the HTTP contract
(status codes, defaults, cache invalidation), not whether the categorizer is
any good -- that is measured by the eval harness.
"""

from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass, field
from decimal import Decimal

import beancount
import pytest
from fastapi.testclient import TestClient

from bookkeeper import paths
from bookkeeper.api import app


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


@dataclass
class _FakeQueue:
    ok: bool = True
    entries: list[dict] = field(default_factory=list)

    def render(self) -> str:
        return f"{len(self.entries)} transaction(s) awaiting review"


@dataclass
class _FakeCategorizeResult:
    ok: bool = True
    predicted: int = 0
    applied: int = 0
    total_amount: Decimal = Decimal("0.00")

    def render(self) -> str:
        return f"predicted {self.predicted}, applied {self.applied}"


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
        return _FakeQueue(entries=[{"description": "SQ *COFFEE", "amount": Decimal("4.50")}])

    fake_categorize("review", review_queue=review_queue)

    client = TestClient(app)
    resp = client.get("/review-queue", params={"limit": 5})
    assert resp.status_code == 200
    body = resp.json()

    assert captured["limit"] == 5
    assert body["ok"] is True
    assert body["summary"] == "1 transaction(s) awaiting review"
    # Structural pass-through: the queue's own fields, JSON-encoded.
    assert body["queue"]["entries"][0]["description"] == "SQ *COFFEE"


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
        return _FakeCategorizeResult(predicted=3)

    fake_categorize("apply", run_categorize=run_categorize)

    client = TestClient(app)
    resp = client.post("/categorize", json={})
    assert resp.status_code == 200
    assert captured["apply"] is False
    assert captured["limit"] is None
    assert captured["use_llm"] is True

    body = resp.json()
    assert body["applied"] is False
    assert body["result"]["predicted"] == 3
    assert body["summary"] == "predicted 3, applied 0"


def test_categorize_forwards_its_options(fake_categorize):
    captured = {}

    def run_categorize(apply=False, limit=None, use_llm=True):
        captured.update(apply=apply, limit=limit, use_llm=use_llm)
        return _FakeCategorizeResult(predicted=2, applied=2)

    fake_categorize("apply", run_categorize=run_categorize)

    client = TestClient(app)
    resp = client.post("/categorize", json={"apply": True, "limit": 2, "use_llm": False})
    assert resp.status_code == 200
    assert captured == {"apply": True, "limit": 2, "use_llm": False}
    assert resp.json()["applied"] is True


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
