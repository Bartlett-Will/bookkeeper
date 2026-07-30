from __future__ import annotations

import os

import beancount
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
