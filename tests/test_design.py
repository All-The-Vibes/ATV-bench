"""Design / identifiability gates for the rating engine (plan Section 5).

The engine may only publish a factored-out harness rating when the design actually
IDENTIFIES the harness effect apart from the base model. These structural gates decide
that BEFORE any fit is trusted:

  - crossover rank  : is there enough harness<->model crossover to separate the effects?
  - VIF / condition : is the ACTUAL design matrix near-collinear?
  - real roster     : the declared v1 roster is model-locked (Copilot->GPT, Claude Code->
                      Claude, Codex->unknown). If a harness cannot be re-hosted on a shared
                      base model, the engine must emit a harness+model BUNDLE (bundle_unit=
                      True, factor_out=False) — an HONEST bundled number, never a fake
                      factored-out one. This test is the GO/NO-GO for the whole section.

RED before src/atv_bench/design.py exists.
"""
from __future__ import annotations

import pytest

from atv_bench.design import (
    crossover_rank,
    design_report,
    roster_attribution_plan,
)


# ---------------------------------------------------------------------------
# 1. Crossover rank: >=2 models per harness and vice versa => identifiable.
# ---------------------------------------------------------------------------


def test_crossover_rank():
    """Structural gate. A design where each harness is observed with >=2 base models AND each
    base model with >=2 harnesses has full crossover rank (harness and model effects are
    separately estimable). A design lacking that crossover falls below the rank threshold and
    is reported not-identifiable — independent of any fitted numbers."""
    # Balanced 3x3 cross: fully identifiable.
    cells_ok = [("H0", "M0"), ("H0", "M1"), ("H1", "M0"), ("H1", "M1"),
                ("H2", "M0"), ("H2", "M1")]
    rep_ok = crossover_rank(cells_ok)
    assert rep_ok["identifiable"] is True
    assert rep_ok["rank"] >= rep_ok["rank_needed"]

    # Model-locked chain: each harness on its OWN single model => no crossover.
    cells_locked = [("H0", "M0"), ("H1", "M1"), ("H2", "M2")]
    rep_locked = crossover_rank(cells_locked)
    assert rep_locked["identifiable"] is False
    assert rep_locked["rank"] < rep_locked["rank_needed"]


# ---------------------------------------------------------------------------
# 2. VIF / condition-number gate on the ACTUAL design matrix.
# ---------------------------------------------------------------------------


def test_design_vif_gate():
    """Condition number / VIF computed on the real design matrix. A near-collinear design
    (harness almost perfectly predicts model) exceeds the collinearity ceiling and is reported
    attributed=False; a well-conditioned crossover design passes with attributed=True.

    Theory: VIF_j = 1/(1-R_j^2) where R_j^2 is the fit of column j on the others; perfect
    confounding drives R_j^2 -> 1 and VIF -> infinity (condition number blows up). The gate
    thresholds this structural quantity, so it fires regardless of the outcome data."""
    good = [("H0", "M0"), ("H0", "M1"), ("H1", "M0"), ("H1", "M1")]
    rep_good = design_report(good)
    assert rep_good["attributed"] is True
    assert rep_good["condition_number"] < rep_good["condition_ceiling"]
    assert max(rep_good["vif"].values()) < rep_good["vif_ceiling"]

    # H0 only ever on M0, H1 only ever on M1 (one shared crossover cell too few) -> collinear.
    bad = [("H0", "M0"), ("H0", "M0"), ("H1", "M1"), ("H1", "M1")]
    rep_bad = design_report(bad)
    assert rep_bad["attributed"] is False
    assert rep_bad["condition_number"] >= rep_bad["condition_ceiling"]


# ---------------------------------------------------------------------------
# 3. GO/NO-GO: the DECLARED v1 roster is model-locked => bundle, don't fake-factor.
# ---------------------------------------------------------------------------


def test_crossover_rank_on_real_roster():
    """Build the attribution plan for the ACTUAL v1 roster (harnesses.py):
        claude-code -> Claude, copilot-cli -> GPT, codex -> unknown.
    These are model-locked: a contributor cannot re-host Copilot CLI on Claude, etc., so the
    harness column is perfectly confounded with the model column and there is NO crossover.

    The engine's honest fallback (the GO/NO-GO): for a model-locked roster it must emit, for
    EACH harness, bundle_unit=True and factor_out=False — i.e. report a harness+model BUNDLE,
    never a fabricated factored-out theta. codex's 'unknown' model can never even back a
    published number (match_record.is_publishable), so it is additionally non-publishable.
    """
    roster = [
        ("claude-code", "claude"),
        ("copilot-cli", "gpt"),
        ("codex", "unknown"),
    ]
    plan = roster_attribution_plan(roster)
    assert plan["model_locked"] is True, "declared roster has no crossover — must be locked"
    assert plan["factor_out"] is False, "must NOT publish a factored-out number for a locked roster"
    for h, _m in roster:
        entry = plan["harnesses"][h]
        assert entry["bundle_unit"] is True, f"{h} must be reported as a harness+model bundle"
        assert entry["factor_out"] is False, f"{h} must not claim a factored-out theta"
    # codex's model is non-publishable ('unknown') and must be flagged as such.
    assert plan["harnesses"]["codex"]["publishable"] is False

    # Counterfactual: if the SAME harnesses could be re-hosted on a shared model (crossover),
    # the plan flips to factor_out=True — proving the lock is data-driven, not hardcoded.
    rehostable = [
        ("claude-code", "claude"), ("claude-code", "gpt"),
        ("copilot-cli", "claude"), ("copilot-cli", "gpt"),
    ]
    plan2 = roster_attribution_plan(rehostable)
    assert plan2["model_locked"] is False
    assert plan2["factor_out"] is True
