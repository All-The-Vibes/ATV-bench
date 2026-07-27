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


def test_seat_bias_not_flagged_as_referee_nondeterminism(tmp_path):
    """A harness with a real, DETERMINISTIC seat bias (always wins as player_a, always loses as
    player_b) must NOT be counted as referee nondeterminism — same seating is consistent, so the
    referee is deterministic and a powered corpus stays credible."""
    def execute(*, harness_a, harness_b, game, model, seed, index):
        # deterministic by seat: harness (claude-code) always wins when it is seat A.
        score_a = 1.0 if harness_a == "claude-code" else 1.0  # player_a always wins => seat bias
        return {"harness_a": harness_a, "harness_b": harness_b, "model_a": model,
                "model_b": model, "score_a": score_a, "game": game, "match_id": f"{game}-{index}"}
    from atv_bench.quickstart import _measure_referee_nondeterminism
    from atv_bench.runner import load_rating_rows
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet",
        games=["lightcycles", "chess", "ants"], repeats=20,
        store=tmp_path / "league", execute=execute,
    )
    rows = load_rating_rows(tmp_path / "league" / "rating_matches.jsonl")
    # same-orientation cells are internally consistent => 0.0 nondeterminism (not 1.0)
    assert _measure_referee_nondeterminism(rows, "claude-code") == 0.0
    # and the deterministic seat-biased corpus is NOT failed on a phantom nondeterminism gate
    assert res.gate_report is not None
    nondet_fail = any("nondeterminism" in f.get("gate", "") for f in res.gate_report.failures)
    assert not nondet_fail, res.gate_report.to_dict()


def test_match_event_carries_match_out_and_seats(tmp_path):
    """T5: the per-match progress event carries the EXACT match_out dir the executor uses plus the
    seat labels, so the live watcher binds to the right directory (never a glob of store_dir)."""
    ex = _stub_executor(plays={"lightcycles": 1.0})
    # the executor exposes where it writes each match's artifacts (mirrors live_match_executor).
    base_out = tmp_path / "match-artifacts"
    ex.base_out = base_out

    events: list[dict] = []
    run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=2,
        store=tmp_path / "league", execute=ex, progress=events.append,
    )
    match_events = [e for e in events if e.get("phase") == "match"]
    assert match_events, "no match-phase progress events emitted"
    for e in match_events:
        # exact per-match directory, not a glob
        expected = str(base_out / f"{e['game']}-{e['index']}")
        assert e["match_out"] == expected
        # seat labels: index0=harness_a=blue, index1=harness_b=red
        seats = e["seats"]
        assert seats["a"] == e["harness_a"] and seats["b"] == e["harness_b"]
        assert seats["a_color"] == "blue" and seats["b_color"] == "red"


def test_match_event_match_out_none_without_base_out(tmp_path):
    """An executor with no known artifact dir (a bare stub) yields match_out=None — the watcher
    simply has nothing to bind to, but the event still carries seat labels."""
    ex = _stub_executor(plays={"lightcycles": 1.0})  # no .base_out attribute
    events: list[dict] = []
    run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=1,
        store=tmp_path / "league", execute=ex, progress=events.append,
    )
    match_events = [e for e in events if e.get("phase") == "match"]
    assert match_events
    for e in match_events:
        assert e["match_out"] is None
        assert e["seats"]["a"] == e["harness_a"] and e["seats"]["b"] == e["harness_b"]


def test_live_url_defaults_none_and_round_trips(tmp_path):
    """QuickstartResult.live_url defaults to None (the engine itself never starts a server) and
    round-trips through to_dict for headless (--json) consumers."""
    ex = _stub_executor(plays={"lightcycles": 1.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=5,
        store=tmp_path / "league", execute=ex,
    )
    assert res.live_url is None
    d = res.to_dict()
    assert "live_url" in d and d["live_url"] is None
    # explicitly-set live_url round-trips too
    res.live_url = "http://127.0.0.1:8731/live.html"
    assert res.to_dict()["live_url"] == "http://127.0.0.1:8731/live.html"


def test_headless_json_path_live_url_none(tmp_path):
    """The headless (--yes/--json) engine path returns live_url=None: no server, and the
    serialized result advertises no live link."""
    ex = _stub_executor(plays={"lightcycles": 1.0})
    res = run_quickstart_eval(
        harness="claude-code", model="sonnet", games=["lightcycles"], repeats=5,
        store=tmp_path / "league", execute=ex,
    )
    assert res.live_url is None
    import json as _json
    persisted = _json.loads((tmp_path / "league" / "quickstart_result.json").read_text())
    assert persisted["live_url"] is None


def test_second_run_to_same_store_scores_only_its_own_rows(tmp_path):
    """Re-running quickstart against the SAME store must score only the CURRENT run — prior
    runs accumulated in the corpus must NOT blend into per-game/overall/gate scores."""
    store = tmp_path / "league"
    ex1 = _stub_executor(plays={"lightcycles": 1.0})   # run 1: harness wins all
    r1 = run_quickstart_eval(harness="claude-code", model="sonnet", games=["lightcycles"],
                             repeats=5, store=store, execute=ex1)
    assert r1.n_matches == 5
    assert next(g for g in r1.per_game if g.game == "lightcycles").win_rate == 1.0

    ex2 = _stub_executor(plays={"lightcycles": 0.0})   # run 2 (SAME store): harness loses all
    r2 = run_quickstart_eval(harness="claude-code", model="sonnet", games=["lightcycles"],
                             repeats=5, store=store, execute=ex2)
    # r2 reflects ONLY run 2 (5 matches, 0.0 win-rate) — NOT the blended 10 matches / 0.5.
    assert r2.n_matches == 5, "second run must not count run-1 rows"
    assert next(g for g in r2.per_game if g.game == "lightcycles").win_rate == 0.0
