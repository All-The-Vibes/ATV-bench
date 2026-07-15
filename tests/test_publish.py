"""Tests for the publish-side entrypoint (trusted job) + the league data store.

The publish job must build a REAL leaderboard from the committed store (submissions +
match history), not a hardcoded empty board. These tests fail if the pipeline
regresses to an empty/1970 board (santa round-1 critical finding).
"""
from __future__ import annotations

import json

import pytest

from atv_bench.publish import build_site, validate_artifact, ingest_result
from atv_bench.store import (
    LeagueStore,
    build_leaderboard_from_store,
)


def _sub(identity, harness="claude-code", gstack=True):
    return {
        "identity": identity,
        "game": "battlesnake",
        "bot_sha256": "a" * 64,
        "bot_filename": "main.py",
        "fingerprint": {
            "harness": harness, "model": "claude-opus-4-8", "gstack": gstack,
            "skills": ["gstack"], "mcps": ["github"], "plugins": [],
            "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0",
        },
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
    }


def test_validate_artifact_accepts_wellformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"status": "ok", "player_a": "alice", "player_b": "bob",
                             "outcome": "a_wins", "match_id": "m1"}))
    assert validate_artifact(str(p))["status"] == "ok"


def test_validate_artifact_rejects_malformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        validate_artifact(str(p))


def test_store_roundtrip_and_real_board(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob", harness="copilot-cli", gstack=False))
    store.append_match({"player_a": "alice", "player_b": "bob", "outcome": "a_wins",
                        "match_id": "m1", "game": "battlesnake"})
    doc = build_leaderboard_from_store(str(tmp_path / "league"), updated_at="2026-07-15T18:00:00Z")
    assert doc["schema_version"] == 1
    # REAL board — not empty, not 1970
    assert len(doc["rows"]) == 2
    assert doc["updated_at"] == "2026-07-15T18:00:00Z"
    winner = next(r for r in doc["rows"] if r["identity"] == "alice")
    assert winner["rank"] == 1
    assert winner["elo"] > 1500


def test_build_site_from_store_is_not_empty(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob"))
    store.append_match({"player_a": "alice", "player_b": "bob", "outcome": "a_wins",
                        "match_id": "m1", "game": "battlesnake"})
    out = build_site(str(tmp_path / "site"), store_dir=str(tmp_path / "league"),
                     updated_at="2026-07-15T18:00:00Z")
    doc = json.loads((out / "leaderboard.json").read_text())
    assert doc["rows"], "published board must not be empty when the store has data"
    assert doc["updated_at"] != "1970-01-01T00:00:00Z"


def test_ingest_result_appends_match_to_store(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob"))
    artifact = tmp_path / "match-result.json"
    artifact.write_text(json.dumps({
        "status": "ok", "player_a": "alice", "player_b": "bob",
        "outcome": "b_wins", "match_id": "m42", "game": "battlesnake",
    }))
    ingest_result(str(artifact), store_dir=str(tmp_path / "league"))
    matches = store.load_matches()
    assert any(m["match_id"] == "m42" for m in matches)


def test_empty_store_yields_empty_but_valid_board(tmp_path):
    # an empty store is legitimately empty (no submitters yet) — but still schema-valid
    doc = build_leaderboard_from_store(str(tmp_path / "league"), updated_at="2026-07-15T18:00:00Z")
    assert doc["rows"] == []
    assert doc["schema_version"] == 1
