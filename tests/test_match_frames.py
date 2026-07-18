"""RED->GREEN tests for deterministic match frame recording.

A visualization needs the per-tick geometry, not just the final verdict. `run_match`
gains an opt-in `record=True` that captures a frame per tick (both heads + full trails +
turn), so the match can be animated/replayed. Recording must NOT change the adjudicated
outcome, and — like everything in the engine — must be deterministic: same inputs, same
frames.
"""
from __future__ import annotations

from atv_bench.arena.engine import Direction, Outcome, TronEngine
from atv_bench.arena.referee import run_match
from atv_bench.bots import make_bot


def _engine(**kw):
    return TronEngine(
        width=kw.get("width", 13), height=kw.get("height", 13),
        start_a=(1, 6), start_b=(11, 6),
        dir_a=Direction.RIGHT, dir_b=Direction.LEFT,
        max_turns=kw.get("max_turns", 100),
    )


def _run(record):
    return run_match(
        _engine(), make_bot("greedy", "a"), make_bot("wall_hugger", "b"),
        player_a="greedy", player_b="wall_hugger", match_id="m",
        game="lightcycles", seed=0, record=record,
    )


def test_no_frames_key_when_not_recording():
    result = _run(record=False)
    assert "frames" not in result


def test_recording_adds_frames_list():
    result = _run(record=True)
    frames = result["frames"]
    assert isinstance(frames, list) and len(frames) >= 2  # initial + >=1 tick


def test_frame_shape():
    result = _run(record=True)
    f0 = result["frames"][0]
    assert set(f0) >= {"turn", "a", "b"}
    assert set(f0["a"]) >= {"pos", "trail"}
    assert set(f0["b"]) >= {"pos", "trail"}
    # pos is [x, y]; trail is a list of [x, y]
    assert len(f0["a"]["pos"]) == 2
    assert all(len(c) == 2 for c in f0["a"]["trail"])


def test_recording_does_not_change_outcome():
    plain = _run(record=False)
    rec = _run(record=True)
    assert plain["outcome"] == rec["outcome"]
    assert plain["match_id"] == rec["match_id"]


def test_recording_is_deterministic():
    a = _run(record=True)["frames"]
    b = _run(record=True)["frames"]
    assert a == b


def test_frames_include_board_dims():
    result = _run(record=True)
    assert result.get("board", {}).get("width") == 13
    assert result.get("board", {}).get("height") == 13


def test_last_frame_matches_final_positions_advance():
    """Trails grow monotonically across frames (heads move, cells accrete)."""
    frames = _run(record=True)["frames"]
    a_lens = [len(f["a"]["trail"]) for f in frames]
    assert a_lens == sorted(a_lens)  # non-decreasing
    assert a_lens[-1] > a_lens[0]
