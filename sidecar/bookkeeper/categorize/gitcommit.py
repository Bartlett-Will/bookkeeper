"""Auto-commit the ledger after a write. Git is the undo system (PLAN.md §9).

§5.5 lists reversibility as the mitigation that makes auto-apply acceptable
at all: "ledger files are git-committed on every write, so any bad
auto-apply batch is one `git revert` away." That promise is only worth
anything if the commit actually happens and is scoped tightly enough that
reverting it undoes the bad batch *and nothing else*. Hence the rules
below, each of which exists because the alternative is a footgun on
someone's financial records:

- **Only `ledger/` and `data/memory.json` are ever committed.** These are
  the two things a categorization write touches. A commit that swept up
  whatever else happened to be dirty would make `git revert` unusable as
  an undo -- reverting would also undo unrelated work.
- **A user's staged files are never disturbed.** We commit with an
  explicit pathspec (`git commit -- <paths>`), which commits the working
  tree content of those paths and leaves every other index entry exactly
  as the user left it. We only run `git add` for paths that are untracked,
  because a pathspec commit cannot include a file git has never seen.
- **Nothing to commit is a success, not an error.** A dry run, or a rerun
  that changed nothing, must not fail a command.
- **No repo, or no git, is a warning.** The ledger is usable without
  version control; losing the undo system is worth saying out loud but is
  not a reason to fail a write that already succeeded.
- **Never push.** Pushing is a decision about a remote, and a ledger is
  private financial data. It is not ours to make.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from bookkeeper import paths

#: Everything a categorization write is allowed to commit, relative to the
#: repo root. Deliberately a fixed literal list rather than anything
#: derived at runtime: the whole safety argument above rests on this set
#: being small, auditable, and impossible to widen by accident.
COMMITTABLE_PATHS: tuple[str, ...] = ("ledger", "data/memory.json")

_GIT_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CommitResult:
    """Outcome of one auto-commit attempt.

    `ok` is False only for a genuine git failure. A missing repo, a missing
    git binary, or an empty changeset are all `ok=True` -- the caller's
    write already succeeded and none of those are its fault.
    """

    ok: bool
    committed: bool = False
    sha: str = ""
    message: str = ""
    files: tuple[str, ...] = ()
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def render(self) -> str:
        if not self.ok:
            lines = ["git commit failed:"]
            lines.extend(f"  - {w}" for w in self.warnings)
            return "\n".join(lines)
        if self.committed:
            lines = [f"committed {self.sha} — {self.message}"]
            lines.extend(f"  {f}" for f in self.files)
        else:
            lines = ["git: nothing to commit"]
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return "\n".join(lines)


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=False,
    )


def _git_toplevel(root: Path) -> tuple[Path | None, str]:
    """The work tree containing `root`, or `(None, reason)`."""
    try:
        proc = _run_git(["rev-parse", "--show-toplevel"], root)
    except FileNotFoundError:
        return None, "git is not installed; ledger changes are not versioned"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run git ({exc}); ledger changes are not versioned"
    if proc.returncode != 0:
        return None, f"{root} is not a git repository; ledger changes are not versioned"
    return Path(proc.stdout.strip()), ""


def _existing_committable(root: Path) -> list[str]:
    """`COMMITTABLE_PATHS` that exist on disk, as repo-root-relative strings.

    A pathspec naming something that has never existed makes `git commit`
    exit non-zero with "did not match any file(s) known to git", which
    would turn a perfectly good no-op into a reported failure.
    """
    return [rel for rel in COMMITTABLE_PATHS if (root / rel).exists()]


def commit_ledger(message: str, root: Path | None = None) -> CommitResult:
    """Commit ledger changes with `message`. Safe to call after every write.

    `root` defaults to `paths.root()`, so tests point this at a throwaway
    repo via `BOOKKEEPER_ROOT` exactly like every other module here. It must
    be inside the git work tree it resolves to -- committing a pathspec
    from a repo that merely *contains* an unrelated checkout would commit
    the wrong `ledger/`.
    """
    base = (root or paths.root()).resolve()
    if not base.exists():
        return CommitResult(ok=True, warnings=(f"{base} does not exist; nothing committed",))

    toplevel, reason = _git_toplevel(base)
    if toplevel is None:
        return CommitResult(ok=True, warnings=(reason,))

    toplevel = toplevel.resolve()
    if base != toplevel and toplevel not in base.parents:
        return CommitResult(
            ok=True,
            warnings=(
                (
                    f"{base} is not inside git work tree {toplevel}; ledger changes are "
                    "not versioned"
                ),
            ),
        )

    relative = _existing_committable(base)
    if not relative:
        return CommitResult(ok=True)

    # Pathspecs are resolved against `cwd`, which is `base` -- not the repo
    # toplevel -- so a ledger nested inside a larger repo still commits the
    # right files.
    pathspec = ["--", *relative]

    status = _run_git(["status", "--porcelain", *pathspec], base)
    if status.returncode != 0:
        return CommitResult(ok=False, warnings=(status.stderr.strip() or "git status failed",))
    if not status.stdout.strip():
        return CommitResult(ok=True)

    # Untracked files can't be named in a pathspec commit, so they need an
    # explicit `add`. Only untracked ones: staging a tracked file here would
    # overwrite a user's deliberately-staged partial content for it.
    untracked = _run_git(["ls-files", "--others", "--exclude-standard", *pathspec], base)
    new_files = [line for line in untracked.stdout.splitlines() if line.strip()]
    if new_files:
        add = _run_git(["add", "--", *new_files], base)
        if add.returncode != 0:
            return CommitResult(ok=False, warnings=(add.stderr.strip() or "git add failed",))

    commit = _run_git(["commit", "-m", message, *pathspec], base)
    if commit.returncode != 0:
        return CommitResult(
            ok=False, warnings=(commit.stderr.strip() or commit.stdout.strip() or "git commit failed",)
        )

    sha = _run_git(["rev-parse", "--short", "HEAD"], base)
    changed = _run_git(["show", "--name-only", "--pretty=format:", "HEAD"], base)
    return CommitResult(
        ok=True,
        committed=True,
        sha=sha.stdout.strip(),
        message=message,
        files=tuple(line for line in changed.stdout.splitlines() if line.strip()),
    )


def describe_batch(count: int, kind: str) -> str:
    """A commit message that says what changed and how, for `git log` archaeology.

    Deliberately mentions *how* the categorization was decided. Six months
    on, "which of these did a model choose and which did I confirm?" is the
    question a ledger owner will actually ask, and the answer should be in
    the log, not only in per-transaction metadata.
    """
    noun = "transaction" if count == 1 else "transactions"
    return f"Categorize {count} {noun} ({kind})"
