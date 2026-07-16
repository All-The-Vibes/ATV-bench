"""PR-path governance (santa round-4, Reviewer B CRITICAL): a community PR must be
confined to its OWN submission tree. The runtime scoring path is workflow-pinned to the
PR author, but the durable store is rebuilt from committed files — so a merged PR that
edits league/matches.jsonl directly, or writes into another entrant's directory, could
forge history or poison another row. This guard fails closed on any changed path outside
league/submissions/<author>/{main.py,submission.json}, so CI can block the PR before merge.
"""
from __future__ import annotations

import pytest

from atv_bench.validate import validate_pr_paths


def test_own_submission_files_allowed():
    res = validate_pr_paths("octocat", [
        "league/submissions/octocat/main.py",
        "league/submissions/octocat/submission.json",
    ])
    assert res["ok"] is True and res["errors"] == []


def test_direct_matches_edit_rejected():
    res = validate_pr_paths("octocat", ["league/matches.jsonl"])
    assert res["ok"] is False
    assert any("matches.jsonl" in e for e in res["errors"])


def test_other_entrant_dir_rejected():
    res = validate_pr_paths("octocat", ["league/submissions/victim/main.py"])
    assert res["ok"] is False


def test_stray_file_in_own_dir_rejected():
    res = validate_pr_paths("octocat", ["league/submissions/octocat/evil.sh"])
    assert res["ok"] is False


def test_path_traversal_in_changed_path_rejected():
    res = validate_pr_paths("octocat", ["league/submissions/octocat/../victim/main.py"])
    assert res["ok"] is False


def test_edits_outside_league_rejected():
    res = validate_pr_paths("octocat", ["src/atv_bench/store.py"])
    assert res["ok"] is False


def test_empty_or_invalid_author_rejected():
    for bad in ("", "has space", "a/b"):
        res = validate_pr_paths(bad, ["league/submissions/x/main.py"])
        assert res["ok"] is False
