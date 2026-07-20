"""PR-path governance (santa round-4, Reviewer B CRITICAL): a community PR must be
confined to its OWN submission tree. The runtime scoring path is workflow-pinned to the
PR author, but the durable store is rebuilt from committed files — so a merged PR that
edits league/matches.jsonl directly, or writes into another entrant's directory, could
forge history or poison another row. This guard fails closed on any changed path outside
league/submissions/<author>/{main.py,submission.json}, so CI can block the PR before merge.
"""
from __future__ import annotations

import pytest

from atv_bench.validate import validate_pr_changes, validate_pr_paths


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


# --- name-status handling + always-on submission-PR confinement (santa round-7, Reviewer
#     B): the gate must reject renames/deletes (not just added/modified paths) and treat a
#     PR as a "submission PR" (subject to confinement) iff it touches league/submissions/**.
#     A submission PR that ALSO touches workflows / matches.jsonl / other dirs is the attack.

def test_changes_add_modify_own_files_allowed():
    res = validate_pr_changes("octocat", [
        "A\tleague/submissions/octocat/main.py",
        "M\tleague/submissions/octocat/submission.json",
    ])
    assert res["ok"] is True


def test_changes_rename_rejected():
    # a rename of another entrant's bot into your dir must be rejected (R status, 2 paths)
    res = validate_pr_changes("octocat", [
        "R100\tleague/submissions/victim/main.py\tleague/submissions/octocat/main.py",
    ])
    assert res["ok"] is False


def test_changes_delete_rejected():
    res = validate_pr_changes("octocat", ["D\tleague/matches.jsonl"])
    assert res["ok"] is False


@pytest.mark.parametrize(
    "record",
    [
        "T\tleague/submissions/octocat/main.py",
        "U\tleague/submissions/octocat/main.py",
        "M100\tleague/submissions/octocat/main.py",
        "M",
        "M\tleague/submissions/octocat/main.py\textra-path",
        " M\tleague/submissions/octocat/main.py",
        "M\t league/submissions/octocat/main.py",
        "M\x00\tleague/submissions/octocat/main.py",
    ],
)
def test_changes_reject_type_changes_unknown_statuses_and_malformed_records(record):
    res = validate_pr_changes("octocat", [record])
    assert res["ok"] is False


def test_changes_reject_non_string_records():
    res = validate_pr_changes("octocat", [None])  # type: ignore[list-item]
    assert res["ok"] is False


def test_changes_workflow_edit_on_submission_pr_rejected():
    # a submission PR that also edits a workflow file is the pwn-request vector
    res = validate_pr_changes("octocat", [
        "M\tleague/submissions/octocat/main.py",
        "M\t.github/workflows/league.yml",
    ])
    assert res["ok"] is False


def test_changes_non_submission_pr_is_not_confined():
    # a pure maintainer/plumbing PR (touches no league/submissions/**) is NOT a submission
    # PR and is not confined by this gate (it goes through normal review, not the league).
    res = validate_pr_changes("maintainer", [
        "M\tsrc/atv_bench/store.py",
        "M\t.github/workflows/ci.yml",
    ])
    assert res["ok"] is True
    assert res["is_submission_pr"] is False


def test_changes_submission_pr_flag_set():
    res = validate_pr_changes("octocat", ["A\tleague/submissions/octocat/main.py"])
    assert res["is_submission_pr"] is True


def test_malformed_submission_record_cannot_downgrade_to_plumbing_pr():
    res = validate_pr_changes(
        "octocat",
        ["M\tleague/submissions/octocat/main.py\textra-path"],
    )
    assert res["is_submission_pr"] is True
    assert res["ok"] is False


def test_submissions_root_scaffolding_is_not_a_submission():
    # league/submissions/.gitkeep (directory scaffolding at the submissions ROOT, no
    # per-entrant subdir) must NOT flip is_submission_pr — otherwise the foundational
    # maintainer PR that creates the tree gets confined to submission-only paths and its
    # own .github/** and src/** files are rejected. A real submission lives one level
    # deeper: league/submissions/<identity>/{main.py,submission.json}.
    res = validate_pr_changes("maintainer", [
        "A\tleague/submissions/.gitkeep",
        "M\tsrc/atv_bench/store.py",
        "A\t.github/workflows/ci.yml",
    ])
    assert res["is_submission_pr"] is False
    assert res["ok"] is True
