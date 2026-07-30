"""Canonical filesystem locations.

Every module resolves paths through here so that tests can point the whole
system at a fixture tree by setting BOOKKEEPER_ROOT.
"""

from __future__ import annotations

import os
from pathlib import Path

# repo root = parent of sidecar/
_DEFAULT_ROOT = Path(__file__).resolve().parents[2]


def root() -> Path:
    return Path(os.environ.get("BOOKKEEPER_ROOT", _DEFAULT_ROOT))


def ledger_dir() -> Path:
    return root() / "ledger"


def main_ledger() -> Path:
    return ledger_dir() / "main.beancount"


def accounts_ledger() -> Path:
    return ledger_dir() / "accounts.beancount"


def accounts_simplefin_ledger() -> Path:
    """Generated `open` directives for Assets:SimpleFIN:<Name> accounts.

    Owned and rewritten wholesale by bookkeeper.ingest on every sync --
    accounts_ledger() above stays hand-curated for expense/envelope
    accounts only, so no one has to hand-open an account per bank
    connection (team-lead decision, 2026-07-30).
    """
    return ledger_dir() / "accounts-simplefin.beancount"


def budget_ledger() -> Path:
    return ledger_dir() / "budget.beancount"


def transactions_dir() -> Path:
    return ledger_dir() / "transactions"


def balances_ledger() -> Path:
    return ledger_dir() / "balances.beancount"


def data_dir() -> Path:
    return root() / "data"


def raw_dir() -> Path:
    return data_dir() / "raw"


def secrets_dir() -> Path:
    return data_dir() / "secrets"


def access_url_file() -> Path:
    """SimpleFIN Access URL. Banking-credential grade: mode 0600, never logged."""
    return secrets_dir() / "simplefin.access-url"
