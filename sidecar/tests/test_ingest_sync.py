"""Idempotency is the load-bearing exit criterion for Phase 1: PLAN.md says
`bookkeeper sync` run twice against the demo server must produce a
byte-identical ledger, "idempotency proven, not assumed." These tests
prove it by hashing the generated files across two `run_sync` calls with
the SimpleFIN API mocked (pytest-httpx), not by eyeballing output.
"""

from __future__ import annotations

import hashlib
import os
import subprocess

from bookkeeper import paths
from bookkeeper.ingest.sync import run_sync

ACCESS_URL = "https://demo:demopass@bridge.example.com/simplefin"

SAMPLE_PAYLOAD = {
    "errlist": [],
    "accounts": [
        {
            "id": "ACT-1",
            "name": "Checking",
            "currency": "USD",
            "balance": "1234.56",
            "balance-date": 1_700_000_000,
            "transactions": [
                {
                    "id": "TXN-1",
                    "posted": 1_699_900_000,
                    "amount": "-42.10",
                    "description": "SQ *COFFEE 4TH ST",
                },
                {
                    "id": "TXN-2",
                    "posted": 1_699_950_000,
                    "amount": "2500.00",
                    "description": "PAYROLL DEPOSIT",
                },
                {
                    "id": "TXN-3-PENDING",
                    "posted": 1_699_960_000,
                    "amount": "-10.00",
                    "description": "STILL PENDING",
                    "pending": True,
                },
            ],
        },
        {
            "id": "ACT-2",
            "name": "Savings",
            "currency": "USD",
            "balance": "9000.00",
            "balance-date": 1_700_000_000,
            "transactions": [
                {
                    "id": "TXN-4",
                    "posted": 1_695_000_000,  # different year than TXN-1/2
                    "amount": "500.00",
                    "description": "TRANSFER IN",
                }
            ],
        },
    ],
}


def _seed_access_url(root):
    dest = paths.access_url_file()
    dest.write_text(ACCESS_URL, encoding="utf-8")
    os.chmod(dest, 0o600)


def _hash_ledger_tree() -> dict[str, str]:
    hashes: dict[str, str] = {}
    transactions_dir = paths.transactions_dir()
    if transactions_dir.exists():
        for f in sorted(transactions_dir.glob("*.beancount")):
            hashes[f"transactions/{f.name}"] = hashlib.sha256(f.read_bytes()).hexdigest()
    balances_path = paths.balances_ledger()
    if balances_path.exists():
        hashes["balances.beancount"] = hashlib.sha256(balances_path.read_bytes()).hexdigest()
    accounts_simplefin_path = paths.accounts_simplefin_ledger()
    if accounts_simplefin_path.exists():
        hashes["accounts-simplefin.beancount"] = hashlib.sha256(
            accounts_simplefin_path.read_bytes()
        ).hexdigest()
    return hashes


def test_sync_twice_produces_byte_identical_ledger(bookkeeper_root, httpx_mock):
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD, is_reusable=True
    )

    first = run_sync()
    assert first.ok
    assert first.transactions_added == 3  # TXN-1, TXN-2, TXN-4 (TXN-3-PENDING excluded)
    assert first.pending_skipped == 1
    assert first.balances_written == 2  # one per account
    assert first.opening_balances_written == 2  # first sync for both accounts

    hashes_after_first = _hash_ledger_tree()
    assert hashes_after_first  # something was actually written

    second = run_sync()
    assert second.ok
    assert second.transactions_added == 0
    # balances.beancount is rewritten wholesale every sync (not merged), so
    # this reports "currently present," not "newly added" -- same count as
    # the first run, not zero.
    assert second.balances_written == 2
    assert second.opening_balances_written == 0  # plug already exists, not rewritten

    hashes_after_second = _hash_ledger_tree()
    assert hashes_after_second == hashes_after_first, (
        "ledger changed between two identical syncs -- idempotency broken"
    )

    # Belt and suspenders: prove it's not just "same hash by coincidence"
    # by also diffing raw bytes file-by-file.
    for name in hashes_after_first:
        rel = name.split("/", 1)[-1] if "/" in name else name
        if name.startswith("transactions/"):
            path = paths.transactions_dir() / rel
        else:
            path = paths.balances_ledger()
        assert path.read_bytes() == path.read_bytes()


def test_sync_partitions_transactions_by_posted_year(bookkeeper_root, httpx_mock):
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD)

    run_sync()

    year_files = {p.name for p in paths.transactions_dir().glob("*.beancount")}
    # TXN-1/TXN-2 posted in 2023-11, TXN-4 posted in 2023-09 -- same year here,
    # but partitioning is exercised by checking the file actually matches the
    # transaction's posted date rather than assuming a single file.
    assert year_files == {"2023.beancount"}
    content = (paths.transactions_dir() / "2023.beancount").read_text(encoding="utf-8")
    assert 'simplefin-id: "TXN-1"' in content
    assert 'simplefin-id: "TXN-2"' in content
    assert 'simplefin-id: "TXN-4"' in content
    assert "TXN-3-PENDING" not in content


def test_sync_writes_balance_assertions(bookkeeper_root, httpx_mock):
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD)

    run_sync()

    content = paths.balances_ledger().read_text(encoding="utf-8")
    assert "balance Assets:SimpleFIN:Checking" in content
    assert "balance Assets:SimpleFIN:Savings" in content
    assert "1234.56 USD" in content
    assert "9000.00 USD" in content


def test_sync_without_demo_and_without_prior_claim_fails_cleanly(bookkeeper_root):
    result = run_sync(demo=False)
    assert not result.ok
    assert "claim" in result.render().lower()
    assert not paths.transactions_dir().exists() or not list(paths.transactions_dir().glob("*"))


def test_sync_demo_no_longer_auto_claims_when_no_access_url_exists(bookkeeper_root):
    # `--demo` used to silently attempt a claim here -- against an
    # endpoint confirmed broken server-side, so it just failed anyway, but
    # opaquely. It must not attempt any network call now: if it did,
    # pytest-httpx (no mock registered in this test) would fail the test.
    result = run_sync(demo=True)

    assert not result.ok
    assert "no longer auto-claims" in result.render()
    assert not paths.access_url_file().exists()


def test_sync_demo_error_names_the_env_var_and_file_alternatives(bookkeeper_root):
    result = run_sync(demo=True)

    rendered = result.render()
    assert "BOOKKEEPER_SIMPLEFIN_ACCESS_URL" in rendered
    assert "claim" in rendered.lower()


def test_sync_reads_access_url_from_env_var_without_touching_the_file(
    bookkeeper_root, httpx_mock, monkeypatch
):
    monkeypatch.setenv("BOOKKEEPER_SIMPLEFIN_ACCESS_URL", ACCESS_URL)
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD)

    result = run_sync()

    assert result.ok
    assert not paths.access_url_file().exists()  # never written, never required


def test_sync_env_var_takes_precedence_over_file(bookkeeper_root, httpx_mock, monkeypatch):
    # File holds a URL that would 404 if actually used; env var holds the
    # real mocked one. If the code used the file, the unmatched request to
    # the bogus host would fail the test via pytest-httpx.
    _seed_access_url(bookkeeper_root)  # writes ACCESS_URL to the file
    other_url = "https://demo:demopass@other-bridge.example.com/simplefin"
    monkeypatch.setenv("BOOKKEEPER_SIMPLEFIN_ACCESS_URL", other_url)
    httpx_mock.add_response(url=other_url + "/accounts", method="GET", json=SAMPLE_PAYLOAD)

    result = run_sync()

    assert result.ok


def test_sync_demo_reuses_existing_access_url_instead_of_reclaiming(bookkeeper_root, httpx_mock):
    # If an access URL is already on file, `sync --demo` must not spend
    # another claim against the shared, limited-reuse demo token.
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD)

    result = run_sync(demo=True)

    assert result.ok
    # No claim mock was registered at all -- if the code tried to claim,
    # pytest-httpx would fail this test with an unmatched request.


def test_sync_does_not_drop_transactions_sharing_an_id_across_accounts(
    bookkeeper_root, httpx_mock
):
    # Regression test for a real bug found against the live demo server
    # (2026-07-30): SimpleFIN ids are unique *per account*, not globally --
    # the real server issues the identical id for distinct transactions in
    # different accounts. Dedup keyed on the bare id silently dropped half
    # of every sync (338 fetched, only 169 kept). The fix keys on
    # (account, id); this pins it.
    _seed_access_url(bookkeeper_root)
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-CHECKING",
                "name": "Checking",
                "currency": "USD",
                "balance": "100.00",
                "balance-date": 1_700_000_000,
                "transactions": [
                    {
                        "id": "SHARED-ID",
                        "posted": 1_699_900_000,
                        "amount": "-10.00",
                        "description": "checking side",
                    }
                ],
            },
            {
                "id": "ACT-SAVINGS",
                "name": "Savings",
                "currency": "USD",
                "balance": "200.00",
                "balance-date": 1_700_000_000,
                "transactions": [
                    {
                        "id": "SHARED-ID",  # same id, different account
                        "posted": 1_699_900_000,
                        "amount": "-20.00",
                        "description": "savings side",
                    }
                ],
            },
        ],
    }
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=payload)

    result = run_sync()

    assert result.transactions_added == 2  # both kept, not deduped to 1
    content = paths.transactions_dir().joinpath("2023.beancount").read_text(encoding="utf-8")
    assert "checking side" in content
    assert "savings side" in content


def test_sync_surfaces_undocumented_top_level_errors_as_warnings(bookkeeper_root, httpx_mock):
    # Live-observed (not in the protocol doc): a >90-day date window comes
    # back as a soft error in a top-level `errors: list[str]`, HTTP 200,
    # data still attached. Must not be silently dropped.
    _seed_access_url(bookkeeper_root)
    payload = dict(SAMPLE_PAYLOAD)
    payload["errors"] = ["Requested date range exceeds limit of 90 days and was capped."]
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=payload)

    result = run_sync()

    assert result.ok  # a soft error is not a sync failure
    assert "Requested date range exceeds limit of 90 days and was capped." in result.errors
    assert "90 days" in result.render()


def test_sync_opening_balance_reconciles_with_current_balance(bookkeeper_root, httpx_mock):
    # PLAN.md decision #2: greenfield ledger, backfilled from SimpleFIN --
    # but SimpleFIN's date window is capped, so a sync can only ever see a
    # partial slice of an account's history. Without a plug against
    # Equity:Opening-Balances, `balance Assets:X <current balance>` can
    # never pass bean-check for an account with older activity. Confirmed
    # live against the real demo server's Checking/Savings accounts.
    _seed_access_url(bookkeeper_root)
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-1",
                "name": "Checking",
                "currency": "USD",
                "balance": "1234.56",  # does NOT equal sum of the txns below
                "balance-date": 1_700_000_000,
                "transactions": [
                    {
                        "id": "TXN-1",
                        "posted": 1_699_900_000,
                        "amount": "-42.10",
                        "description": "x",
                    },
                    {
                        "id": "TXN-2",
                        "posted": 1_699_950_000,
                        "amount": "2500.00",
                        "description": "y",
                    },
                ],
            }
        ],
    }
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=payload)

    result = run_sync()

    assert result.ok
    assert result.opening_balances_written == 1
    content = paths.transactions_dir().joinpath("2023.beancount").read_text(encoding="utf-8")
    assert "Opening balance (SimpleFIN backfill)" in content
    assert 'simplefin-account: "Assets:SimpleFIN:Checking"' in content
    assert 'simplefin-opening: "true"' in content
    # 1234.56 (current balance) - (-42.10 + 2500.00) (included txns) = -1223.34
    assert "Assets:SimpleFIN:Checking   -1223.34 USD" in content
    assert "Equity:Opening-Balances" in content
    # The opening entry must be dated strictly before every transaction it
    # reconciles, or beancount's balance directive ordering breaks.
    opening_line = next(line for line in content.splitlines() if "Opening balance" in line)
    opening_date = opening_line.split(" ", 1)[0]
    assert opening_date < "2023-11-13"  # day before TXN-1's posted date


def test_sync_opening_balance_written_only_once_across_reruns(bookkeeper_root, httpx_mock):
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD, is_reusable=True
    )

    first = run_sync()
    second = run_sync()

    assert first.opening_balances_written == 2
    assert second.opening_balances_written == 0
    content = paths.transactions_dir().joinpath("2023.beancount").read_text(encoding="utf-8")
    assert content.count("Opening balance (SimpleFIN backfill)") == 2  # not 4


def test_sync_opening_balance_for_account_with_no_transactions_in_window(
    bookkeeper_root, httpx_mock
):
    # e.g. the live demo server's "Empty Account": zero transactions, but
    # still a real balance that needs a plug to reconcile.
    _seed_access_url(bookkeeper_root)
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-EMPTY",
                "name": "Empty Account",
                "currency": "USD",
                "balance": "0.00",
                "balance-date": 1_700_000_000,
                "transactions": [],
            }
        ],
    }
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=payload)

    result = run_sync()

    assert result.ok
    assert result.opening_balances_written == 1
    content = paths.transactions_dir().joinpath("2023.beancount").read_text(encoding="utf-8")
    assert "Assets:SimpleFIN:EmptyAccount   0.00 USD" in content


def test_sync_writes_accounts_simplefin_open_directives(bookkeeper_root, httpx_mock):
    # Ingest owns opening SimpleFIN accounts (decided 2026-07-30):
    # accounts.beancount must never need a
    # hand-added `open` line for a bank account again.
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD)

    run_sync()

    content = paths.accounts_simplefin_ledger().read_text(encoding="utf-8")
    assert "2000-01-01 open Assets:SimpleFIN:Checking   USD" in content
    assert "2000-01-01 open Assets:SimpleFIN:Savings   USD" in content
    # Sorted, not SimpleFIN response order.
    checking_pos = content.index("Assets:SimpleFIN:Checking")
    savings_pos = content.index("Assets:SimpleFIN:Savings")
    assert checking_pos < savings_pos


def test_sync_accounts_simplefin_file_is_byte_identical_across_reruns(
    bookkeeper_root, httpx_mock
):
    # The open date is a fixed sentinel, not derived from any run's data,
    # specifically so this file doesn't jeopardize the overall
    # byte-identical-on-rerun proof.
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD, is_reusable=True
    )

    run_sync()
    first = paths.accounts_simplefin_ledger().read_bytes()
    run_sync()
    second = paths.accounts_simplefin_ledger().read_bytes()

    assert first == second


def test_sync_hard_fails_on_asset_account_name_collision(bookkeeper_root, httpx_mock):
    # Two different SimpleFIN accounts sanitizing to the same name must
    # abort the whole sync, not silently merge two banks' transactions
    # under one ledger account.
    _seed_access_url(bookkeeper_root)
    payload = {
        "errlist": [],
        "accounts": [
            {
                "id": "ACT-1",
                "name": "Checking",
                "currency": "USD",
                "balance": "1.00",
                "balance-date": 1_700_000_000,
                "transactions": [],
            },
            {
                "id": "ACT-2",
                "name": "Checking",  # same display name, different id
                "currency": "USD",
                "balance": "2.00",
                "balance-date": 1_700_000_000,
                "transactions": [],
            },
        ],
    }
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=payload)

    result = run_sync()

    assert not result.ok
    assert "ACT-1" in result.render()
    assert "ACT-2" in result.render()
    # No partial write: nothing should have landed anywhere.
    assert not list(paths.transactions_dir().glob("*.beancount"))
    assert not paths.balances_ledger().exists()
    assert not paths.accounts_simplefin_ledger().exists()


def test_clean_rebuild_matches_a_resync_byte_for_byte(bookkeeper_root, httpx_mock):
    # Regression test for a real bug (found 2026-07-30): the
    # idempotency proof above only shows re-sync-on-top-of-existing-state
    # is a no-op. It does NOT show a from-nothing rebuild produces the same
    # bytes -- balances.beancount used to merge into whatever the file
    # already contained, so a from-scratch build (no pre-existing file)
    # rendered different bytes (missing the pre-existing placeholder
    # comment header) than a resync on top of an already-synced tree. Now
    # that balances.beancount is rewritten wholesale every time, both paths
    # must converge on the same content.
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD, is_reusable=True
    )

    # Path A: sync once (from nothing), then sync again on top of it.
    run_sync()
    run_sync()
    resync_hashes = _hash_ledger_tree()

    # Path B: clean rebuild -- delete every generated file, sync once.
    for f in paths.transactions_dir().glob("*.beancount"):
        f.unlink()
    paths.balances_ledger().unlink()
    paths.accounts_simplefin_ledger().unlink()
    run_sync()
    rebuild_hashes = _hash_ledger_tree()

    assert rebuild_hashes == resync_hashes, (
        "a clean rebuild produced different bytes than a resync -- some "
        "generated file is still merging with stale prior content instead "
        "of being rewritten wholesale"
    )


def _git(args: list[str], cwd) -> str:
    out = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )
    return out.stdout


def _commit_count(root) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], root).strip())


def test_a_sync_that_adds_transactions_commits_them(bookkeeper_root, httpx_mock):
    """PLAN.md §9 makes git the undo system "after each sync and each accepted
    batch". Only the batch half was wired for most of Phase 4: `review`,
    `apply` and `allocate` committed and `sync` did not, so a sync nobody
    confirmed afterwards left the ledger written but with nothing to revert to.

    A real repo is required or this passes trivially -- `commit_ledger` treats
    a missing repo as a warning and carries on, so against a repo-less fixture
    "it committed" and "it could not possibly commit" look identical. Local
    identity because a fresh machine has no global one, and that failure would
    also surface only as a warning.
    """
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD)
    _git(["init", "-q"], bookkeeper_root)
    _git(["config", "user.email", "harness@example.invalid"], bookkeeper_root)
    _git(["config", "user.name", "Sync harness"], bookkeeper_root)
    _git(["add", "-A"], bookkeeper_root)
    _git(["commit", "-qm", "fixture"], bookkeeper_root)
    before = _commit_count(bookkeeper_root)

    result = run_sync()

    assert result.ok
    assert result.transactions_added == 3
    assert result.commit is not None
    assert result.commit.committed is True
    assert _commit_count(bookkeeper_root) == before + 1
    assert _git(["status", "--short", "--", "ledger"], bookkeeper_root).strip() == ""


def test_a_sync_that_adds_nothing_makes_no_commit(bookkeeper_root, httpx_mock):
    """A rerun writes no file (see the module docstring on idempotency), so an
    empty commit would be noise in the very history someone reverts through."""
    _seed_access_url(bookkeeper_root)
    httpx_mock.add_response(
        url=ACCESS_URL + "/accounts", method="GET", json=SAMPLE_PAYLOAD, is_reusable=True
    )
    _git(["init", "-q"], bookkeeper_root)
    _git(["config", "user.email", "harness@example.invalid"], bookkeeper_root)
    _git(["config", "user.name", "Sync harness"], bookkeeper_root)
    _git(["add", "-A"], bookkeeper_root)
    _git(["commit", "-qm", "fixture"], bookkeeper_root)

    run_sync()
    after_first = _commit_count(bookkeeper_root)

    second = run_sync()

    assert second.transactions_added == 0
    assert second.commit is None
    assert _commit_count(bookkeeper_root) == after_first
