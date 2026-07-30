from __future__ import annotations

from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def bookkeeper_root(tmp_path, monkeypatch):
    """Point every `bookkeeper.paths` lookup at an isolated temp tree.

    `paths.root()` reads `BOOKKEEPER_ROOT` on every call, so setting the env
    var is enough -- no need to patch individual path functions.
    """
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(tmp_path))
    (tmp_path / "ledger" / "transactions").mkdir(parents=True)
    (tmp_path / "data" / "secrets").mkdir(parents=True)
    (tmp_path / "data" / "raw").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def fixture_root(monkeypatch):
    """Point BOOKKEEPER_ROOT at a hand-written ledger under tests/fixtures/<name>.

    Unlike `bookkeeper_root`, these trees are committed fixtures (not empty
    temp dirs) — each `tests/fixtures/<name>/` contains its own
    `ledger/main.beancount`. Returns a function so a test can name the
    scenario it wants (`fixture_root("basic")`, `fixture_root("over_allocation")`, ...).
    """

    def _use(name: str) -> Path:
        root = FIXTURES_DIR / name
        assert (root / "ledger" / "main.beancount").exists(), f"no fixture named {name!r}"
        monkeypatch.setenv("BOOKKEEPER_ROOT", str(root))
        return root

    return _use
