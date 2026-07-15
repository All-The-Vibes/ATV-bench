"""Tests for the publish-side entrypoint (trusted job) + the league data store.

The publish job must build a REAL leaderboard from the committed store (submissions +
match history), fail-closed on bad artifacts, and score crashes as forfeits (never
silently drop them). Regresses if the board goes empty/1970 or a bad artifact is
accepted (santa rounds 1-2).
"""
from __future__ import annotations

import json

import pytest

from atv_bench.publish import build_site, validate_artifact, ingest_result
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


def _ok(pa="alice", pb="bob", outcome="a_wins", mid="m1", **extra):
    return {"status": "ok", "player_a": pa, "player_b": pb, "outcome": outcome,
            "match_id": mid, "game": "battlesnake", **extra}


# --- fail-closed artifact validation (R2-Fix A) ---

def test_validate_artifact_accepts_wellformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_ok()))
    assert validate_artifact(str(p))["status"] == "ok"


def test_validate_artifact_rejects_malformed(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"nope": 1}))
    with pytest.raises(ValueError):
        validate_artifact(str(p))


def test_validate_artifact_rejects_bogus_outcome(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_ok(outcome="totally_fake")))
    with pytest.raises(ValueError):
        validate_artifact(str(p))


def test_validate_artifact_rejects_forfeit_without_reason(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_ok(outcome="forfeit_a")))  # no forfeit_reason
    with pytest.raises(ValueError):
        validate_artifact(str(p))


def test_validate_artifact_accepts_forfeit_with_reason(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps(_ok(outcome="forfeit_a", forfeit_reason="TIMEOUT")))
    assert validate_artifact(str(p))["outcome"] == "forfeit_a"


def test_validate_artifact_rejects_missing_players(tmp_path):
    p = tmp_path / "r.json"
    bad = _ok(); del bad["player_b"]
    p.write_text(json.dumps(bad))
    with pytest.raises(ValueError):
        validate_artifact(str(p))


# --- crash scored as forfeit, never dropped (R2-Fix B) ---

def test_crash_artifact_scored_as_forfeit(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob"))
    # a crash record carries who crashed (loser) + opponent so it can be scored
    crash = {"status": "crash", "loser": "bob", "opponent": "alice",
             "match_id": "c1", "game": "battlesnake"}
    art = tmp_path / "c.json"
    art.write_text(json.dumps(crash))
    appended = ingest_result(str(art), store_dir=str(tmp_path / "league"))
    assert appended is True  # NOT dropped
    matches = store.load_matches()
    m = next(x for x in matches if x["match_id"] == "c1")
    # scored as a forfeit loss for bob with reason CRASH
    assert m["outcome"] in ("forfeit_a", "forfeit_b")
    assert m["forfeit_reason"] == "CRASH"
    doc = build_leaderboard_from_store(str(tmp_path / "league"), updated_at="2026-07-15T18:00:00Z")
    alice = next(r for r in doc["rows"] if r["identity"] == "alice")
    bob = next(r for r in doc["rows"] if r["identity"] == "bob")
    assert alice["elo"] > bob["elo"]  # bob's crash counted as a loss


# --- real board from store (R1 + R2) ---

def test_store_roundtrip_and_real_board(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob", harness="copilot-cli", gstack=False))
    store.append_match(_ok())
    doc = build_leaderboard_from_store(str(tmp_path / "league"), updated_at="2026-07-15T18:00:00Z")
    assert len(doc["rows"]) == 2
    assert doc["updated_at"] == "2026-07-15T18:00:00Z"
    winner = next(r for r in doc["rows"] if r["identity"] == "alice")
    assert winner["rank"] == 1 and winner["elo"] > 1500


def test_build_site_from_store_is_not_empty(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob"))
    store.append_match(_ok())
    out = build_site(str(tmp_path / "site"), store_dir=str(tmp_path / "league"),
                     updated_at="2026-07-15T18:00:00Z")
    doc = json.loads((out / "leaderboard.json").read_text())
    assert doc["rows"]
    assert doc["updated_at"] != "1970-01-01T00:00:00Z"


def test_ingest_ok_result_appends(tmp_path):
    store = LeagueStore(str(tmp_path / "league"))
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob"))
    art = tmp_path / "r.json"
    art.write_text(json.dumps(_ok(outcome="b_wins", mid="m42")))
    assert ingest_result(str(art), store_dir=str(tmp_path / "league")) is True
    assert any(m["match_id"] == "m42" for m in store.load_matches())


def test_empty_store_yields_empty_but_valid_board(tmp_path):
    doc = build_leaderboard_from_store(str(tmp_path / "league"), updated_at="2026-07-15T18:00:00Z")
    assert doc["rows"] == []
    assert doc["schema_version"] == 1


def test_history_persists_across_fresh_checkout(tmp_path):
    """Reviewer-A suggestion: ingest a match, then rebuild from a FRESH store handle
    (simulating a new checkout reading only what's on disk) and assert the prior match
    still counts. Guards the 'recompute-from-committed-history' claim end-to-end."""
    store_dir = str(tmp_path / "league")
    s1 = LeagueStore(store_dir)
    s1.add_submission(_sub("alice"))
    s1.add_submission(_sub("bob"))
    art = tmp_path / "r.json"
    art.write_text(json.dumps(_ok(outcome="a_wins", mid="persist1")))
    ingest_result(str(art), store_dir=store_dir)
    # a second, independent match on top
    art2 = tmp_path / "r2.json"
    art2.write_text(json.dumps(_ok(outcome="a_wins", mid="persist2")))
    ingest_result(str(art2), store_dir=store_dir)
    # fresh handle reads only disk state
    s2 = LeagueStore(store_dir)
    matches = s2.load_matches()
    assert {m["match_id"] for m in matches} == {"persist1", "persist2"}
    doc = build_leaderboard_from_store(store_dir, updated_at="2026-07-15T18:00:00Z")
    alice = next(r for r in doc["rows"] if r["identity"] == "alice")
    assert alice["match_count"] == 2  # both persisted matches counted
