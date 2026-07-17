"""TDD for the run-pipeline match record + identity key (schema v2, ENG-10 / gap #14).

This is the shared contract frozen in build step 0: Lane A (fingerprint) produces the
fields, Lane C (runner) consumes them. It is SEPARATE from the v1 community-league
leaderboard (`leaderboard.py`), which serves the PR-submission flow. The run pipeline
records a live host-subprocess match with full provenance and honest verification state.
"""
from __future__ import annotations

import pytest

from atv_bench.match_record import (
    MATCH_RECORD_SCHEMA_VERSION,
    PlayerRecord,
    MatchRecord,
    identity_key,
    is_publishable,
)


def _player(**over):
    base = dict(
        harness="copilot-cli",
        model="claude-opus-4.8",
        model_source="parsed",
        verified=False,
        tools=["read", "write"],
        nested_skills=["gstack/plan"],
        fingerprint_sha256="a" * 64,
        adapter_version="1.0.0",
    )
    base.update(over)
    return PlayerRecord(**base)


def test_schema_version_is_two():
    assert MATCH_RECORD_SCHEMA_VERSION == 2


def test_identity_key_uses_full_v2_tuple():
    p = _player(verified=True, model="claude-opus-4.8")
    key = identity_key(
        p, game_version="lightcycles@1", prompt_version="edit@1"
    )
    # (game_version, prompt_version, harness, verified_model, fingerprint_sha256, adapter_version)
    assert key == (
        "lightcycles@1", "edit@1", "copilot-cli", "claude-opus-4.8", "a" * 64, "1.0.0",
    )


def test_unverified_row_never_publishes_a_number():
    p = _player(verified=False)
    assert is_publishable(p) is False


def test_verified_row_with_real_model_publishes():
    p = _player(verified=True, model="claude-opus-4.8", model_source="gateway")
    assert is_publishable(p) is True


def test_unknown_model_blocks_publish_even_if_flagged_verified():
    # A model tag of 'unknown' or an echoed 'auto' can never publish, regardless.
    assert is_publishable(_player(verified=True, model="unknown")) is False
    assert is_publishable(_player(verified=True, model="auto")) is False


def test_match_record_round_trips_to_dict():
    rec = MatchRecord(
        game="lightcycles",
        game_version="lightcycles@1",
        prompt_version="edit@1",
        codeclash_version="vendored@f0694c64ecf6",
        players=[_player(), _player(harness="claude-code")],
        rounds=3,
        outcome={"winner": "copilot-cli", "scores": {"copilot-cli": 2, "claude-code": 1}},
        replay_path="_replay/index.html",
        verified=False,
    )
    d = rec.to_dict()
    assert d["schema_version"] == 2
    assert d["verified"] is False
    assert len(d["players"]) == 2
    assert d["players"][0]["fingerprint_sha256"] == "a" * 64
    assert d["codeclash_version"].startswith("vendored@")


def test_match_verified_only_when_all_players_publishable():
    rec = MatchRecord(
        game="lightcycles", game_version="lightcycles@1", prompt_version="edit@1",
        codeclash_version="v", rounds=1, outcome={}, replay_path="",
        players=[_player(verified=True, model_source="gateway"),
                 _player(verified=False)],
    )
    # one unverified player -> whole match unverified
    assert rec.is_verified() is False
