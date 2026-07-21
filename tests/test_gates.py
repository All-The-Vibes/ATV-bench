"""G5 + G6 — publication decision & quality/futility gates (RED-first).

PR17 computes the raw signals (FIT_EXCLUDED, data_sufficiency, n_min_for_power, per-harness
theta + CI) but never converts them into a fail-closed PUBLICATION decision. PR16's rulebook
does: it refuses to publish when infrastructure noise or thin data dominate (quality/futility
gates), and it only declares a winner when the contrast CI excludes a preregistered practical
margin, the direction is stable, and >=2 model policies agree.

This module ports those decision rules as PURE, deterministic functions over already-computed
statistics — no live LLM, no Docker.
"""
from __future__ import annotations

import pytest

from atv_bench.gates import (
    GateThresholds,
    QualityGateReport,
    evaluate_quality_gates,
    decide_contrast,
)


# --------------------------------------------------------------------------- #
# G6 — quality / futility gates
# --------------------------------------------------------------------------- #
def _ok_stats():
    return {
        "infrastructure_error_rate": 0.0,
        "eligible_n": 200,
        "min_trials_per_cell": 10,
        "referee_nondeterminism_rate": 0.0,
    }


def test_quality_gates_pass_on_clean_stats():
    rep = evaluate_quality_gates(_ok_stats())
    assert isinstance(rep, QualityGateReport)
    assert rep.passed is True
    assert rep.failures == []


def test_quality_gates_fail_on_high_infra_error_rate():
    s = _ok_stats()
    s["infrastructure_error_rate"] = 0.40  # 40% crashes
    rep = evaluate_quality_gates(s)
    assert rep.passed is False
    names = {f["gate"] for f in rep.failures}
    assert "infrastructure_error_rate" in names
    # each failure carries observed + threshold
    f = next(f for f in rep.failures if f["gate"] == "infrastructure_error_rate")
    assert f["observed"] == 0.40 and "threshold" in f


def test_quality_gates_fail_on_thin_eligible_n():
    s = _ok_stats()
    s["eligible_n"] = 3
    rep = evaluate_quality_gates(s)
    assert rep.passed is False
    assert any(f["gate"] == "eligible_n" for f in rep.failures)


def test_quality_gates_fail_on_referee_nondeterminism():
    s = _ok_stats()
    s["referee_nondeterminism_rate"] = 0.5
    rep = evaluate_quality_gates(s)
    assert rep.passed is False
    assert any(f["gate"] == "referee_nondeterminism_rate" for f in rep.failures)


def test_quality_gates_custom_thresholds():
    s = _ok_stats()
    s["eligible_n"] = 60
    strict = GateThresholds(min_eligible_n=100)
    rep = evaluate_quality_gates(s, thresholds=strict)
    assert rep.passed is False
    assert any(f["gate"] == "eligible_n" for f in rep.failures)


def test_quality_gate_report_is_serializable():
    rep = evaluate_quality_gates(_ok_stats())
    d = rep.to_dict()
    assert d["passed"] is True
    assert d["failures"] == []
    import json
    json.dumps(d)  # must not raise


# --------------------------------------------------------------------------- #
# G5 — winner / equivalence decision rule
# --------------------------------------------------------------------------- #
def test_decide_a_wins_when_ci_excludes_margin_and_stable_and_multipolicy():
    v = decide_contrast(diff=0.9, lo=0.5, hi=1.3, margin=0.1,
                        direction_stability=0.99, n_policies=2)
    assert v["verdict"] == "A_wins"


def test_decide_b_wins_symmetric():
    v = decide_contrast(diff=-0.9, lo=-1.3, hi=-0.5, margin=0.1,
                        direction_stability=0.99, n_policies=2)
    assert v["verdict"] == "B_wins"


def test_decide_inconclusive_when_ci_straddles_zero():
    v = decide_contrast(diff=0.1, lo=-0.4, hi=0.6, margin=0.1,
                        direction_stability=0.6, n_policies=2)
    assert v["verdict"] == "inconclusive"


def test_decide_equivalent_when_ci_inside_margin_band():
    v = decide_contrast(diff=0.0, lo=-0.05, hi=0.05, margin=0.1,
                        direction_stability=0.5, n_policies=2)
    assert v["verdict"] == "equivalent"


def test_decide_forced_inconclusive_with_single_policy():
    # CI clearly excludes margin, stable — but only ONE model policy -> cannot win.
    v = decide_contrast(diff=0.9, lo=0.5, hi=1.3, margin=0.1,
                        direction_stability=0.99, n_policies=1)
    assert v["verdict"] == "inconclusive"
    assert "policy" in v["reason"].lower()


def test_decide_inconclusive_when_direction_unstable():
    v = decide_contrast(diff=0.5, lo=0.2, hi=0.8, margin=0.1,
                        direction_stability=0.55, n_policies=2,
                        min_direction_stability=0.9)
    assert v["verdict"] == "inconclusive"


def test_decide_reason_is_present_and_stringy():
    for kw in (
        dict(diff=0.9, lo=0.5, hi=1.3, margin=0.1, direction_stability=0.99, n_policies=2),
        dict(diff=0.1, lo=-0.4, hi=0.6, margin=0.1, direction_stability=0.6, n_policies=2),
    ):
        v = decide_contrast(**kw)
        assert isinstance(v["reason"], str) and v["reason"]


# --------------------------------------------------------------------------- #
# Santa round 1 — quality gates must FAIL CLOSED on missing signals.
# --------------------------------------------------------------------------- #
def test_empty_stats_blob_fails_closed():
    """An empty stats blob must NOT pass publication gating. Missing load-bearing
    signals are unknowable, and the rulebook is fail-closed: absence of evidence is
    not evidence of quality."""
    report = evaluate_quality_gates({})
    assert report.passed is False
    assert any(f["gate"].startswith("missing") or "missing" in f.get("reason", "")
               for f in report.failures)


def test_partial_stats_blob_fails_closed():
    """A blob missing even one required signal fails closed."""
    incomplete = {
        "infrastructure_error_rate": 0.0,
        "eligible_n": 200,
        "min_trials_per_cell": 10,
        # referee_nondeterminism_rate missing
    }
    assert evaluate_quality_gates(incomplete).passed is False


def test_complete_in_threshold_stats_passes():
    """Regression pin: a complete, in-threshold blob still passes."""
    ok = {
        "infrastructure_error_rate": 0.0,
        "eligible_n": 200,
        "min_trials_per_cell": 10,
        "referee_nondeterminism_rate": 0.0,
    }
    assert evaluate_quality_gates(ok).passed is True
