"""CLI tests for the local play/visualization UX (`atv-bench play` + `atv-bench bots`).

These cover the demo-failure fix: a first-time user must be able to list the opponent
series and run a real (not mocked) refereed match locally, rendered as ASCII and an HTML
replay, with clear copy-paste commands.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from atv_bench.cli import app

runner = CliRunner()


def test_bots_lists_the_series():
    res = runner.invoke(app, ["bots"])
    assert res.exit_code == 0, res.output
    for key in ("greedy", "wall_hugger", "bare"):
        assert key in res.output


def test_bots_json():
    res = runner.invoke(app, ["bots", "--json"])
    assert res.exit_code == 0
    payload = json.loads(res.output)
    keys = {b["key"] for b in payload}
    assert {"greedy", "wall_hugger", "bare"} <= keys


def test_play_named_vs_named_writes_replay(tmp_path):
    out = tmp_path / "match"
    res = runner.invoke(app, [
        "play", "--player", "greedy", "--opponent", "wall_hugger",
        "--out", str(out), "--no-open",
    ])
    assert res.exit_code == 0, res.output
    assert "outcome=" in res.output
    assert (out / "replay.html").exists()


def test_play_unknown_bot_fails_closed(tmp_path):
    res = runner.invoke(app, [
        "play", "--player", "nope", "--opponent", "greedy",
        "--out", str(tmp_path / "m"), "--no-open",
    ])
    assert res.exit_code != 0
    assert "nope" in res.output


def test_play_unknown_game_fails_closed(tmp_path):
    res = runner.invoke(app, [
        "play", "--game", "battlesnake", "--player", "greedy",
        "--opponent", "bare", "--out", str(tmp_path / "m"), "--no-open",
    ])
    assert res.exit_code != 0


def test_play_with_bot_file(tmp_path):
    bot = tmp_path / "main.py"
    bot.write_text("import sys\nfor line in sys.stdin:\n    print('up'); sys.stdout.flush()\n")
    out = tmp_path / "match"
    res = runner.invoke(app, [
        "play", "--player-bot", str(bot), "--opponent", "greedy",
        "--out", str(out), "--no-open",
    ])
    assert res.exit_code == 0, res.output
    assert (out / "replay.html").exists()


def test_play_bot_vs_bot(tmp_path):
    """Two harness-built bot files can face off head-to-head via --opponent-bot."""
    a = tmp_path / "a.py"; b = tmp_path / "b.py"
    src = "import sys\nfor line in sys.stdin:\n    print('up'); sys.stdout.flush()\n"
    a.write_text(src); b.write_text(src)
    out = tmp_path / "m"
    res = runner.invoke(app, [
        "play", "--player-bot", str(a), "--opponent-bot", str(b),
        "--out", str(out), "--no-open",
    ])
    assert res.exit_code == 0, res.output
    assert (out / "replay.html").exists()
