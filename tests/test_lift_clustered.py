"""G2 — cluster-correct lift bootstrap (plan gap-fill, RED-first).

PR17's ``compute_lift`` resamples whole MATCHES i.i.d. ("each match is an independent
run"). But N games produced by ONE harness build-artifact are NOT N independent
observations: they share the artifact, so their outcomes are intra-cluster correlated.
Resampling the nested unit (the row) understates the sampling variance — the CI is
anticonservative, and a "CI excludes 0 -> real harness" verdict becomes phantom precision.

The fix (mirroring ``stats.bootstrap_ci(cluster_ids=...)``): when a cluster key is supplied,
the bootstrap draws whole CLUSTERS with replacement and refits theta on the pooled rows, so
the design effect ``1 + (m-1)rho`` is reflected and coverage returns to nominal.

These tests PIN the direction of the effect:
  * on correlated synthetic data the CLUSTERED CI is strictly WIDER than the naive per-match CI;
  * on i.i.d. data (one row per cluster) the two coincide;
  * the ``cluster_ids=None`` path is byte-identical to today's output (regression pin).
"""
from __future__ import annotations

import math

import numpy as np

from atv_bench.rating import RatingMatch
from atv_bench.lift import compute_lift


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------- #
# Synthetic generator with CLUSTERS. A cluster = one build-artifact of a harness
# playing a block of games. Within a cluster the artifact draws a shared latent
# shock (its "form on the day"), so the block's outcomes are correlated; across
# clusters the shock is independent. This is exactly the nested structure the
# clustered bootstrap must price in.
# --------------------------------------------------------------------------- #
def _gen_clustered(
    *,
    skill: dict[tuple[str, str], float],
    pairs,
    n_clusters: int,
    games_per_cluster: int,
    rho_shock: float,
    seed: int,
):
    rng = np.random.default_rng(seed)
    matches: list[RatingMatch] = []
    cluster_ids: list[str] = []
    cid = 0
    for pa, pb in pairs:
        sa, sb = skill[pa], skill[pb]
        for _ in range(n_clusters):
            cid += 1
            key = f"{pa[0]}~{pb[0]}~c{cid}"
            # shared per-cluster shock -> intra-cluster correlation
            shock = rng.normal(0.0, rho_shock)
            p_a = _sigmoid((sa + shock) - sb)
            for _ in range(games_per_cluster):
                score_a = 1.0 if rng.random() < p_a else 0.0
                matches.append(RatingMatch(
                    harness_a=pa[0], harness_b=pb[0],
                    model_a=pa[1], model_b=pb[1], score_a=score_a,
                ))
                cluster_ids.append(key)
    return matches, cluster_ids


def _pairs_for(bare, harness, model):
    b = (bare, model)
    h = (harness, model)
    # both players must appear against a common opponent; play them against each other
    return [(h, b)]


def test_cluster_ids_none_is_backcompat_identical():
    """Passing cluster_ids=None reproduces the legacy per-match bootstrap byte-for-byte."""
    skill = {("gstack", "m"): 0.8, ("bare", "m"): 0.0}
    pairs = _pairs_for("bare", "gstack", "m")
    matches, _ = _gen_clustered(
        skill=skill, pairs=pairs, n_clusters=10, games_per_cluster=1,
        rho_shock=0.0, seed=1,
    )
    baselines = {"gstack": "bare"}
    legacy = compute_lift(matches, baselines, seed=7, n_boot=300)
    withnone = compute_lift(matches, baselines, seed=7, n_boot=300, cluster_ids=None)
    assert legacy["gstack"].lo == withnone["gstack"].lo
    assert legacy["gstack"].hi == withnone["gstack"].hi
    assert legacy["gstack"].lift == withnone["gstack"].lift


def test_clustered_ci_is_wider_on_correlated_data():
    """On intra-cluster-correlated data the clustered CI must be strictly WIDER.

    Same rows, same seed, same n_boot — the ONLY difference is the resampling unit. If the
    clustered CI were not wider, the per-match bootstrap would be understating variance
    exactly as the gap describes.
    """
    skill = {("gstack", "m"): 0.8, ("bare", "m"): 0.0}
    pairs = _pairs_for("bare", "gstack", "m")
    matches, cluster_ids = _gen_clustered(
        skill=skill, pairs=pairs, n_clusters=12, games_per_cluster=6,
        rho_shock=1.5, seed=3,
    )
    baselines = {"gstack": "bare"}
    naive = compute_lift(matches, baselines, seed=11, n_boot=400)
    clustered = compute_lift(matches, baselines, seed=11, n_boot=400, cluster_ids=cluster_ids)

    naive_w = naive["gstack"].hi - naive["gstack"].lo
    clustered_w = clustered["gstack"].hi - clustered["gstack"].lo
    assert clustered_w > naive_w * 1.10, (
        f"clustered CI ({clustered_w:.4f}) should be materially wider than "
        f"naive ({naive_w:.4f}) on correlated data"
    )
    # point estimate is unchanged by the resampling scheme
    assert abs(clustered["gstack"].lift - naive["gstack"].lift) < 1e-9


def test_clustered_matches_naive_when_one_row_per_cluster():
    """With exactly one row per cluster there is no nesting, so the two CIs coincide."""
    skill = {("gstack", "m"): 0.6, ("bare", "m"): 0.0}
    pairs = _pairs_for("bare", "gstack", "m")
    matches, _ = _gen_clustered(
        skill=skill, pairs=pairs, n_clusters=40, games_per_cluster=1,
        rho_shock=0.0, seed=5,
    )
    cluster_ids = [f"c{i}" for i in range(len(matches))]  # unique -> singleton clusters
    baselines = {"gstack": "bare"}
    naive = compute_lift(matches, baselines, seed=9, n_boot=400)
    clustered = compute_lift(matches, baselines, seed=9, n_boot=400, cluster_ids=cluster_ids)
    naive_w = naive["gstack"].hi - naive["gstack"].lo
    clustered_w = clustered["gstack"].hi - clustered["gstack"].lo
    # Singleton clusters == i.i.d. rows in DISTRIBUTION, but the two bootstraps consume the
    # RNG stream differently (n cluster-index draws vs n row-index draws), so widths agree
    # only up to Monte-Carlo noise — not byte-identically. The point: no systematic
    # widening like the correlated case (which was >10% wider).
    assert abs(clustered_w - naive_w) < 0.15 * naive_w + 1e-6


def test_clustered_bootstrap_is_seed_deterministic():
    skill = {("gstack", "m"): 0.7, ("bare", "m"): 0.0}
    pairs = _pairs_for("bare", "gstack", "m")
    matches, cluster_ids = _gen_clustered(
        skill=skill, pairs=pairs, n_clusters=8, games_per_cluster=4,
        rho_shock=1.0, seed=2,
    )
    baselines = {"gstack": "bare"}
    a = compute_lift(matches, baselines, seed=42, n_boot=200, cluster_ids=cluster_ids)
    b = compute_lift(matches, baselines, seed=42, n_boot=200, cluster_ids=cluster_ids)
    assert a["gstack"].lo == b["gstack"].lo
    assert a["gstack"].hi == b["gstack"].hi
