"""F1 (santa round-1, both reviewers CRITICAL): the submission-record layout must be
ONE canonical path shared by the match job, the live-submit writer, and the store
reader — otherwise a `--live` entrant is scored but never appears on the board.

Canonical layout (matches league.yml match job `submissions/<id>/main.py`):

    league/submissions/<identity>/main.py          # the bot (match job reads this)
    league/submissions/<identity>/submission.json  # the record (store reads this)

Identity is anchored to the PARENT DIRECTORY name (not a file stem), preserving the
spoof protection from the flat layout: a hand-edited record claiming another entrant's
identity is rejected.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.store import LeagueStore, build_leaderboard_from_store


def _sub(identity, harness="claude-code", gstack=True):
    return {
        "identity": identity,
        "game": "battlesnake",
        "bot_sha256": "a" * 64,
        "bot_filename": "main.py",
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
        "fingerprint": {
            "harness": harness, "model": "claude-opus-4-8", "gstack": gstack,
            "skills": ["gstack"], "mcps": ["github"], "plugins": [],
            "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0",
        },
    }


def _write_live_tree(root, identity, record=None):
    """Materialize exactly what `submit --live` (open_submission_pr) commits."""
    dest = root / "submissions" / identity
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "main.py").write_text("def move(state):\n    return 'up'\n")
    (dest / "submission.json").write_text(
        json.dumps(record or _sub(identity), indent=2, sort_keys=True))
    return dest


def test_store_ingests_live_submitted_nested_tree(tmp_path):
    """The exact tree submit --live commits must be visible to the store, or the
    entrant is board-invisible after merge (the F1 bug)."""
    league = tmp_path / "league"
    _write_live_tree(league, "octocat")
    subs = LeagueStore(str(league)).load_submissions()
    assert set(subs) == {"octocat"}
    assert subs["octocat"]["identity"] == "octocat"


def test_add_submission_writes_nested_layout(tmp_path):
    """add_submission must write the SAME nested path the match job + live writer use,
    so a store-built submission and a live-submitted one are byte-identical in shape."""
    league = tmp_path / "league"
    store = LeagueStore(str(league))
    store.add_submission(_sub("alice"))
    assert (league / "submissions" / "alice" / "submission.json").is_file()
    # round-trips
    assert set(store.load_submissions()) == {"alice"}


def test_load_submissions_anchors_identity_to_parent_dir(tmp_path):
    """Spoof protection preserved: a record body claiming a different identity than its
    parent directory is rejected (mallory/ dir cannot claim to be alice)."""
    league = tmp_path / "league"
    _write_live_tree(league, "alice")
    # attacker creates mallory/ but the record body claims identity=alice
    _write_live_tree(league, "mallory", record=_sub("alice"))
    with pytest.raises(ValueError):
        LeagueStore(str(league)).load_submissions()


def test_live_submitted_entrant_appears_on_board(tmp_path):
    """End-to-end: a live-submitted (nested) record surfaces as a leaderboard row.
    This is the assertion whose absence let F1 ship."""
    league = tmp_path / "league"
    _write_live_tree(league, "octocat")
    doc = build_leaderboard_from_store(str(league), updated_at="2026-07-16T00:00:00Z")
    rows = [r for r in doc["rows"] if r["identity"] == "octocat"]
    assert len(rows) == 1, "live-submitted entrant must have a board row"


def test_duplicate_identity_dirs_rejected(tmp_path):
    """Two directories cannot both resolve to the same identity."""
    league = tmp_path / "league"
    _write_live_tree(league, "alice")
    # a second dir whose record also claims alice (parent 'alice2' != 'alice' -> mismatch)
    _write_live_tree(league, "alice2", record=_sub("alice"))
    with pytest.raises(ValueError):
        LeagueStore(str(league)).load_submissions()


# --- santa round-3 (H3, Reviewer B): malformed committed records must fail closed, not
#     crash trusted board generation with an uncaught KeyError/JSONDecodeError. ---

def test_malformed_json_record_raises_controlled_error(tmp_path):
    """A committed submission.json with invalid JSON must raise a controlled ValueError on
    load (fail-closed), not an uncaught JSONDecodeError deeper in board generation."""
    league = tmp_path / "league"
    d = league / "submissions" / "alice"
    d.mkdir(parents=True)
    (d / "submission.json").write_text("{ this is not valid json ")
    with pytest.raises(ValueError):
        LeagueStore(str(league)).load_submissions()


def test_record_missing_required_keys_raises_on_load(tmp_path):
    """A record missing required keys (identity/fingerprint/bot_sha256/pr_url/logs_url) must
    be rejected on load with a controlled error, not indexed blindly by build_leaderboard_doc
    (which would KeyError and take down the whole trusted board build)."""
    league = tmp_path / "league"
    d = league / "submissions" / "alice"
    d.mkdir(parents=True)
    # identity matches the dir, but everything else is missing
    (d / "submission.json").write_text(json.dumps({"identity": "alice"}))
    with pytest.raises((ValueError, KeyError)):
        LeagueStore(str(league)).load_submissions()


def test_one_malformed_record_does_not_silently_zero_the_board(tmp_path):
    """A malformed record must not be silently skipped either (that would drop a real
    entrant). It must fail closed so a maintainer sees and fixes it."""
    league = tmp_path / "league"
    _write_live_tree(league, "alice")
    bad = league / "submissions" / "bob"
    bad.mkdir(parents=True)
    (bad / "submission.json").write_text(json.dumps({"identity": "bob"}))  # missing keys
    with pytest.raises((ValueError, KeyError)):
        build_leaderboard_from_store(str(league), updated_at="2026-07-16T00:00:00Z")
