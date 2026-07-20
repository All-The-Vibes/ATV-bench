"""Statistical machinery for the rating engine (plan Section 5): clustered bootstrap,
multiple-comparison correction, match-noise floor, power-derived N_MIN, and an
intransitivity detector.

RED before src/atv_bench/stats.py exists. Each test pins a seed and a theory-derived
acceptance criterion rather than a number reverse-engineered from a passing run.
"""
from __future__ import annotations

import numpy as np
import pytest

from atv_bench.stats import (
    bh_fdr,
    bootstrap_ci,
    intransitivity_statistic,
    n_min_for_power,
    noise_floor_variance,
)


# ---------------------------------------------------------------------------
# 1. Clustered bootstrap achieves nominal coverage where i.i.d. under-covers.
# ---------------------------------------------------------------------------


def test_bootstrap_clusters_by_pair():
    """On CORRELATED data (many matches share a pair/cluster, so observations are not
    independent) an i.i.d. row bootstrap under-estimates variance and its 95% CI covers the
    truth < 95% of the time. Resampling whole CLUSTERS (by pair) restores nominal coverage.

    Theory (why this is the right control, not a tuned threshold): with intra-cluster
    correlation rho and cluster size m, the true variance of the mean carries a design effect
    1+(m-1)*rho > 1. The i.i.d. bootstrap ignores it and produces intervals too narrow by
    ~sqrt(design effect), so its coverage falls materially below 0.95; the clustered bootstrap
    resamples at the level the correlation lives at and recovers ~0.95. We require the
    clustered coverage to beat the i.i.d. coverage AND to reach at least 0.90 (nominal 0.95
    minus a Monte-Carlo margin over the replicate count), while i.i.d. sits clearly under.
    """
    rng = np.random.default_rng(101)
    true_mean = 0.0
    n_clusters = 60
    cluster_size = 20
    rho_shift_sd = 1.0  # shared per-cluster shift => strong intra-cluster correlation
    n_experiments = 200

    def one_experiment(exp_seed: int):
        r = np.random.default_rng(exp_seed)
        clusters = []
        values = []
        cluster_ids = []
        for c in range(n_clusters):
            shift = r.normal(0.0, rho_shift_sd)
            for _ in range(cluster_size):
                values.append(true_mean + shift + r.normal(0.0, 0.3))
                cluster_ids.append(c)
        values = np.asarray(values)
        cluster_ids = np.asarray(cluster_ids)
        iid = bootstrap_ci(values, cluster_ids=None, seed=exp_seed, n_boot=300)
        clustered = bootstrap_ci(values, cluster_ids=cluster_ids, seed=exp_seed, n_boot=300)
        return (iid["lo"] <= true_mean <= iid["hi"],
                clustered["lo"] <= true_mean <= clustered["hi"])

    iid_cov = 0
    clus_cov = 0
    for e in range(n_experiments):
        a, b = one_experiment(1000 + e)
        iid_cov += int(a)
        clus_cov += int(b)
    iid_frac = iid_cov / n_experiments
    clus_frac = clus_cov / n_experiments
    assert clus_frac > iid_frac, (
        f"clustered coverage {clus_frac:.3f} did not beat i.i.d. {iid_frac:.3f}")
    assert clus_frac >= 0.90, f"clustered coverage {clus_frac:.3f} below nominal floor 0.90"
    assert iid_frac < 0.90, (
        f"i.i.d. coverage {iid_frac:.3f} should UNDER-cover on correlated data")


# ---------------------------------------------------------------------------
# 2. BH/FDR correction is strictly stricter than the naive family-wise gate.
# ---------------------------------------------------------------------------


def test_multiple_comparison_correction():
    """Across the K^2/2 pairwise family, BH-adjusted p-values are >= raw p-values, so the
    corrected significance gate is STRICTLY stricter (never passes something the naive
    threshold rejected, and rejects at least one borderline case the naive gate passed).

    Theory: Benjamini-Hochberg adjusted p_(i) = min_{j>=i} ( m/j * p_(j) ), which is >= p_(i)
    for every hypothesis (the m/j factor is >= 1 at the largest rank and the running min only
    raises small p's). So corrected <= naive count of rejections, deterministically.
    """
    raw = [0.001, 0.008, 0.02, 0.03, 0.04, 0.049, 0.2, 0.5, 0.9]
    alpha = 0.05
    adj = bh_fdr(raw)
    assert len(adj) == len(raw)
    # (a) every adjusted p is >= its raw p
    for r, a in zip(raw, adj):
        assert a >= r - 1e-12, f"adjusted {a} < raw {r}"
    naive_rejections = sum(p < alpha for p in raw)
    corrected_rejections = sum(p < alpha for p in adj)
    # (b) strictly stricter on THIS family (several raw p's sit just under 0.05)
    assert corrected_rejections < naive_rejections, (
        f"BH not stricter: naive={naive_rejections}, corrected={corrected_rejections}")


# ---------------------------------------------------------------------------
# 3. Match-noise floor: repeated identical cell => estimated variance feeds power.
# ---------------------------------------------------------------------------


def test_match_noise_floor():
    """Repeat ONE cell (same harness+model on both sides, so true win prob = 0.5) M times and
    estimate the run-to-run variance. For a fair Bernoulli(0.5) the per-match outcome variance
    is p(1-p)=0.25; the estimator must land near it. Theory-derived tolerance: the sample
    variance of M Bernoulli draws has SE ~ sqrt(2)*p(1-p)/sqrt(M) (delta method), so at
    M=4000 a +/-0.03 window around 0.25 is ~5 SE — comfortably covering an unbiased estimate
    while still rejecting a mis-scaled one."""
    rng = np.random.default_rng(55)
    M = 4000
    outcomes = (rng.random(M) < 0.5).astype(float)  # A/A cell: fair coin
    var = noise_floor_variance(outcomes)
    assert abs(var - 0.25) < 0.03, f"noise-floor variance {var:.4f} not near Bernoulli 0.25"


# ---------------------------------------------------------------------------
# 4. N_MIN is DERIVED from measured noise + target effect, not hardcoded.
# ---------------------------------------------------------------------------


def test_n_min_derived_from_power():
    """N_MIN must be a FUNCTION of (measured noise, target effect size), not a constant:
    halving the target effect roughly quadruples the required N (N ~ 1/effect^2), and higher
    per-match noise raises N. We assert the functional relationship, so a hardcoded constant
    (identical N for different inputs) fails.

    Theory: a two-proportion power calculation gives N proportional to
    variance / effect^2 for fixed alpha/power, so N(effect/2) ~= 4 * N(effect), monotone up in
    variance and down in effect. We check the ratio sits in [3.5, 4.5] (the exact 4x, with a
    small allowance for the discrete ceil and the z-score constants)."""
    n_big_effect = n_min_for_power(effect_size=0.4, noise_var=0.25, power=0.8, alpha=0.05)
    n_half_effect = n_min_for_power(effect_size=0.2, noise_var=0.25, power=0.8, alpha=0.05)
    n_more_noise = n_min_for_power(effect_size=0.4, noise_var=0.40, power=0.8, alpha=0.05)
    assert isinstance(n_big_effect, int) and n_big_effect > 0
    # halving effect ~ quadruples N
    ratio = n_half_effect / n_big_effect
    assert 3.5 <= ratio <= 4.5, f"N_MIN(effect/2)/N_MIN(effect)={ratio:.2f}, expected ~4"
    # more noise strictly raises N
    assert n_more_noise > n_big_effect, "higher noise must raise the required N"


# ---------------------------------------------------------------------------
# 5. Intransitivity detector fires on a cyclic tournament.
# ---------------------------------------------------------------------------


def test_intransitivity_detected():
    """A rock-paper-scissors cycle (A beats B, B beats C, C beats A, each ~70/30) must fire an
    intransitivity statistic ABOVE the transitive-null band; a genuinely transitive tournament
    must sit BELOW it. Theory: under a Bradley-Terry (transitive) model the fitted probs
    reproduce the observed pairwise rates up to sampling noise, so the cyclic residual
    concentrates near 0; a true 3-cycle cannot be represented by any single rating vector, so
    its residual is bounded away from 0 as N grows. We assert cyclic_stat > transitive_stat AND
    cyclic_stat exceeds a null threshold the transitive case clears."""
    rng = np.random.default_rng(202)

    def sample(pairs_prob, n_per_pair, seed):
        r = np.random.default_rng(seed)
        results = []
        for (x, y), p in pairs_prob.items():
            for _ in range(n_per_pair):
                results.append((x, y, 1.0 if r.random() < p else 0.0))
        return results

    cyclic = sample({("A", "B"): 0.7, ("B", "C"): 0.7, ("C", "A"): 0.7}, 400, 1)
    transitive = sample({("A", "B"): 0.7, ("B", "C"): 0.7, ("A", "C"): 0.85}, 400, 2)
    stat_cyclic = intransitivity_statistic(cyclic)  # {"statistic": float, "flagged": bool}
    stat_trans = intransitivity_statistic(transitive)
    assert stat_cyclic["statistic"] > stat_trans["statistic"], (
        f"cyclic {stat_cyclic['statistic']:.3f} not above transitive "
        f"{stat_trans['statistic']:.3f}")
    assert stat_cyclic["flagged"] is True, "cyclic tournament must be flagged"
    assert stat_trans["flagged"] is False, "transitive tournament must not be flagged"
