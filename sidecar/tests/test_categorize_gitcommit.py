"""Git is the undo system (PLAN.md §9), and §5.5 leans on it as the reason
auto-apply is acceptable at all. These tests pin the properties that make
that true: the commit is scoped to the ledger, a user's staged work is left
alone, nothing-to-commit is a success, and a missing repo degrades to a
warning rather than failing a write that already landed.

Every test runs against a throwaway repo created under `tmp_path`. Nothing
here may run git against the real repository.
"""

from __future__ import annotations

import subprocess

import pytest

from bookkeeper.categorize.gitcommit import COMMITTABLE_PATHS, commit_ledger, describe_batch


def _git(repo, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    )
    return proc.stdout


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with a committed ledger tree."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / "ledger" / "transactions").mkdir(parents=True)
    (tmp_path / "data").mkdir()
    (tmp_path / "ledger" / "transactions" / "2026.beancount").write_text("; start\n")
    (tmp_path / "README.md").write_text("unrelated\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "seed")
    return tmp_path


def _head_files(repo) -> list[str]:
    out = _git(repo, "show", "--name-only", "--pretty=format:", "HEAD")
    return [line for line in out.splitlines() if line.strip()]


def test_commits_a_modified_ledger_file(repo):
    (repo / "ledger" / "transactions" / "2026.beancount").write_text("; start\n; changed\n")

    result = commit_ledger("Categorize 1 transaction (confirmed by hand)", root=repo)

    assert result.ok
    assert result.committed
    assert result.sha
    assert _head_files(repo) == ["ledger/transactions/2026.beancount"]
    assert "Categorize 1 transaction" in _git(repo, "log", "-1", "--pretty=%s")


def test_commits_a_newly_created_untracked_ledger_file(repo):
    # A pathspec commit cannot include a file git has never seen, so a new
    # year file would silently never be committed without the `add` step.
    (repo / "ledger" / "transactions" / "2027.beancount").write_text("; new year\n")

    result = commit_ledger("new year", root=repo)

    assert result.committed
    assert "ledger/transactions/2027.beancount" in _head_files(repo)


def test_commits_memory_json_alongside_the_ledger(repo):
    # A confirmation writes both; reverting one without the other would
    # leave memory teaching a categorization the ledger no longer has.
    (repo / "ledger" / "transactions" / "2026.beancount").write_text("; start\n; changed\n")
    (repo / "data" / "memory.json").write_text('{"sq coffee": "Expenses:Food:Coffee"}\n')

    result = commit_ledger("confirm", root=repo)

    assert result.committed
    assert set(_head_files(repo)) == {
        "ledger/transactions/2026.beancount",
        "data/memory.json",
    }


def test_never_commits_files_outside_the_ledger(repo):
    (repo / "ledger" / "transactions" / "2026.beancount").write_text("; start\n; changed\n")
    (repo / "README.md").write_text("edited by the user, not by us\n")
    (repo / "secrets.txt").write_text("untracked and unrelated\n")

    result = commit_ledger("ledger only", root=repo)

    assert result.committed
    assert _head_files(repo) == ["ledger/transactions/2026.beancount"]
    # The user's unrelated edits survive untouched in the working tree.
    assert (repo / "README.md").read_text() == "edited by the user, not by us\n"
    status = _git(repo, "status", "--porcelain")
    assert "README.md" in status
    assert "secrets.txt" in status


def test_leaves_a_users_staged_unrelated_file_staged(repo):
    # The user staged something mid-session. Auto-commit must not sweep it
    # into our commit, and must not unstage it either.
    (repo / "README.md").write_text("staged by the user\n")
    _git(repo, "add", "README.md")
    (repo / "ledger" / "transactions" / "2026.beancount").write_text("; start\n; changed\n")

    result = commit_ledger("ledger only", root=repo)

    assert result.committed
    assert _head_files(repo) == ["ledger/transactions/2026.beancount"]
    staged = _git(repo, "diff", "--cached", "--name-only").split()
    assert staged == ["README.md"], "the user's staged file must still be staged"


def test_nothing_to_commit_is_a_clean_no_op(repo):
    before = _git(repo, "rev-parse", "HEAD")

    result = commit_ledger("nothing changed", root=repo)

    assert result.ok
    assert not result.committed
    assert "nothing to commit" in result.render()
    assert _git(repo, "rev-parse", "HEAD") == before


def test_non_git_directory_warns_instead_of_crashing(tmp_path):
    (tmp_path / "ledger").mkdir()
    (tmp_path / "ledger" / "main.beancount").write_text("; no repo here\n")

    result = commit_ledger("no repo", root=tmp_path)

    assert result.ok  # the write already succeeded; this is not its failure
    assert not result.committed
    assert any("not a git repository" in w for w in result.warnings)


def test_missing_ledger_tree_is_a_no_op(repo):
    for rel in COMMITTABLE_PATHS:
        target = repo / rel
        if target.is_dir():
            _git(repo, "rm", "-r", "-q", "--", rel)
    _git(repo, "commit", "-q", "-m", "drop ledger")

    result = commit_ledger("nothing to commit", root=repo)

    assert result.ok
    assert not result.committed


def test_does_not_push(repo, monkeypatch):
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(args, *rest, **kwargs):
        calls.append(list(args))
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    (repo / "ledger" / "transactions" / "2026.beancount").write_text("; start\n; changed\n")

    commit_ledger("no push please", root=repo)

    assert calls, "git was never invoked"
    assert not any("push" in call for call in calls)


def test_uses_bookkeeper_root_by_default(repo, monkeypatch):
    monkeypatch.setenv("BOOKKEEPER_ROOT", str(repo))
    (repo / "ledger" / "transactions" / "2026.beancount").write_text("; start\n; changed\n")

    result = commit_ledger("via BOOKKEEPER_ROOT")

    assert result.committed
    assert _head_files(repo) == ["ledger/transactions/2026.beancount"]


def test_describe_batch_reads_naturally_in_a_log():
    assert describe_batch(1, "confirmed by hand") == "Categorize 1 transaction (confirmed by hand)"
    assert describe_batch(40, "auto-applied") == "Categorize 40 transactions (auto-applied)"
