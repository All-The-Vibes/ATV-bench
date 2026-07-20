"""Harness LIFT over the bare model (plan Section 5.5) — measuring the HARNESS, not the model.

The roster is MODEL-LOCKED: we cannot factor the base model out by crossover. Section 5.5's
answer is to measure each harness against ITS OWN bare model, holding the base model M fixed:

    lift(H, M) = theta(M WITH harness H) - theta(M BARE)

Both terms share the SAME base model M, so writing the launch-model skill of a player as
``theta_H + phi_M`` (Section 5's decomposition), the phi_M term CANCELS in the subtraction:

    lift(H, M) = (theta_H + phi_M) - (theta_bare + phi_M) = theta_H - theta_bare.

That cancellation is the whole point: lift is identifiable WITHOUT crossover because M is its
own control. A bare model shows ~0 lift; a real harness shows LARGE positive lift — the exact
quantity the product sells. These tests PIN a seed, a TRUE lift, and a THEORY-DERIVED
tolerance; the null / positive controls are the thesis stated as falsifiable assertions.

THE BARE MECHANISM (what makes a run "bare"): Section 2's ``isolated_home`` seeds the per-run
HOME from the harness's config root; ``isolated_home(None)`` seeds NOTHING, so the probe of
that empty root reports empty skills/tools/mcps/plugins/nested_skills — the harness scaffolding
is physically absent, not relabelled. Same model CLI, stripped harness.

RED before ``src/atv_bench/lift.py`` exists.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from atv_bench.rating import RatingMatch

# The API under test (RED until src/atv_bench/lift.py lands).
from atv_bench.lift import (  # noqa: E402
    LiftError,
    LiftResult,
    compute_lift,
    fit_lift,
    fit_player_ratings,
    manifest_is_bare,
)


# ---------------------------------------------------------------------------
# Synthetic generator: each PLAYER is (harness, model); its launch skill is
# theta_H + phi_M. Matches are Bernoulli under the BT logit gap. The generator
# puts NO harness skill into a bare/null player (theta_bare = 0), so any lift the
# estimator reports for the null control is attributable bias — the falsification core.
# ---------------------------------------------------------------------------


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _gen_lift_matches(
    *,
    skill: dict[tuple[str, str], float],
    pairs: list[tuple[tuple[str, str], tuple[str, str]]],
    n_per_pair: int,
    seed: int,
) -> list[RatingMatch]:
    """Sample Bernoulli matches for each (player_a, player_b) pair.

    ``skill[(harness, model)] = theta_H + phi_M`` is the player's total launch skill. Each
    row's P(a beats b) = sigmoid(skill_a - skill_b). Every match is an INDEPENDENT run
    (its own draw) — the run-to-run nondeterminism the bootstrap must reflect lives at the
    row level here.
    """
    rng = np.random.default_rng(seed)
    out: list[RatingMatch] = []
    for pa, pb in pairs:
        sa, sb = skill[pa], skill[pb]
        p_a = _sigmoid(sa - sb)
        for _ in range(n_per_pair):
            score_a = 1.0 if rng.random() < p_a else 0.0
            out.append(RatingMatch(
                harness_a=pa[0], harness_b=pb[0],
                model_a=pa[1], model_b=pb[1], score_a=score_a,
            ))
    return out


# ---------------------------------------------------------------------------
# 1. The bare run strips the harness: its fingerprint is empty scaffolding.
# ---------------------------------------------------------------------------


def test_bare_run_has_empty_harness_fingerprint(tmp_path):
    """A BARE run's manifest has empty skills/tools/mcps/plugins/nested_skills.

    Deterministic, no live CLI: a bare run's HOME is an isolated dir seeded with NOTHING
    (``isolated_home(None)``), so probing that empty root is exactly probing ``tmp_path``.
    With no settings.json / skills/ / .claude.json, every scaffolding field is empty — the
    harness was actually stripped, not relabelled. (cli_version is a RUNTIME surface, not
    harness scaffolding, so it is intentionally NOT asserted here.)
    """
    from atv_bench.fingerprint.probe import probe_claude_code

    manifest = probe_claude_code(tmp_path).manifest

    for scaffolding_field in ("skills", "nested_skills", "tools", "mcps", "plugins"):
        assert manifest[scaffolding_field] == [], (
            f"bare fingerprint leaked {scaffolding_field}={manifest[scaffolding_field]!r} "
            f"— the harness was not actually stripped")
    assert manifest["gstack"] is False
    assert manifest["custom_agents_count"] == 0
    # The published bare predicate must agree.
    assert manifest_is_bare(manifest) is True


def test_bare_predicate_rejects_a_harnessed_manifest():
    """manifest_is_bare is FALSE when any scaffolding field is populated (so 'bare' can't be
    faked by a manifest that merely relabels a real harness as bare)."""
    harnessed = {
        "skills": ["gstack"], "nested_skills": [], "tools": [], "mcps": [],
        "plugins": [], "gstack": True, "custom_agents_count": 0,
    }
    assert manifest_is_bare(harnessed) is False


# ---------------------------------------------------------------------------
# 2. Lift = theta(M+H) - theta(M bare), with a bootstrap CI.
# ---------------------------------------------------------------------------


def test_lift_is_harnessed_minus_bare():
    """compute_lift recovers the injected lift theta_H - theta_bare for a harness on model M.

    Design: one base model ``m``. Harness ``H`` has theta_H = 0.8; its bare baseline ``bare``
    has theta_bare = 0. Both run model ``m`` (phi_m cancels), so the true lift is exactly 0.8.

    Tolerance (theory-derived, NOT tuned to a passing run): the H-vs-bare contrast is a
    2-player Bradley-Terry logit gap, whose MLE has asymptotic variance 1/(N p(1-p)) with
    p=sigmoid(0.8)=0.690, p(1-p)=0.214. At N=4000, SE = sqrt(1/(4000*0.214)) = 0.034. We
    require |point - 0.8| < 0.15 ≈ 4.4 SE (a bound any consistent estimator clears at a pinned
    seed) and that the 95% bootstrap CI COVERS 0.8 (nominal coverage 0.95).
    """
    skill = {("H", "m"): 0.8, ("bare", "m"): 0.0}
    pairs = [(("H", "m"), ("bare", "m"))]
    matches = _gen_lift_matches(skill=skill, pairs=pairs, n_per_pair=4000, seed=5)

    out = compute_lift(matches, {"H": "bare"}, seed=0, n_boot=400)
    assert set(out) == {"H"}
    res = out["H"]
    assert isinstance(res, LiftResult)
    assert res.harness == "H" and res.bare_harness == "bare" and res.base_model == "m"

    assert abs(res.lift - 0.8) < 0.15, f"lift point {res.lift:.3f} off true 0.8 by > 0.15"
    assert res.lo <= 0.8 <= res.hi, f"95% CI ({res.lo:.3f},{res.hi:.3f}) missed true 0.8"
    # fit_lift is the documented alias.
    assert fit_lift(matches, {"H": "bare"}, seed=0, n_boot=400)["H"].lift == pytest.approx(
        res.lift)


# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROL / thesis falsification: a NULL harness shows zero lift.
# ---------------------------------------------------------------------------


def test_null_harness_zero_lift():
    """A NULL harness (empty config — identical to bare) must show a lift CI that INCLUDES 0.

    The generator gives the null harness EXACTLY the bare skill (theta_null = theta_bare = 0),
    so the true lift is 0 and matches are fair coin flips (p=0.5). If the lift math secretly
    rewarded mere participation (e.g. counted 'ran a match' as skill), the CI would be pushed
    off 0 and this test would FAIL — that is the falsification this control provides.

    Tolerance (theory-derived): at N=4000, p=0.5, SE = sqrt(1/(4000*0.25)) = 0.032; the 95%
    bootstrap half-width ≈ 0.062. A correctly-centred null lift lands within ~1 SE of 0 with
    overwhelming probability, so the CI covers 0.
    """
    skill = {("null", "m"): 0.0, ("bare", "m"): 0.0}
    pairs = [(("null", "m"), ("bare", "m"))]
    matches = _gen_lift_matches(skill=skill, pairs=pairs, n_per_pair=4000, seed=13)

    res = compute_lift(matches, {"null": "bare"}, seed=0, n_boot=400)["null"]
    assert res.lo <= 0.0 <= res.hi, (
        f"NULL harness showed non-zero lift: CI ({res.lo:.3f},{res.hi:.3f}) excludes 0 — "
        f"the lift math is rewarding participation, not harness contribution")


# ---------------------------------------------------------------------------
# 4. POSITIVE CONTROL / the differentiator: a REAL harness shows large + lift.
# ---------------------------------------------------------------------------


def test_real_harness_positive_lift():
    """A REAL harness (genuinely higher win-rate than bare) must show a lift CI that EXCLUDES
    0 and is POSITIVE. This is the product thesis as a test: real harness >> bare.

    Design: theta_real = 0.9 over the bare baseline (both on model ``m``). At N=4000,
    p=sigmoid(0.9)=0.711, p(1-p)=0.205, SE=0.035, 95% half-width ≈ 0.069 — the true 0.9 sits
    ~26 SE above 0, so a correct estimator's CI excludes 0 with power ≈ 1. Guards against an
    over-shrinking estimator that refuses to credit a real harness.
    """
    skill = {("real", "m"): 0.9, ("bare", "m"): 0.0}
    pairs = [(("real", "m"), ("bare", "m"))]
    matches = _gen_lift_matches(skill=skill, pairs=pairs, n_per_pair=4000, seed=21)

    res = compute_lift(matches, {"real": "bare"}, seed=0, n_boot=400)["real"]
    assert res.lo > 0.0, (
        f"REAL harness lift CI ({res.lo:.3f},{res.hi:.3f}) does not exclude 0 — the estimator "
        f"failed to credit a genuine harness advantage")
    assert res.lift > 0.0


# ---------------------------------------------------------------------------
# 5. THE KEY PROPERTY: lifts are comparable across DIFFERENT base models, even
#    though raw thetas are not. This is what solves the model-locked problem.
# ---------------------------------------------------------------------------


def test_lift_identifiable_across_different_base_models():
    """Two harnesses on DIFFERENT base models, each vs its OWN bare baseline, have COMPARABLE
    lifts even though their RAW thetas are miles apart.

    Design: harness A runs a STRONG model (phi_fast = 2.5), harness B runs a WEAK model
    (phi_slow = 0.0). BOTH have the same true harness skill theta = 0.7. A bridge game
    (bareA-fast vs bareB-slow) connects the graph so a single global BT is well-defined.

      * Raw player thetas: theta(A,fast) - theta(B,slow) ≈ (0.7+2.5) - (0.7+0) = 2.5 — the
        base model dominates, so raw skill is NOT comparable across the roster.
      * Lifts: lift_A = theta(A,fast) - theta(bareA,fast) = 0.7; lift_B = 0.7. The phi term
        cancels WITHIN each lift, so the two lifts coincide.

    Tolerances (theory-derived): each lift is a 2-player logit gap (SE ≈ 0.04 at N=3000), so
    Var(lift_A - lift_B) ≈ 2*0.04^2 and SD ≈ 0.057; |lift_A - lift_B| < 0.20 ≈ 3.5 SD. The raw
    gap's truth is 2.5, so asserting raw gap > 1.5 is a wide, safe floor that a phi-cancelling
    lift would NEVER satisfy — proving the two quantities behave oppositely.
    """
    theta = 0.7
    phi_fast, phi_slow = 2.5, 0.0
    skill = {
        ("A", "fast"): theta + phi_fast,
        ("bareA", "fast"): 0.0 + phi_fast,
        ("B", "slow"): theta + phi_slow,
        ("bareB", "slow"): 0.0 + phi_slow,
    }
    pairs = [
        (("A", "fast"), ("bareA", "fast")),     # fast-model cluster
        (("B", "slow"), ("bareB", "slow")),     # slow-model cluster
        (("bareA", "fast"), ("bareB", "slow")),  # bridge: connects the graph
    ]
    matches = _gen_lift_matches(skill=skill, pairs=pairs, n_per_pair=3000, seed=29)

    lifts = compute_lift(matches, {"A": "bareA", "B": "bareB"}, seed=0, n_boot=400)
    lift_a, lift_b = lifts["A"].lift, lifts["B"].lift

    assert abs(lift_a - lift_b) < 0.20, (
        f"lifts not comparable across base models: lift_A={lift_a:.3f}, lift_B={lift_b:.3f} — "
        f"the model term did not cancel within each lift")
    # Each lift recovers the true 0.7.
    assert abs(lift_a - 0.7) < 0.20 and abs(lift_b - 0.7) < 0.20

    # Raw player thetas are NOT comparable: the base-model term dominates the cross-model gap.
    raw = fit_player_ratings(matches)
    raw_gap = raw[("A", "fast")] - raw[("B", "slow")]
    assert raw_gap > 1.5, (
        f"raw theta gap {raw_gap:.3f} unexpectedly small — the design does not actually "
        f"confound base-model skill into raw theta, so the lift claim is not being tested")


# ---------------------------------------------------------------------------
# 6. Can't claim lift without a bare baseline.
# ---------------------------------------------------------------------------


def test_lift_requires_bare_baseline():
    """Requesting lift for a harness whose bare-model control was never run must RAISE.

    ``H`` played matches, but its declared baseline ``bare`` has NO control run on the same
    base model, so the theta(bare) term is undefined — there is no baseline to subtract, hence
    no defensible lift number. The engine must refuse (LiftError), never fabricate a lift from
    a missing baseline.
    """
    skill = {("H", "m"): 0.8, ("other", "m"): 0.0}
    pairs = [(("H", "m"), ("other", "m"))]  # 'bare' never appears
    matches = _gen_lift_matches(skill=skill, pairs=pairs, n_per_pair=500, seed=1)

    with pytest.raises(LiftError):
        compute_lift(matches, {"H": "bare"}, seed=0, n_boot=50)
