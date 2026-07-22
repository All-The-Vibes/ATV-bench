"""Launch rating model (Tier-1) — the statistical core (plan Section 5).

Model under test (per player p=(harness Hp, base-model Mp) in a match vs q):

    logit P(p beats q) = (theta_Hp - theta_Hq) + (phi_Mp - phi_Mq) + eps_cell

theta_H is the PUBLISHED harness rating (marginalized over the base model). phi_M is a
base-model nuisance that is estimated then removed. eps_cell is a per-(H,M,G) overdispersion
term for LLM run-to-run nondeterminism. Estimation is penalized MLE (ridge on theta, phi)
with partial pooling via scipy.optimize.

These tests are RED before src/atv_bench/rating.py exists. Every synthetic test PINS a seed,
a true parameter vector (theta*, phi*), a design, N matches, and a PRE-COMMITTED,
theory-derived tolerance. The negative-control tests (identical-harness / near-collinear)
are the falsification core: they assert the estimator REFUSES a separation the generator
never put into harness skill, so a model-skill leak makes them fail.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from atv_bench.rating import RatingMatch, fit_ratings

# ---------------------------------------------------------------------------
# Synthetic generator: emits Bernoulli match outcomes from the EXACT generative
# model above. Because the generator uses ONLY (theta*, phi*), any theta_hat
# separation the estimator reports that is not in theta* is attributable bias.
# ---------------------------------------------------------------------------


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _gen_matches(
    *,
    theta: dict[str, float],
    phi: dict[str, float],
    players: list[tuple[str, str]],  # (harness, model) identities that can play
    n: int,
    seed: int,
    eps_cell_sd: float = 0.0,
) -> list[RatingMatch]:
    """Sample `n` matches between random distinct players under the launch model.

    `eps_cell_sd` > 0 injects a per-cell latent shift (overdispersion): a fixed random
    logit offset per (harness, model) cell, redrawn per generator, modelling LLM
    run-to-run nondeterminism that a plain BT fit would mistake for signal.
    """
    rng = np.random.default_rng(seed)
    cell_eps: dict[tuple[str, str], float] = {}
    if eps_cell_sd > 0:
        for h, m in players:
            cell_eps[(h, m)] = float(rng.normal(0.0, eps_cell_sd))
    idx = np.arange(len(players))
    out: list[RatingMatch] = []
    for _ in range(n):
        i, j = rng.choice(idx, size=2, replace=False)
        (ha, ma), (hb, mb) = players[i], players[j]
        logit = (theta[ha] - theta[hb]) + (phi[ma] - phi[mb])
        logit += cell_eps.get((ha, ma), 0.0) - cell_eps.get((hb, mb), 0.0)
        p_a = _sigmoid(logit)
        score_a = 1.0 if rng.random() < p_a else 0.0
        out.append(RatingMatch(harness_a=ha, harness_b=hb,
                               model_a=ma, model_b=mb, score_a=score_a))
    return out


def _balanced_players(harnesses: list[str], models: list[str]) -> list[tuple[str, str]]:
    """Full cross of harness x model — every harness appears with EVERY model, so the
    harness effect and the model effect are separately identifiable (crossover present)."""
    return [(h, m) for h in harnesses for m in models]


def _gen_confounded_matches(
    *,
    theta: dict[str, float],
    phi: dict[str, float],
    harness_model_bias: dict[str, tuple[str, str, float]],
    n: int,
    seed: int,
    eps_cell_sd: float = 0.0,
) -> list[RatingMatch]:
    """Sample `n` matches where each harness is CONFOUNDED with a base model.

    ``harness_model_bias[h] = (dominant_model, minority_model, p_dominant)``: harness ``h`` is
    observed running ``dominant_model`` a fraction ``p_dominant`` of the time and
    ``minority_model`` otherwise. With different dominant models per harness this yields a
    BIASED (not balanced) design: the base-model effect does NOT cancel on average, so a
    model-blind estimator mis-attributes the phi gap to theta. A minority of crossover games
    keeps phi estimable for a model-aware fit.
    """
    rng = np.random.default_rng(seed)
    harness_names = list(harness_model_bias)
    cell_eps: dict[tuple[str, str], float] = {}
    if eps_cell_sd > 0:
        for h, (dm, mm, _) in harness_model_bias.items():
            for mdl in {dm, mm}:
                cell_eps[(h, mdl)] = float(rng.normal(0.0, eps_cell_sd))

    def draw_player() -> tuple[str, str]:
        h = harness_names[int(rng.integers(len(harness_names)))]
        dm, mm, p_dom = harness_model_bias[h]
        m = dm if rng.random() < p_dom else mm
        return h, m

    out: list[RatingMatch] = []
    while len(out) < n:
        ha, ma = draw_player()
        hb, mb = draw_player()
        if ha == hb:
            continue  # need a cross-harness contrast to carry theta signal
        logit = (theta[ha] - theta[hb]) + (phi[ma] - phi[mb])
        logit += cell_eps.get((ha, ma), 0.0) - cell_eps.get((hb, mb), 0.0)
        p_a = _sigmoid(logit)
        score_a = 1.0 if rng.random() < p_a else 0.0
        out.append(RatingMatch(harness_a=ha, harness_b=hb,
                               model_a=ma, model_b=mb, score_a=score_a))
    return out


# ---------------------------------------------------------------------------
# 1. Recover the harness rating with the base model factored out.
# ---------------------------------------------------------------------------


def test_model_factored_out():
    """Confounded crossover design: theta_hat must recover theta* with model removed.

    Each harness is biased toward a DIFFERENT base model (confounded), so the base-model
    effect does NOT cancel on average — a phi-blind fit is miscentred by O(phi) and cannot
    hit nominal Wald coverage. A crossover minority keeps phi identifiable for the model-aware
    fit.

    Tolerance (theory-derived, NOT tuned to a passing run): theta is identified only up to
    an additive constant, so we test the reference-contrast (theta[h]-theta[ref]) whose Wald
    statistic z=(est-truth)/SE is asymptotically N(0,1). A |z|<2 interval has nominal
    coverage Phi(2)-Phi(-2)=0.9545. Over 200 pinned seeds x (K-1) contrasts, the coverage
    fraction has small binomial SD. We require >= 0.94, i.e. nominal minus ~1 binomial SD — a
    floor any correctly-centred Wald estimator clears, while a phi-contaminated estimator is
    miscentred by O(phi) >> SE and collapses far below.
    """
    harnesses = ["H0", "H1", "H2", "H3"]
    theta_star = {"H0": 0.0, "H1": 0.6, "H2": -0.4, "H3": 0.9}
    phi_star = {"M0": 0.0, "M1": 1.2, "M2": -0.8}  # non-trivial base-model skill
    # Confound each harness with a distinct dominant base model (biased, not balanced).
    bias = {
        "H0": ("M0", "M1", 0.8),
        "H1": ("M1", "M2", 0.8),
        "H2": ("M2", "M0", 0.8),
        "H3": ("M0", "M2", 0.8),
    }
    ref = "H0"
    covered = 0
    total = 0
    blind_covered = 0
    blind_total = 0
    for seed in range(200):
        matches = _gen_confounded_matches(theta=theta_star, phi=phi_star,
                                          harness_model_bias=bias, n=1200, seed=seed)
        res = fit_ratings(matches)
        assert res.attributed is True  # crossover present => identifiable
        blind = fit_ratings(matches, factor_out_model=False)
        for h in harnesses:
            if h == ref:
                continue
            truth = theta_star[h] - theta_star[ref]
            pw = res.pairwise(h, ref)  # {"diff","se","lo","hi"}
            z = (pw["diff"] - truth) / pw["se"]
            covered += int(abs(z) < 2.0)
            total += 1
            bpw = blind.pairwise(h, ref)
            bz = (bpw["diff"] - truth) / bpw["se"]
            blind_covered += int(abs(bz) < 2.0)
            blind_total += 1
    frac = covered / total
    assert frac >= 0.94, f"Wald coverage {frac:.3f} below theory floor 0.94 (nominal 0.9545)"
    # The phi-blind fit is miscentred by O(phi) on this confounded design and MUST miss the
    # coverage floor — proving the model-factoring is what earns nominal coverage.
    blind_frac = blind_covered / blind_total
    assert blind_frac < 0.94, (
        f"model-blind coverage {blind_frac:.3f} unexpectedly met the floor — the design is "
        f"not actually stressing model-factoring")


# ---------------------------------------------------------------------------
# 2. THE HEADLINE FALSIFICATION TEST: identical harness skill, wildly different
#    base models, must NOT separate. If model skill leaks into theta, this fails.
# ---------------------------------------------------------------------------


def test_identical_harness_different_model_no_separation():
    """Two harnesses with IDENTICAL theta, each CONFOUNDED with a very different base model
    (A runs "fast" ~88% of the time, B runs "slow" ~88%). This is a BIASED design: the wide
    phi gap does NOT cancel on average, so the only reason theta_A - theta_B can come back at
    ~0 is that the estimator correctly MODELS and removes phi. A model-blind estimator instead
    absorbs the O(phi_fast - phi_slow) gap into theta and separates the harnesses.

    Non-circular by construction: the generator put NO harness-skill difference in, yet the
    design is deliberately confounded so that only a phi-aware fit can return the null. We
    assert BOTH directions here:
      * the real (factor_out_model=True) fit's A-B CI INCLUDES 0, and
      * a model-blind (factor_out_model=False) fit on the SAME data EXCLUDES 0 — i.e. the
        control has teeth: strip phi and it fails.
    """
    theta_star = {"A": 0.3, "B": 0.3}          # IDENTICAL harness skill
    phi_star = {"fast": 1.5, "slow": -1.5}     # WIDE base-model gap (3.0 logits)
    # Confounded: A mostly "fast", B mostly "slow"; a minority of crossover games keeps phi
    # estimable for the model-aware fit.
    bias = {
        "A": ("fast", "slow", 0.88),
        "B": ("slow", "fast", 0.88),
    }
    matches = _gen_confounded_matches(theta=theta_star, phi=phi_star,
                                      harness_model_bias=bias, n=8000, seed=7)

    # Sanity: the observed design really is biased/confounded (not balanced).
    from collections import Counter
    cells = Counter()
    for m in matches:
        cells[(m.harness_a, m.model_a)] += 1
        cells[(m.harness_b, m.model_b)] += 1
    a_fast = cells[("A", "fast")]
    a_slow = cells[("A", "slow")]
    b_slow = cells[("B", "slow")]
    b_fast = cells[("B", "fast")]
    assert a_fast > 5 * a_slow, f"A not confounded with fast: {a_fast} vs {a_slow}"
    assert b_slow > 5 * b_fast, f"B not confounded with slow: {b_slow} vs {b_fast}"

    res = fit_ratings(matches)
    pw = res.pairwise("A", "B")
    assert pw["lo"] <= 0.0 <= pw["hi"], (
        f"model-aware estimator SEPARATED identical harnesses: CI ({pw['lo']:.3f}, "
        f"{pw['hi']:.3f}) excludes 0 — base-model skill leaked into theta")

    # Teeth check: a model-blind fit on the SAME confounded data must WRONGLY separate.
    blind = fit_ratings(matches, factor_out_model=False)
    bpw = blind.pairwise("A", "B")
    assert not (bpw["lo"] <= 0.0 <= bpw["hi"]), (
        f"model-blind fit FAILED to separate — negative control is toothless: "
        f"CI ({bpw['lo']:.3f}, {bpw['hi']:.3f}) includes 0")


def test_negative_control_catches_model_leak():
    """Mutation guard: PROVE the negative control has teeth.

    Same confounded design as the headline test (identical harness skill, each harness locked
    mostly to a different base model). We fit BOTH ways:
      * factor_out_model=True  (real estimator): A-B CI INCLUDES 0 — no leak.
      * factor_out_model=False (model-blind mutation): A-B CI EXCLUDES 0 and, because A runs
        the STRONGER base model ("fast"), the model-blind fit reports A > B — it has WRONGLY
        attributed the base-model gap to the harness.

    If someone deletes phi modeling from the estimator, the real fit collapses onto the
    model-blind behaviour and the headline control fails. This test pins that failure mode
    directly, so it cannot silently regress.
    """
    theta_star = {"A": 0.3, "B": 0.3}
    phi_star = {"fast": 1.5, "slow": -1.5}
    bias = {
        "A": ("fast", "slow", 0.88),
        "B": ("slow", "fast", 0.88),
    }
    matches = _gen_confounded_matches(theta=theta_star, phi=phi_star,
                                      harness_model_bias=bias, n=8000, seed=7)

    aware = fit_ratings(matches, factor_out_model=True).pairwise("A", "B")
    assert aware["lo"] <= 0.0 <= aware["hi"], (
        f"model-aware fit unexpectedly separated: CI "
        f"({aware['lo']:.3f}, {aware['hi']:.3f})")

    blind = fit_ratings(matches, factor_out_model=False).pairwise("A", "B")
    assert not (blind["lo"] <= 0.0 <= blind["hi"]), (
        f"model-blind fit did NOT separate — control has no teeth: CI "
        f"({blind['lo']:.3f}, {blind['hi']:.3f})")
    # A carries the stronger base model, so the leak inflates A's harness rating.
    assert blind["diff"] > 0.0, (
        f"model-blind leak expected A > B, got diff {blind['diff']:.3f}")


# ---------------------------------------------------------------------------
# 3. Mirror: identical base model, genuinely different harness skill => separates.
# ---------------------------------------------------------------------------


def test_identical_model_different_harness_separates():
    """Same base model on both sides, real harness-skill gap => theta MUST separate.
    Effect size 1.2 logits >> SE at N=4000, so a correct estimator's CI excludes 0 with
    power ~1 (theory: |true diff|/SE is many multiples of 1.96). Guards against an
    over-shrinking estimator that refuses ALL separation."""
    harnesses = ["strong", "weak"]
    theta_star = {"strong": 0.6, "weak": -0.6}   # 1.2-logit true gap
    phi_star = {"only": 0.0}
    players = [("strong", "only"), ("weak", "only")]
    matches = _gen_matches(theta=theta_star, phi=phi_star,
                           players=players, n=4000, seed=11)
    res = fit_ratings(matches)
    pw = res.pairwise("strong", "weak")
    assert not (pw["lo"] <= 0.0 <= pw["hi"]), (
        f"estimator FAILED to separate a real 1.2-logit harness gap: "
        f"CI ({pw['lo']:.3f}, {pw['hi']:.3f}) includes 0")
    assert pw["diff"] > 0.0  # sign recovered: strong > weak


# ---------------------------------------------------------------------------
# 4. Near-collinear design (harness perfectly correlated with model) => refuse.
# ---------------------------------------------------------------------------


def test_near_collinear_design_refused():
    """Each harness ALWAYS runs a unique base model — harness and model are perfectly
    confounded, so theta and phi are NOT separately identifiable. The estimator must return
    attributed=False and NOT let ridge silently fill theta with the confounded effect.

    Theory: with no crossover the design matrix column for harness H equals the column for
    model M(H); the Fisher information for the theta/phi contrast is singular. Ridge makes the
    penalized objective strictly convex (a unique minimiser exists) but that minimiser is a
    regularisation artefact, not an identified estimate — the engine must detect the rank
    deficiency and refuse attribution rather than publish the artefact."""
    theta_star = {"H0": 0.0, "H1": 0.8, "H2": -0.5}
    phi_star = {"m0": 0.4, "m1": -0.3, "m2": 1.1}
    players = [("H0", "m0"), ("H1", "m1"), ("H2", "m2")]  # locked 1:1, no crossover
    matches = _gen_matches(theta=theta_star, phi=phi_star,
                           players=players, n=1500, seed=3)
    res = fit_ratings(matches)
    assert res.attributed is False, "confounded design must not report a factored-out theta"


# ---------------------------------------------------------------------------
# 5. Overdispersion (eps_cell) inflates the CIs vs a no-eps fit.
# ---------------------------------------------------------------------------


def test_overdispersion_inflates_ci():
    """Injected intra-cell variance must WIDEN the reported CIs relative to the same design
    with no eps_cell. Theory: unmodelled per-cell latent shifts add between-cell variance on
    top of Bernoulli sampling variance; an overdispersion-aware fit scales the covariance by a
    dispersion factor phi_hat > 1, so its SEs strictly exceed the clean-fit SEs. Pre-committed
    relation: SE_overdispersed / SE_clean = sqrt(dispersion) > 1."""
    harnesses = ["H0", "H1", "H2", "H3"]
    models = ["M0", "M1", "M2"]
    theta_star = {"H0": 0.0, "H1": 0.5, "H2": -0.3, "H3": 0.7}
    phi_star = {"M0": 0.0, "M1": 0.9, "M2": -0.6}
    players = _balanced_players(harnesses, models)
    clean = _gen_matches(theta=theta_star, phi=phi_star, players=players,
                         n=1200, seed=21, eps_cell_sd=0.0)
    noisy = _gen_matches(theta=theta_star, phi=phi_star, players=players,
                         n=1200, seed=21, eps_cell_sd=1.0)
    res_clean = fit_ratings(clean, model_overdispersion=True)
    res_noisy = fit_ratings(noisy, model_overdispersion=True)
    se_clean = res_clean.pairwise("H1", "H0")["se"]
    se_noisy = res_noisy.pairwise("H1", "H0")["se"]
    assert se_noisy > se_clean, (
        f"overdispersed SE {se_noisy:.4f} did not exceed clean SE {se_clean:.4f}")
