from __future__ import annotations

from pathlib import Path

from bookkeeper.ingest.dedup import existing_simplefin_keys


def _txn_block(txn_id: str, account: str) -> str:
    return (
        f'2026-01-05 * "Something"\n'
        f'  simplefin-id: "{txn_id}"\n'
        f'  simplefin-account: "{account}"\n'
        f"  {account}   -1.00 USD\n"
        f"  Expenses:Unknown\n\n"
    )


def test_existing_simplefin_keys_missing_dir_returns_empty(tmp_path):
    assert existing_simplefin_keys(tmp_path / "does-not-exist") == set()


def test_existing_simplefin_keys_scans_all_year_files(tmp_path):
    (tmp_path / "2025.beancount").write_text(
        _txn_block("TXN-A", "Assets:Checking"), encoding="utf-8"
    )
    (tmp_path / "2026.beancount").write_text(
        _txn_block("TXN-B", "Assets:Checking") + _txn_block("TXN-C", "Assets:Savings"),
        encoding="utf-8",
    )

    assert existing_simplefin_keys(tmp_path) == {
        ("Assets:Checking", "TXN-A"),
        ("Assets:Checking", "TXN-B"),
        ("Assets:Savings", "TXN-C"),
    }


def test_existing_simplefin_keys_ignores_non_beancount_files(tmp_path):
    (tmp_path / "2026.beancount").write_text(
        _txn_block("TXN-B", "Assets:Checking"), encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text(
        'simplefin-id: "SHOULD-NOT-COUNT"\nsimplefin-account: "Assets:Checking"\n',
        encoding="utf-8",
    )

    assert existing_simplefin_keys(tmp_path) == {("Assets:Checking", "TXN-B")}


def test_existing_simplefin_keys_distinguishes_same_id_across_accounts(tmp_path: Path):
    # The load-bearing case: live-confirmed (2026-07-30), the real demo
    # server issues the *same* transaction id for genuinely different
    # transactions in different accounts. Dedup must not collapse them.
    text = _txn_block("1777859572", "Assets:Checking") + _txn_block(
        "1777859572", "Assets:Savings"
    )
    (tmp_path / "2026.beancount").write_text(text, encoding="utf-8")

    keys = existing_simplefin_keys(tmp_path)

    assert keys == {("Assets:Checking", "1777859572"), ("Assets:Savings", "1777859572")}
    assert len(keys) == 2  # not deduped down to 1
