"""Trusted match-spec binding (Reviewer B, held FAIL through round 5).

THE GAP: an `ok` artifact's player_a/player_b/match_id come straight from the
untrusted bot's stdout. A hostile bot can print

    {"status":"ok","player_a":"famous-dev","player_b":"me","outcome":"b_wins",
     "match_id":"whatever"}

and fabricate a permanent ELO match against ANY identity, with ANY match_id — the
publish job would ingest it verbatim. The crash/invalid_output records are already
safe (the workflow builds them from trusted GitHub context); only `ok` is forgeable.

THE FIX: the workflow issues a trusted MatchSpec (submitter, opponent, match_id) from
GitHub context. On ingest the ok artifact's identities + match_id must BIND to that
spec. Any mismatch fails closed to a CRASH forfeit scored against the submitter —
never trusts forged identities, never drops the match (a dropped match skews ELO).

The outcome remains bot-asserted in v1 (honest trust boundary; a real adjudicated
arena is deferred). What this closes is IDENTITY + match_id forgery.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.publish import (
    MatchSpec,
    SpecMismatch,
    bind_ok_to_spec,
    ingest_result,
)
from atv_bench.store import LeagueStore, build_leaderboard_from_store


def _ok(pa="alice", pb="byok-anchor", outcome="a_wins", mid="run-1", **extra):
    return {"status": "ok", "player_a": pa, "player_b": pb, "outcome": outcome,
            "match_id": mid, "game": "battlesnake", **extra}


def _spec(submitter="alice", opponent="byok-anchor", match_id="run-1"):
    return MatchSpec(submitter=submitter, opponent=opponent, match_id=match_id)


def _sub(identity, harness="claude-code"):
    return {
        "identity": identity, "game": "battlesnake",
        "bot_sha256": "a" * 64, "bot_filename": "main.py",
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
        "fingerprint": {"harness": harness, "model": "claude-opus-4-8", "gstack": True,
                        "skills": ["gstack"], "mcps": [], "plugins": [],
                        "custom_agents_count": 0, "unknown": [], "probe_version": "1.0.0"},
    }


# --- pure binding: bind_ok_to_spec ---

def test_bind_accepts_matching_identities_and_id():
    """The honest case: bot reports the real two participants + the issued match_id."""
    rec = bind_ok_to_spec(_ok(pa="alice", pb="byok-anchor", mid="run-1"), _spec())
    assert {rec["player_a"], rec["player_b"]} == {"alice", "byok-anchor"}
    assert rec["match_id"] == "run-1"


def test_bind_accepts_swapped_orientation():
    """A/B orientation is not security-critical (outcome is bot-asserted in v1); the
    two identities being the real participants IS. Swapped A/B still binds."""
    rec = bind_ok_to_spec(_ok(pa="byok-anchor", pb="alice", mid="run-1"), _spec())
    assert {rec["player_a"], rec["player_b"]} == {"alice", "byok-anchor"}


def test_bind_rejects_third_party_identity():
    """THE core forgery: bot names a third party it never played."""
    with pytest.raises(SpecMismatch):
        bind_ok_to_spec(_ok(pa="famous-dev", pb="alice", mid="run-1"), _spec())


def test_bind_rejects_both_identities_forged():
    with pytest.raises(SpecMismatch):
        bind_ok_to_spec(_ok(pa="mallory", pb="famous-dev", mid="run-1"), _spec())


def test_bind_rejects_forged_match_id():
    """A fabricated match_id lets a bot inject a second, unissued match (or replay)."""
    with pytest.raises(SpecMismatch):
        bind_ok_to_spec(_ok(pa="alice", pb="byok-anchor", mid="not-the-run-id"), _spec())


def test_bind_rejects_opponent_substituted_for_self_match():
    """Bot claims it played itself (both = submitter) to farm a guaranteed win."""
    with pytest.raises(SpecMismatch):
        bind_ok_to_spec(_ok(pa="alice", pb="alice", mid="run-1"), _spec())


# --- ingest with spec: fail closed to a submitter forfeit, never trust the forgery ---

def test_ingest_forged_identity_scores_submitter_forfeit(tmp_path):
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("byok-anchor"))
    art = tmp_path / "forge.json"
    art.write_text(json.dumps(_ok(pa="famous-dev", pb="alice", outcome="b_wins", mid="run-1")))

    appended = ingest_result(str(art), store_dir=store_dir, spec=_spec())
    assert appended is True  # not dropped

    matches = store.load_matches()
    # exactly one match, and NOT the forged one — no "famous-dev" anywhere
    assert len(matches) == 1
    m = matches[0]
    assert "famous-dev" not in (m["player_a"], m["player_b"])
    assert {m["player_a"], m["player_b"]} == {"alice", "byok-anchor"}
    # scored as a forfeit loss for the submitter with reason CRASH
    assert m["outcome"] in ("forfeit_a", "forfeit_b")
    assert m["forfeit_reason"] == "CRASH"
    assert m["match_id"] == "run-1"


def test_ingest_forged_match_never_credits_a_win_to_forger(tmp_path):
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("byok-anchor"))
    # alice tries to fabricate a WIN over byok-anchor by simply asserting a_wins with a
    # forged match_id (a second unissued match).
    art = tmp_path / "farm.json"
    art.write_text(json.dumps(_ok(pa="alice", pb="byok-anchor", outcome="a_wins", mid="forged-2")))
    ingest_result(str(art), store_dir=store_dir, spec=_spec(match_id="run-1"))
    doc = build_leaderboard_from_store(store_dir, updated_at="2026-07-15T18:00:00Z")
    alice = next(r for r in doc["rows"] if r["identity"] == "alice")
    # the forged win did NOT push alice above seed — it was rebound to a forfeit LOSS
    assert alice["elo"] <= 1500


def test_ingest_honest_ok_with_spec_appends_bound_record(tmp_path):
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("byok-anchor"))
    art = tmp_path / "honest.json"
    art.write_text(json.dumps(_ok(pa="alice", pb="byok-anchor", outcome="a_wins", mid="run-1")))
    assert ingest_result(str(art), store_dir=store_dir, spec=_spec()) is True
    m = store.load_matches()[0]
    assert m["match_id"] == "run-1"
    assert m["outcome"] == "a_wins"
    assert {m["player_a"], m["player_b"]} == {"alice", "byok-anchor"}


def test_ingest_without_spec_is_unchanged(tmp_path):
    """Back-compat: no spec (local/hermetic use) keeps the prior verbatim behavior so
    existing callers and tests are untouched. The workflow always passes a spec."""
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("bob"))
    art = tmp_path / "nospec.json"
    art.write_text(json.dumps(_ok(pa="alice", pb="bob", outcome="a_wins", mid="m1")))
    assert ingest_result(str(art), store_dir=store_dir) is True
    m = store.load_matches()[0]
    assert {m["player_a"], m["player_b"]} == {"alice", "bob"}


def test_ingest_crash_with_spec_still_scores_submitter_forfeit(tmp_path):
    """A crash record is already trusted (workflow-built). With a spec present it still
    scores the submitter's forfeit and binds to the issued match_id."""
    store_dir = str(tmp_path / "league")
    store = LeagueStore(store_dir)
    store.add_submission(_sub("alice"))
    store.add_submission(_sub("byok-anchor"))
    crash = {"status": "crash", "loser": "alice", "opponent": "byok-anchor",
             "match_id": "run-1", "game": "battlesnake"}
    art = tmp_path / "crash.json"
    art.write_text(json.dumps(crash))
    assert ingest_result(str(art), store_dir=store_dir, spec=_spec()) is True
    m = store.load_matches()[0]
    assert m["forfeit_reason"] == "CRASH"
    assert {m["player_a"], m["player_b"]} == {"alice", "byok-anchor"}


def test_spec_from_env_reads_github_context(monkeypatch):
    """The workflow exports SUBMITTER/OPPONENT/MATCH_ID; MatchSpec.from_env builds the
    trusted spec so the publish CLI doesn't reparse GitHub context by hand."""
    monkeypatch.setenv("ATV_SUBMITTER", "alice")
    monkeypatch.setenv("ATV_OPPONENT", "byok-anchor")
    monkeypatch.setenv("ATV_MATCH_ID", "run-1-1")
    spec = MatchSpec.from_env()
    assert spec == MatchSpec(submitter="alice", opponent="byok-anchor", match_id="run-1-1")


def test_spec_from_env_missing_fails_closed(monkeypatch):
    monkeypatch.delenv("ATV_SUBMITTER", raising=False)
    monkeypatch.delenv("ATV_OPPONENT", raising=False)
    monkeypatch.delenv("ATV_MATCH_ID", raising=False)
    with pytest.raises(ValueError):
        MatchSpec.from_env()
