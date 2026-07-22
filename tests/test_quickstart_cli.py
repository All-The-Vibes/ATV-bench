"""Unit 5 (quickstart): the `atv-bench quickstart` CLI command.

Tests the full UX wiring with the engine + probe monkeypatched, so no Docker/live CLI is
touched: detect harness -> fingerprint render -> model select -> run engine -> print scientific
summary + board link. Non-interactive (--model + --yes) makes it fully scriptable.
"""
from __future__ import annotations

import json
import types

import pytest
from typer.testing import CliRunner

from atv_bench.cli import app
from atv_bench.gates import QualityGateReport
from atv_bench.lift import LiftResult
from atv_bench.pergame import GameScore
from atv_bench.quickstart import QuickstartResult

runner = CliRunner()


def _fake_probe(harness_key="claude-code"):
    return types.SimpleNamespace(
        manifest={"harness": harness_key, "model": "claude-sonnet-4-6", "gstack": False,
                  "skills": [], "nested_skills": [], "tools": [], "mcps": [], "plugins": [],
                  "custom_agents_count": 0, "cli_version": "1.0", "unknown_runtime": [],
                  "unknown": [], "probe_version": "1.0"},
        log=[],
    )


def _fake_result(tmp_board):
    return QuickstartResult(
        harness="claude-code", baseline="bare:claude-code", model="claude-sonnet-4-6",
        games=["lightcycles", "chess", "ants"], n_matches=6,
        per_game=[
            GameScore("lightcycles", 2, 1.0, 1.0, None, None, False),
            GameScore("chess", 2, 0.5, 0.0, None, None, False),
            GameScore("ants", 2, 0.0, -1.0, None, None, True),
        ],
        overall=LiftResult("claude-code", "bare:claude-code", "claude-sonnet-4-6",
                           lift=0.42, lo=0.1, hi=0.7, n_boot_used=500),
        gate_report=QualityGateReport(passed=False, failures=[{"gate": "eligible_n"}]),
        credible=False, failures=[], board_path=tmp_board, board_url=tmp_board.as_uri(),
        corpus_path=tmp_board / "rating_matches.jsonl",
    )


def _patch(monkeypatch, tmp_path, capture=None):
    board = tmp_path / "_board"; board.mkdir()
    monkeypatch.setattr("atv_bench.cli._probe_or_exit", lambda home, harness: _fake_probe(harness or "claude-code"))

    def fake_eval(**kwargs):
        if capture is not None:
            capture.update(kwargs)
        return _fake_result(board)

    monkeypatch.setattr("atv_bench.quickstart.run_quickstart_eval", fake_eval)
    # never build a real live executor
    monkeypatch.setattr("atv_bench.quickstart.live_match_executor", lambda **k: (lambda **kk: {}))
    return board


def test_quickstart_json_non_interactive(monkeypatch, tmp_path):
    """--model + --yes + --json runs headless and emits the machine-readable result."""
    _patch(monkeypatch, tmp_path)
    r = runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model",
                            "claude-sonnet-4-6", "--yes", "--json"])
    assert r.exit_code == 0, r.output
    doc = json.loads(r.stdout)
    assert doc["harness"] == "claude-code"
    assert doc["model"] == "claude-sonnet-4-6"
    assert doc["credible"] is False
    assert {g["game"] for g in doc["per_game"]} == {"lightcycles", "chess", "ants"}


def test_quickstart_human_shows_scores_and_link(monkeypatch, tmp_path):
    """Human output shows the fingerprint, the per-game + overall scores, and the board link."""
    _patch(monkeypatch, tmp_path)
    r = runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model",
                            "claude-sonnet-4-6", "--yes"])
    assert r.exit_code == 0, r.output
    out = r.output
    assert "lightcycles" in out and "chess" in out  # per-game rows
    assert "lift" in out.lower()                     # overall metric
    assert "provisional" in out.lower() or "not" in out.lower()  # gate verdict surfaced
    assert "scorecard.html" in out or "Leaderboard:" in out       # the actual scorecard link


def test_quickstart_model_flag_bypasses_picker(monkeypatch, tmp_path):
    """--model routes straight through; the interactive picker is never invoked."""
    cap = {}
    _patch(monkeypatch, tmp_path, capture=cap)
    called = {"picker": False}
    monkeypatch.setattr("atv_bench.interactive._questionary_select",
                        lambda *a, **k: called.__setitem__("picker", True))
    r = runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model", "opus-x",
                            "--yes", "--json"])
    assert r.exit_code == 0, r.output
    assert called["picker"] is False
    assert cap["model"] == "opus-x"


def test_quickstart_default_is_quick_trio(monkeypatch, tmp_path):
    """Default run uses a small quick-game set; --all opts into all 20 live games."""
    cap = {}
    _patch(monkeypatch, tmp_path, capture=cap)
    runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model", "m", "--yes", "--json"])
    quick_n = len(cap["games"])
    assert 1 <= quick_n <= 5, f"quick default should be a small trio, got {quick_n}"

    cap.clear()
    runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model", "m", "--yes",
                        "--all", "--json"])
    from atv_bench.games import live_keys
    assert set(cap["games"]) == set(live_keys())


def test_quickstart_explicit_games(monkeypatch, tmp_path):
    cap = {}
    _patch(monkeypatch, tmp_path, capture=cap)
    runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model", "m", "--yes",
                        "--game", "lightcycles", "--game", "chess", "--json"])
    assert set(cap["games"]) == {"lightcycles", "chess"}


def test_quickstart_rejects_unknown_game(monkeypatch, tmp_path):
    """An invalid --game is a USAGE error (exit 2), not an environment error (exit 5)."""
    _patch(monkeypatch, tmp_path)
    r = runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model", "m", "--yes",
                            "--game", "not-a-real-game", "--json"])
    assert r.exit_code == 2, r.output
    assert "not live" in r.output.lower() or "not-a-real-game" in r.output


def test_quickstart_rejects_bad_repeats(monkeypatch, tmp_path):
    """--repeats < 1 is a usage error."""
    _patch(monkeypatch, tmp_path)
    r = runner.invoke(app, ["quickstart", "--harness", "claude-code", "--model", "m", "--yes",
                            "--repeats", "0", "--json"])
    assert r.exit_code == 2, r.output
