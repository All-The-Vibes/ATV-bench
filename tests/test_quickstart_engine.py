"""Unit 4 (quickstart): the eval orchestrator engine.

Given a harness + model, plan harness-vs-its-bare-control across N games, execute each match
(via an INJECTED executor so tests need no Docker), persist a rating corpus, and compute the
overall + per-game scientific scores plus the G5/G6 gate verdict and a leaderboard link.
"""
from __future__ import annotations

import pytest

from pathlib import Path

from atv_bench.quickstart import QuickstartResult, run_quickstart_eval


def _stub_executor(*, plays):
    """Return a canned executor: `plays[game]` is the harness's score (1 win / 0 loss / .5 tie)
    for every match of that game. Records the calls it received."""
    calls = []

    def execute(*, harness_a, harness_b, game, model, seed, index):
        calls.append({"harness_a": harness_a, "harness_b": harness_b, "game": game})
        # orientation: the row is authored from player_a's perspective.
        harness = "claude-code"
        s = plays[game]
        score_a = s if harness_a == harness else (1.0 - s)
        return {
            "harness_a": harness_a, "harness_b": harness_b,
            "model_a": model, "model_b": model,
            "score_a": float(score_a), "game": game,
            "match_id": f"{game}-{index}",
        }

    execute.calls = calls
    return execute


def test_engine_runs_all_requested_games(tmp_path):
    games = ["lightcycles", "chess", "ants"]
    ex = _stub_executor(plays={"lightcycles": 1.0, "chess": 1.0, "ants": 0.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=games, repeats=2,
        store=tmp_path / "league", execute=ex,
    )
    assert isinstance(res, QuickstartResult)
    # 3 games x 2 repeats = 6 matches
    assert len(ex.calls) == 6
    assert {c["game"] for c in ex.calls} == set(games)
    # every match is harness vs its bare control
    for c in ex.calls:
        assert {c["harness_a"], c["harness_b"]} == {"claude-code", "bare:claude-code"}


def test_engine_persists_corpus_and_scores(tmp_path):
    games = ["lightcycles", "chess"]
    ex = _stub_executor(plays={"lightcycles": 1.0, "chess": 0.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=games, repeats=5,
        store=tmp_path / "league", execute=ex,
    )
    # a rating corpus was written
    corpus = (tmp_path / "league" / "rating_matches.jsonl")
    assert corpus.exists()
    # per-game breakdown present for both games
    assert {g.game for g in res.per_game} == set(games)
    lc = next(g for g in res.per_game if g.game == "lightcycles")
    assert lc.win_rate == 1.0
    ch = next(g for g in res.per_game if g.game == "chess")
    assert ch.win_rate == 0.0
    # overall lift computed and finite
    assert res.overall is not None
    assert res.overall.lift == res.overall.lift


def test_engine_builds_board_link(tmp_path):
    ex = _stub_executor(plays={"lightcycles": 1.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=5,
        store=tmp_path / "league", execute=ex,
    )
    # the scorecard IS the leaderboard link — a self-contained page with the real scores
    assert res.board_url is not None and res.board_url.endswith("scorecard.html")
    scorecard = tmp_path / "league" / "scorecard.html"
    assert scorecard.exists()
    body = scorecard.read_text()
    assert "lightcycles" in body and "harness lift" in body.lower()
    # machine-readable result also written
    assert (tmp_path / "league" / "quickstart_result.json").exists()


def test_engine_gate_verdict_provisional_on_thin_corpus(tmp_path):
    """A tiny corpus fails the G5/G6 gates -> result marked provisional, not credible."""
    ex = _stub_executor(plays={"lightcycles": 1.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=2,
        store=tmp_path / "league", execute=ex,
    )
    assert res.credible is False
    assert res.gate_report is not None and res.gate_report.passed is False


def test_engine_failed_arena_recorded_not_fatal(tmp_path):
    """An executor that raises on one game records the failure and continues; the infra-error
    rate rises and the run is marked provisional rather than crashing."""
    def execute(*, harness_a, harness_b, game, model, seed, index):
        if game == "chess":
            raise RuntimeError("docker exploded")
        return {"harness_a": harness_a, "harness_b": harness_b, "model_a": model,
                "model_b": model, "score_a": 1.0 if harness_a == "claude-code" else 0.0,
                "game": game, "match_id": f"{game}-{index}"}

    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles", "chess"], repeats=3,
        store=tmp_path / "league", execute=execute,
    )
    assert res.failures  # chess failures recorded
    assert any(f["game"] == "chess" for f in res.failures)
    # lightcycles still scored
    assert any(g.game == "lightcycles" for g in res.per_game)


def test_engine_deterministic_under_seed(tmp_path):
    ex1 = _stub_executor(plays={"lightcycles": 1.0, "ants": 0.0})
    ex2 = _stub_executor(plays={"lightcycles": 1.0, "ants": 0.0})
    r1 = run_quickstart_eval(harness="claude-code", model="sonnet",
                             games=["lightcycles", "ants"], repeats=2, seed=7,
                             store=tmp_path / "a", execute=ex1)
    r2 = run_quickstart_eval(harness="claude-code", model="sonnet",
                             games=["lightcycles", "ants"], repeats=2, seed=7,
                             store=tmp_path / "b", execute=ex2)
    # same plan order (deterministic schedule under seed)
    assert [c["game"] for c in ex1.calls] == [c["game"] for c in ex2.calls]
    # AND identical scientific output under the same seed (not just plan order)
    assert [g.win_rate for g in sorted(r1.per_game, key=lambda x: x.game)] == \
           [g.win_rate for g in sorted(r2.per_game, key=lambda x: x.game)]
    if r1.overall is not None and r2.overall is not None:
        assert r1.overall.lift == r2.overall.lift
        assert (r1.overall.lo, r1.overall.hi) == (r2.overall.lo, r2.overall.hi)


def test_engine_relative_store_still_links(tmp_path, monkeypatch):
    """A RELATIVE --store default must still produce a working scorecard URL (as_uri needs an
    absolute path; the engine resolves it). Regression guard for the headline link promise."""
    monkeypatch.chdir(tmp_path)
    ex = _stub_executor(plays={"lightcycles": 1.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=5,
        store=Path("./quickstart-league"), execute=ex,  # RELATIVE
    )
    assert res.board_url is not None and res.board_url.startswith("file:///")
    assert res.board_url.endswith("scorecard.html")


def test_engine_credible_on_powered_clean_corpus(tmp_path):
    """A powered corpus with repeats (referee agreement observed) and no failures CAN pass the
    gates — the CREDIBLE path is reachable, not dead."""
    # 3 games x 20 repeats = 60 eligible, >=5 per cell, 0 infra failures, referee deterministic.
    ex = _stub_executor(plays={"lightcycles": 1.0, "chess": 0.0, "ants": 1.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet",
        games=["lightcycles", "chess", "ants"], repeats=20,
        store=tmp_path / "league", execute=ex,
    )
    assert res.gate_report is not None
    assert res.credible is True, res.gate_report.to_dict()


def test_scorecard_escapes_malicious_names(tmp_path):
    """The scorecard HTML-escapes dynamic values (harness/model/game), so a crafted name can't
    inject markup into the rendered leaderboard page."""
    def execute(*, harness_a, harness_b, game, model, seed, index):
        return {"harness_a": harness_a, "harness_b": harness_b, "model_a": model,
                "model_b": model, "score_a": 1.0 if harness_a == "claude-code" else 0.0,
                "game": game, "match_id": f"{game}-{index}"}
    res = run_quickstart_eval(
        harness="claude-code", model="<script>alert(1)</script>",
        games=["<img src=x onerror=alert(1)>"], repeats=5,
        store=tmp_path / "league", execute=execute,
    )
    body = (tmp_path / "league" / "scorecard.html").read_text()
    assert "<script>alert(1)</script>" not in body
    assert "<img src=x onerror" not in body
    assert "&lt;script&gt;" in body  # escaped form present
