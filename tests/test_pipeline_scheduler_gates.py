"""Follow-up 3 (PR #19): wire scheduler.py (G1) and gates.py (G5/G6) into the live pipeline.

scheduler.build_paired_schedule and gates.evaluate_quality_gates existed as library+tests
but were never called by the CLI. These tests pin the wiring: a `plan-schedule` command that
emits a deterministic side-balanced plan, and a `gate_corpus` seam + `rate --enforce-gates`
that refuses to publish a thin/noisy corpus.
"""
from __future__ import annotations

import json

from typer.testing import CliRunner

from atv_bench.cli import app
from atv_bench.pipeline import gate_corpus

runner = CliRunner()


def test_plan_schedule_cli_deterministic():
    """`plan-schedule` emits a side-balanced plan; identical seed => identical JSON (AC3.1)."""
    args = [
        "plan-schedule", "--harness", "claude-code", "--harness", "bare:claude-code",
        "--game", "lightcycles", "--repeats", "2", "--seed", "7", "--json",
    ]
    r1 = runner.invoke(app, args)
    r2 = runner.invoke(app, args)
    assert r1.exit_code == 0, r1.output
    assert r1.stdout == r2.stdout  # deterministic under seed
    plan = json.loads(r1.stdout)
    # 1 unordered pair x 1 game x 2 repeats = 2 matches, side-balanced (one each seat).
    assert len(plan) == 2
    sides = sorted(m["side_index"] for m in plan)
    assert sides == [0, 1]


def test_gate_corpus_blocks_thin_corpus():
    """gate_corpus fails closed on an under-powered corpus (AC3.2/3.4)."""
    stats = {
        "infrastructure_error_rate": 0.0,
        "eligible_n": 3,               # < min_eligible_n (50)
        "min_trials_per_cell": 1,      # < 5
        "referee_nondeterminism_rate": 0.0,
    }
    report = gate_corpus(stats)
    assert report.passed is False
    gates = {f["gate"] for f in report.failures}
    assert "eligible_n" in gates


def test_gate_corpus_passes_when_powered():
    """A powered, clean corpus passes every gate (AC3.2)."""
    stats = {
        "infrastructure_error_rate": 0.0,
        "eligible_n": 200,
        "min_trials_per_cell": 10,
        "referee_nondeterminism_rate": 0.0,
    }
    assert gate_corpus(stats).passed is True


def test_rate_enforce_gates_blocks(tmp_path):
    """`rate --enforce-gates` exits non-zero on a thin corpus (AC3.4).

    Two scored rows is far under the min_eligible_n gate, so with --enforce-gates the
    command must refuse to publish rather than emit a phantom-precision board.
    """
    store = tmp_path / "corpus"
    store.mkdir()
    rows = [
        {"player_a": "claude-code", "player_b": "bare:claude-code", "match_id": "m0",
         "outcome": "a_wins", "harness_a": "claude-code", "harness_b": "bare:claude-code",
         "model_a": "sonnet", "model_b": "sonnet", "score_a": 1.0},
        {"player_a": "claude-code", "player_b": "bare:claude-code", "match_id": "m1",
         "outcome": "b_wins", "harness_a": "claude-code", "harness_b": "bare:claude-code",
         "model_a": "sonnet", "model_b": "sonnet", "score_a": 0.0},
    ]
    (store / "matches.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    r = runner.invoke(app, ["rate", "--store", str(store), "--enforce-gates"])
    assert r.exit_code != 0
    assert "gate" in r.output.lower()
