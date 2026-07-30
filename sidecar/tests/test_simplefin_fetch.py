from __future__ import annotations

import json
from decimal import Decimal

import pytest

from bookkeeper import paths
from bookkeeper.simplefin.fetch import FetchError, fetch_accounts

ACCESS_URL = "https://demo:demopass@bridge.example.com/simplefin"

SAMPLE_PAYLOAD = {
    "errlist": [],
    "accounts": [
        {
            "id": "ACT-1",
            "name": "Checking",
            "conn_id": "CONN-1",
            "currency": "USD",
            "balance": "1234.56",
            "balance-date": 1_700_000_000,
            "transactions": [
                {
                    "id": "TXN-1",
                    "posted": 1_699_900_000,
                    "amount": "-42.10",
                    "description": "SQ *COFFEE 4TH ST",
                }
            ],
        }
    ],
}


def test_fetch_accounts_parses_response(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD
    )

    account_set = fetch_accounts(ACCESS_URL)

    assert len(account_set.accounts) == 1
    account = account_set.accounts[0]
    assert account.balance == Decimal("1234.56")
    assert account.transactions[0].amount == Decimal("-42.10")


def test_fetch_accounts_archives_raw_response_before_parsing(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD
    )

    fetch_accounts(ACCESS_URL)

    archived = list(paths.raw_dir().glob("simplefin-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8")) == SAMPLE_PAYLOAD


def test_fetch_accounts_sends_date_window_as_query_params(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts?start-date=1000&end-date=2000",
        method="GET",
        json=SAMPLE_PAYLOAD,
    )

    fetch_accounts(ACCESS_URL, start_date=1000, end_date=2000)


def test_fetch_accounts_never_requests_more_than_once(bookkeeper_root, httpx_mock):
    # One call must cover every account -- the rate limit is ~24/day
    # (PLAN.md 3.1). This asserts the client issues exactly one request,
    # since pytest-httpx fails on unmatched or extra requests by default.
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD
    )
    fetch_accounts(ACCESS_URL)
    assert len(httpx_mock.get_requests()) == 1


def test_fetch_accounts_raises_on_non_200(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", status_code=500)

    with pytest.raises(FetchError):
        fetch_accounts(ACCESS_URL)


def test_fetch_accounts_error_never_includes_access_url(bookkeeper_root, httpx_mock):
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", status_code=500)

    with pytest.raises(FetchError) as exc_info:
        fetch_accounts(ACCESS_URL)

    assert "demopass" not in str(exc_info.value)
    assert ACCESS_URL not in str(exc_info.value)
