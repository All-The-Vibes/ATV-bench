"""CLI tests for `atv-bench run` (Lane C) — demo, discovery, and error paths."""
from __future__ import annotations

import json

from typer.testing import CliRunner

from atv_bench.cli import app

runner = CliRunner()


def test_run_demo_human():
    r = runner.invoke(app, ["run", "--demo"])
    assert r.exit_code == 0
    assert "canned but REAL" in r.stdout
    assert "verified=False" in r.stdout


def test_run_demo_json_envelope():
    r = runner.invoke(app, ["run", "--demo", "--json"])
    assert r.exit_code == 0
    env = json.loads(r.stdout)
    assert env["success"] is True
    assert env["data"]["verified"] is False
    assert len(env["data"]["players"]) == 2
    assert "next" in env["data"]


def test_run_list_games():
    r = runner.invoke(app, ["run", "--list-games"])
    assert r.exit_code == 0
    assert "lightcycles" in r.stdout
    assert "battlesnake" in r.stdout


def test_run_list_harnesses():
    r = runner.invoke(app, ["run", "--list-harnesses"])
    assert r.exit_code == 0
    assert "claude-code" in r.stdout
    assert "copilot-cli" in r.stdout


def test_run_missing_args_is_usage_exit_2():
    r = runner.invoke(app, ["run", "--a", "copilot-cli"])
    assert r.exit_code == 2


def test_run_unknown_game_exit_2_json():
    r = runner.invoke(app, ["run", "--a", "copilot-cli", "--b", "claude-code",
                            "--model", "m", "--game", "pong", "--json"])
    assert r.exit_code == 2
    env = json.loads(r.stdout)
    assert env["success"] is False
    assert env["error"]["code"] == "usage"
    assert env["error"]["exit_code"] == 2


def test_run_unknown_harness_exit_2():
    r = runner.invoke(app, ["run", "--a", "nope", "--b", "claude-code", "--model", "m"])
    assert r.exit_code == 2
