"""Tests for `atv-bench demo-match` — the head-to-head demo command (Acts 1-3).

Runs two local bots head-to-head, streams a live Tron frame feed, then shows the
leaderboard + insights. Tests drive it through typer's CliRunner with --no-live (no
sleeps) and cover both the pure-match view and the board+insights view. RED first.
"""
from __future__ import annotations

from typer.testing import CliRunner

from atv_bench.cli import app

runner = CliRunner()


def test_demo_match_runs_with_bundled_bots_no_live_no_board():
    # Zero-arg friendly: bundled sample bots play, frames render, an outcome is declared.
    result = runner.invoke(app, ["demo-match", "--no-live", "--no-board"])
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "turn" in out            # live-feed frames rendered
    assert "wins" in out or "draw" in out  # an adjudicated outcome


def test_demo_match_names_both_harnesses():
    result = runner.invoke(app, [
        "demo-match", "--no-live", "--no-board",
        "--a-name", "ATV-StarterKit", "--b-name", "ATV-Phoenix",
    ])
    assert result.exit_code == 0, result.output
    assert "ATV-StarterKit" in result.output
    assert "ATV-Phoenix" in result.output


def test_demo_match_with_board_shows_leaderboard_and_insights():
    result = runner.invoke(app, ["demo-match", "--no-live", "--board"])
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "leaderboard" in out or "rank" in out
    assert "insight" in out


def test_demo_match_board_reflects_the_actual_match_players():
    # The demo narrative: you play a match, then see IT on the board. The two players
    # who just played must appear in the leaderboard/insights — not only a canned roster.
    result = runner.invoke(app, [
        "demo-match", "--no-live", "--board",
        "--a-name", "ATV-StarterKit", "--b-name", "ATV-Phoenix",
    ])
    assert result.exit_code == 0, result.output
    # Split off the pre-board match feed; assert against the board section only.
    board_section = result.output.split("=== Leaderboard ===", 1)[-1]
    assert "ATV-StarterKit" in board_section, result.output
    assert "ATV-Phoenix" in board_section, result.output


def test_demo_match_accepts_custom_bot_paths(tmp_path):
    bot = tmp_path / "mybot.py"
    bot.write_text(
        "import sys\n"
        "for line in sys.stdin:\n"
        "    print('up', flush=True)\n"
    )
    result = runner.invoke(app, [
        "demo-match", "--no-live", "--no-board",
        "--a-bot", str(bot), "--b-bot", str(bot),
    ])
    assert result.exit_code == 0, result.output
