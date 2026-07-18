"""Snapshot diff capture (Eng Decision #3, ENG-1/ENG-11 corrected formula).

The harness CLI runs headless in a seeded git repo and edits the bot however it
likes — in place, staged, committed, multi-file, or a mix. We must capture ALL of
those shapes as "the bot the harness built", without a false forfeit.

Corrected, locked formula (do not regress to `<base>..HEAD` + `--cached`):

    capture = git diff <base-tree-sha>                       (tracked: committed +
                                                              staged + unstaged)
              UNION
              git ls-files --others --exclude-standard        (untracked-not-ignored)

`git diff <tree>` compares the base tree to the WORKING TREE, so it already covers
committed, staged, and plain unstaged edits of tracked paths in one shot. Untracked
new files never appear in a tree-vs-worktree diff, so we add them explicitly and
render each as a proper `/dev/null -> file` addition via `git diff --no-index`.

`seed_base` also plants a lightweight tag (`atv-base`) so a harness that runs
`git gc --prune=now` mid-edit cannot orphan the base object (ENG-11).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

BASE_TAG = "atv-base"


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def seed_base(repo: Path) -> str:
    """Record the base tree of `repo` and pin it against GC. Returns the base SHA.

    Must be called after the seed project is committed and BEFORE the harness runs.
    """
    repo = Path(repo)
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    # Tag the commit so `git gc --prune=now` keeps it reachable (ENG-11).
    _git(repo, "tag", "-f", BASE_TAG, sha, check=False)
    return sha


def capture_diff(repo: Path, base: str) -> str:
    """Unified diff of everything the harness changed since `base` (see module docstring)."""
    repo = Path(repo)
    # Tracked paths: committed + staged + unstaged, all at once.
    tracked = _git(repo, "diff", base).stdout

    # Untracked-not-ignored new files: render each as an addition.
    others = _git(repo, "ls-files", "--others", "--exclude-standard").stdout.split("\n")
    chunks: list[str] = []
    for rel in others:
        rel = rel.strip()
        if not rel:
            continue
        # `git diff --no-index /dev/null <file>` yields a clean add-diff; returns 1
        # (differences found), which is expected, so don't check the return code.
        proc = _git(repo, "diff", "--no-index", "--", "/dev/null", rel, check=False)
        if proc.stdout:
            chunks.append(proc.stdout)

    return tracked + "".join(chunks)
