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


def test_default_demo_bots_are_two_distinct_bots():
    """The head-to-head must be TWO different bots, not one bot playing itself.

    Root cause of the flat-line board: demo_match_cmd defaulted BOTH players to the
    single bundled greedy_survivor bot, so the match was deterministic self-play → a
    draw → zero ELO spread. The two default bot paths must differ.
    """
    from atv_bench.cli import _default_demo_bots

    a_path, b_path = _default_demo_bots()
    assert a_path != b_path, "both demo players default to the same bot file (self-play)"
    from pathlib import Path
    assert Path(a_path).is_file() and Path(b_path).is_file()


def test_default_demo_match_is_decisive_not_a_draw():
    """With two distinct default bots, the deterministic demo match must have a winner.

    A decisive result is what produces a real ELO spread on the board; a draw between
    two 1500 players is a zero-update no-op (the flat line the user reported).
    """
    result = runner.invoke(app, ["demo-match", "--no-live", "--no-board"])
    assert result.exit_code == 0, result.output
    assert "wins" in result.output.lower(), (
        "default demo match was not decisive (still self-play draw?):\n" + result.output
    )


def test_demo_board_shows_distinct_fingerprints_for_the_two_players():
    """Each demo player's board row must carry its OWN fingerprint, and the two must
    differ — a real head-to-head between different harnesses, not two identical rows."""
    result = runner.invoke(app, [
        "demo-match", "--no-live", "--board",
        "--a-name", "ATV-StarterKit", "--b-name", "ATV-Phoenix",
    ])
    assert result.exit_code == 0, result.output
    board = result.output.split("=== Leaderboard ===", 1)[-1]
    assert "ATV-StarterKit" in board and "ATV-Phoenix" in board, result.output

    # Pull the two players' board lines and assert their ELO differs (decisive result
    # recorded, not a flat 1500/1500 draw).
    import re
    elos = {}
    for line in board.splitlines():
        for name in ("ATV-StarterKit", "ATV-Phoenix"):
            if name in line:
                m = re.search(r"(\d+)\s*ELO", line)
                if m:
                    elos[name] = int(m.group(1))
    assert set(elos) == {"ATV-StarterKit", "ATV-Phoenix"}, board
    assert elos["ATV-StarterKit"] != elos["ATV-Phoenix"], (
        "both players show identical ELO — self-play draw, not a real head-to-head:\n" + board
    )


# --- browser live-stream wiring (default surface) ---

def test_demo_match_terminal_mode_still_works():
    # Backward compat: --terminal keeps the in-terminal feed (no server, no browser).
    result = runner.invoke(app, ["demo-match", "--terminal", "--no-live", "--no-board"])
    assert result.exit_code == 0, result.output
    assert "turn" in result.output.lower()
    assert "wins" in result.output.lower() or "draw" in result.output.lower()


def test_demo_match_browser_no_open_prints_url_and_returns():
    # Default surface is the browser stream. --no-open serves without launching a browser
    # and without blocking, printing the local URL (headless/CI friendly).
    result = runner.invoke(app, ["demo-match", "--no-open"])
    assert result.exit_code == 0, result.output
    assert "http://127.0.0.1:" in result.output
    assert "/" in result.output
