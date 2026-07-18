"""TDD for the shared snapshot-diff capture (Eng Decision #3, ENG-1 corrected formula).

The locked-and-corrected capture formula:
    git diff <base-tree-sha>        # base tree -> working tree: covers committed +
                                    # staged + UNSTAGED edits of tracked files
  UNION
    git ls-files --others --exclude-standard   # untracked-not-ignored new files

The old `edited != original` single-file compare and the `<base>..HEAD`+`--cached`
decomposition both DROP the most common harness behavior (an in-place unstaged edit
of a tracked file). These tests pin the corrected behavior with both harness shapes:
  (a) adapter COMMITS its edit               -> captured   [CRITICAL]
  (b) adapter edits a tracked file IN PLACE  -> captured   [CRITICAL]
plus multi-file, untracked, and git-gc-survival.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from atv_bench.adapters.snapshot import capture_diff, seed_base


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    ).stdout


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("def move(o):\n    return 'N'\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=atv", "commit", "-qm", "init")
    return repo


def test_seed_base_returns_a_ref_and_tags_it(tmp_path):
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    assert base  # a sha or tag we can diff against
    # tagged so a harness `git gc --prune=now` cannot orphan it (ENG-11)
    tags = _git(repo, "tag").split()
    assert "atv-base" in tags


def test_captures_a_committed_edit(tmp_path):
    """CRITICAL: harness commits its edit -> still captured (not a false forfeit)."""
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / "main.py").write_text("def move(o):\n    return 'S'\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=atv", "commit", "-qm", "edit")
    diff = capture_diff(repo, base)
    assert "return 'S'" in diff
    assert "main.py" in diff


def test_captures_an_in_place_unstaged_edit(tmp_path):
    """CRITICAL: harness rewrites a tracked file and exits without staging/committing."""
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / "main.py").write_text("def move(o):\n    return 'E'\n")  # unstaged, uncommitted
    diff = capture_diff(repo, base)
    assert "return 'E'" in diff


def test_captures_a_staged_but_uncommitted_edit(tmp_path):
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / "main.py").write_text("def move(o):\n    return 'W'\n")
    _git(repo, "add", "-A")
    diff = capture_diff(repo, base)
    assert "return 'W'" in diff


def test_captures_multiple_files(tmp_path):
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / "main.py").write_text("def move(o):\n    return 'S'\n")
    (repo / "strategy.py").write_text("WEIGHT = 3\n")  # new untracked file
    diff = capture_diff(repo, base)
    assert "main.py" in diff
    assert "strategy.py" in diff


def test_untracked_file_is_captured(tmp_path):
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / "helper.py").write_text("X = 1\n")
    diff = capture_diff(repo, base)
    assert "helper.py" in diff


def test_ignored_file_is_not_captured(tmp_path):
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / ".gitignore").write_text("secret.txt\n")
    (repo / "secret.txt").write_text("TOKEN=abc\n")
    diff = capture_diff(repo, base)
    # The ignored file's CONTENT must never be captured (the .gitignore line naming
    # it is fine — that's the tracked config, not the secret body).
    assert "TOKEN=abc" not in diff


def test_no_edit_yields_empty_diff(tmp_path):
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    diff = capture_diff(repo, base)
    assert diff.strip() == ""


def test_survives_git_gc(tmp_path):
    """ENG-11: a harness running `git gc --prune=now` must not orphan the base."""
    repo = _init_repo(tmp_path)
    base = seed_base(repo)
    (repo / "main.py").write_text("def move(o):\n    return 'S'\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=atv", "commit", "-qm", "edit")
    _git(repo, "gc", "--prune=now")
    diff = capture_diff(repo, base)
    assert "return 'S'" in diff
