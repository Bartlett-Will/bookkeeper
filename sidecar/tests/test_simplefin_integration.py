"""One live integration test against the real SimpleFIN demo server.

Skipped by default -- set RUN_SIMPLEFIN_INTEGRATION=1 to opt in. Unlike
every other test in this suite, this one makes real network calls, so its
outcome depends on a third party's current infrastructure, not just on our
code.

As of 2026-07-30 this fails: bridge.simplefin.org 302-redirects the claim
endpoint to a bare beta-bridge.simplefin.org *root*, dropping the request
path entirely, so the documented claim flow (simplefin.org/protocol.html)
cannot currently complete against the live host -- reported to team-lead
as a discrepancy between docs and the live demo bridge, not a bug in
`claim_token`/`fetch_accounts`. `test_simplefin_claim.py` pins the exact
observed live behavior (a 302 to a path-less root) as a mocked contract
test and asserts it surfaces as a loud `ClaimError`, not a silent
workaround.
"""

from __future__ import annotations

import os

import pytest

from bookkeeper.simplefin.claim import DEMO_TOKEN, claim_token
from bookkeeper.simplefin.fetch import fetch_accounts

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_SIMPLEFIN_INTEGRATION") != "1",
    reason="hits the real SimpleFIN demo server; set RUN_SIMPLEFIN_INTEGRATION=1 to run",
)


def test_claim_and_fetch_against_real_demo_server(bookkeeper_root):
    access_path = claim_token(DEMO_TOKEN)
    assert access_path.exists()
    access_url = access_path.read_text(encoding="utf-8").strip()

    account_set = fetch_accounts(access_url)
    assert len(account_set.accounts) > 0
