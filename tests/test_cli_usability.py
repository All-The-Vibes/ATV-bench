"""Tests for the local-usability CLI surface (goal: install + run + see rankings).

Covers the commands a user needs to (a) discover games, (b) build/view the leaderboard
locally so they can see where they and others rank, and (c) check their environment is
ready — without cloning the repo. Plus the game-validation guard that stops a submission
for a game with no trusted arena.
"""
from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from atv_bench.cli import app

runner = CliRunner()


def _home(tmp_path: Path) -> Path:
    home = tmp_path / ".claude"
    (home / "skills" / "gstack").mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps({"model": "claude-opus-4-8"}))
    return home


# --- games -----------------------------------------------------------------

def test_games_lists_live_and_planned():
    result = runner.invoke(app, ["games"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "lightcycles" in out
    # the live arena is marked live/available; battlesnake is marked planned
    assert "battlesnake" in out
    assert "planned" in out.lower()


def test_games_json():
    result = runner.invoke(app, ["games", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    keys = {g["key"]: g for g in payload}
    assert keys["lightcycles"]["live"] is True
    # robocode is non-live (reusable referee, blocked by an upstream empty-scores crash).
    assert keys["robocode"]["live"] is False


# --- submit game validation ------------------------------------------------

def test_submit_rejects_planned_game(tmp_path):
    home = _home(tmp_path)
    bot = tmp_path / "main.py"
    bot.write_text("def move(s):\n    return 'up'\n")
    result = runner.invoke(app, [
        "submit", str(bot), "--game", "robocode", "--dry-run",
        "--home", str(home), "--identity", "octocat",
        "--out", str(tmp_path / "submission.json"),
    ])
    assert result.exit_code != 0, result.output
    assert "planned" in result.output.lower() or "not playable" in result.output.lower()


def test_submit_accepts_default_live_game(tmp_path):
    home = _home(tmp_path)
    bot = tmp_path / "main.py"
    bot.write_text("def move(s):\n    return 'up'\n")
    out_json = tmp_path / "submission.json"
    # no --game: default must be a live game (lightcycles), so this succeeds
    result = runner.invoke(app, [
        "submit", str(bot), "--dry-run",
        "--home", str(home), "--identity", "octocat", "--out", str(out_json),
    ])
    assert result.exit_code == 0, result.output
    rec = json.loads(out_json.read_text())
    assert rec["game"] == "lightcycles"


# --- board -----------------------------------------------------------------

def test_board_demo_builds_populated_board(tmp_path):
    out_dir = tmp_path / "site"
    result = runner.invoke(app, ["board", "--demo", "--out", str(out_dir), "--no-open"])
    assert result.exit_code == 0, result.output
    board = out_dir / "leaderboard.json"
    index = out_dir / "index.html"
    assert board.exists(), "board must write leaderboard.json"
    assert index.exists(), "board must write the viewer index.html (clone-free)"
    doc = json.loads(board.read_text())
    assert doc["schema_version"] == 1
    assert len(doc["rows"]) >= 2, "demo board should have several ranked rows"


def test_board_from_local_store(tmp_path):
    # a real local store with one submission -> that entrant appears on the board
    from atv_bench.store import LeagueStore
    store_dir = tmp_path / "league"
    store = LeagueStore(str(store_dir))
    store.add_submission({
        "identity": "octocat", "game": "lightcycles",
        "bot_sha256": "a" * 64, "bot_filename": "main.py",
        "pr_url": "https://github.com/All-The-Vibes/ATV-bench/pull/1",
        "logs_url": "https://all-the-vibes.github.io/ATV-bench/logs/1",
        "fingerprint": {
            "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
            "skills": ["gstack"], "mcps": [], "plugins": [],
            "custom_agents_count": 3, "probe_version": "1.0.0", "unknown": [],
        },
    }, bot_source="def move(s):\n    return 'up'\n")
    out_dir = tmp_path / "site"
    result = runner.invoke(app, [
        "board", "--store", str(store_dir), "--out", str(out_dir), "--no-open",
    ])
    assert result.exit_code == 0, result.output
    doc = json.loads((out_dir / "leaderboard.json").read_text())
    assert any(r["identity"] == "octocat" for r in doc["rows"])


# --- doctor ----------------------------------------------------------------

def test_doctor_reports_checks(tmp_path):
    home = _home(tmp_path)
    result = runner.invoke(app, ["doctor", "--home", str(home)])
    # doctor never crashes; it reports readiness. Exit 0 even if optional tools missing.
    assert result.exit_code == 0, result.output
    out = result.output.lower()
    assert "python" in out
    assert "harness" in out  # reports whether a harness config was detected
