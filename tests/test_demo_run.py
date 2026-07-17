"""TDD for `atv-bench run --demo` walking skeleton (DX-6, ENG-13).

--demo is pure recording playback: ZERO Docker/auth/network. It replays a canned-
but-REAL recorded match (a real deterministic engine run between two distinct bots)
with honest schema-v2 provenance: verified=false, model_source=recording. Built FIRST
as the walking skeleton and the first README quickstart line.
"""
from __future__ import annotations

import json
from pathlib import Path

from atv_bench.demo_run import (
    DEMO_RECORDING_PATH,
    load_demo_record,
    demo_envelope,
)
from atv_bench.match_record import MATCH_RECORD_SCHEMA_VERSION


def test_demo_recording_file_exists_and_is_real():
    p = Path(DEMO_RECORDING_PATH)
    assert p.is_file(), "demo recording must be committed (built by scripts/build_demo_recording.py)"
    data = json.loads(p.read_text())
    # A REAL recorded match: frames from the actual engine, not synthetic scores.
    assert data["match"]["frames"], "recording must carry real engine frames"
    assert len(data["match"]["frames"]) > 10


def test_load_demo_record_returns_schema_v2_with_recording_provenance():
    rec = load_demo_record()
    d = rec.to_dict()
    assert d["schema_version"] == MATCH_RECORD_SCHEMA_VERSION
    # Honest provenance: a recording is NEVER a verified/publishable number.
    assert d["verified"] is False
    for p in d["players"]:
        assert p["model_source"] == "recording"


def test_demo_envelope_has_zero_dependency_shape():
    env = demo_envelope()
    assert env["success"] is True
    data = env["data"]
    assert data["game"]
    assert data["replay_path"]  # a playable replay path
    assert len(data["players"]) == 2
    # funnels the user onward (DX-6): demo output points to doctor -> run
    assert "next" in data


def test_demo_players_carry_captured_model_provenance():
    # ENG-13: the recording's model tag ships with captured provenance, not a literal
    # invented at read time.
    rec = load_demo_record()
    for p in rec.players:
        assert p.model  # a real model string from the recording
        assert p.model_source == "recording"
        assert p.verified is False
