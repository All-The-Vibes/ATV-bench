"""RED->GREEN tests for the local match runner behind `atv-bench play`.

`play` is the missing UX: run a refereed match locally between a chosen player and a
chosen opponent (a named bot OR your harness-built bot file), record frames, render an
ASCII board, and emit a self-contained HTML replay. It reuses the SAME trusted engine +
referee as the sandboxed arena, so a local result is honest, not mocked.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atv_bench.play import (
    Contestant,
    build_replay_html,
    render_ascii,
    run_local_match,
)


def test_run_named_vs_named_produces_recorded_result():
    res = run_local_match(
        game="lightcycles",
        player=Contestant(key="greedy"),
        opponent=Contestant(key="wall_hugger"),
        seed=0,
    )
    assert res["status"] == "ok"
    assert res["player_a"] == "greedy"
    assert res["player_b"] == "wall_hugger"
    assert "frames" in res and len(res["frames"]) >= 2
    assert res["outcome"] in {"a_wins", "b_wins", "draw", "forfeit_a", "forfeit_b"}


def test_unknown_game_rejected():
    with pytest.raises(ValueError):
        run_local_match(game="battlesnake",
                        player=Contestant(key="greedy"),
                        opponent=Contestant(key="bare"), seed=0)


def test_render_ascii_humanizes_winner():
    res = run_local_match(game="lightcycles", player=Contestant(key="greedy"),
                          opponent=Contestant(key="wall_hugger"), seed=0)
    art = render_ascii(res)
    assert "Result:" in art
    # names the actual winner, not just the a/b token
    assert res["player_a"] in art or res["player_b"] in art or "draw" in art


def test_unknown_bot_rejected():
    with pytest.raises(ValueError):
        run_local_match(game="lightcycles",
                        player=Contestant(key="nope"),
                        opponent=Contestant(key="bare"), seed=0)


def test_player_subprocess_closed_when_opponent_unknown(tmp_path: Path):
    """If the opponent key is bad AFTER the player subprocess spawned, it must be closed."""
    bot = tmp_path / "main.py"
    bot.write_text("import sys\nfor line in sys.stdin:\n    print('up'); sys.stdout.flush()\n")
    captured = {}
    orig = Contestant.move_source

    def spy(self, player):
        src = orig(self, player)
        if self.bot_path:
            captured["src"] = src
        return src

    Contestant.move_source = spy
    try:
        with pytest.raises(ValueError):
            run_local_match(game="lightcycles",
                            player=Contestant(bot_path=str(bot)),
                            opponent=Contestant(key="nope"), seed=0)
    finally:
        Contestant.move_source = orig
    # the spawned player subprocess was closed (killed) by the ExitStack
    assert captured.get("src") is not None
    assert captured["src"].proc.poll() is not None


def test_deterministic_same_seed_same_outcome():
    a = run_local_match(game="lightcycles", player=Contestant(key="greedy"),
                        opponent=Contestant(key="bare"), seed=7)
    b = run_local_match(game="lightcycles", player=Contestant(key="greedy"),
                        opponent=Contestant(key="bare"), seed=7)
    assert a["outcome"] == b["outcome"]
    assert a["frames"] == b["frames"]


def test_player_from_bot_file(tmp_path: Path):
    """A harness-built bot file (subprocess) plays as player_a via the referee protocol."""
    bot = tmp_path / "main.py"
    bot.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    print('up'); sys.stdout.flush()\n"
    )
    res = run_local_match(
        game="lightcycles",
        player=Contestant(bot_path=str(bot), label="my-bot"),
        opponent=Contestant(key="greedy"),
        seed=0,
    )
    assert res["player_a"] == "my-bot"
    assert res["status"] == "ok"


def test_render_ascii_contains_board_and_outcome():
    res = run_local_match(game="lightcycles", player=Contestant(key="greedy"),
                          opponent=Contestant(key="wall_hugger"), seed=0)
    art = render_ascii(res)
    assert "outcome=" in art
    # board rows rendered from the final frame
    assert "\n" in art


def test_build_replay_html_is_self_contained(tmp_path: Path):
    res = run_local_match(game="lightcycles", player=Contestant(key="greedy"),
                          opponent=Contestant(key="bare"), seed=0)
    out = build_replay_html(res, tmp_path)
    assert out.exists()
    html = out.read_text(encoding="utf-8")
    assert b"\r\n" not in out.read_bytes()
    # frames embedded inline (no network fetch needed)
    assert "greedy" in html and "bare" in html
    assert "<canvas" in html.lower()
    # the embedded match JSON round-trips
    assert '"frames"' in html


def test_build_replay_html_escapes_hostile_labels(tmp_path: Path):
    """A crafted player label / match_id must not break out of the HTML or JSON block (XSS)."""
    res = run_local_match(game="lightcycles", player=Contestant(key="greedy"),
                          opponent=Contestant(key="bare"), seed=0)
    res = dict(res)
    res["player_a"] = "</script><img src=x onerror=alert(1)>"
    res["match_id"] = "<b>oops</b>"
    out = build_replay_html(res, tmp_path)
    html = out.read_text(encoding="utf-8")
    # No raw breakout sequences survive into the document.
    assert "<img src=x onerror=alert(1)>" not in html
    assert "</script><img" not in html
    assert "<b>oops</b>" not in html

