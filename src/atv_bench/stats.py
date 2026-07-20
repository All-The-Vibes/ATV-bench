"""Statistical machinery for the rating engine (plan Section 5).

Real implementations (numpy/scipy) of:
  - ``bootstrap_ci``          : percentile bootstrap with optional CLUSTER resampling.
  - ``bh_fdr``                : Benjamini-Hochberg adjusted p-values.
  - ``noise_floor_variance``  : run-to-run outcome variance of an A/A cell.
  - ``n_min_for_power``       : two-proportion power -> minimum matches, DERIVED not fixed.
  - ``intransitivity_statistic`` : residual of a Bradley-Terry (transitive) fit — fires on
    a genuine cycle it cannot represent.
  - ``reliability_ece``       : reliability / expected calibration error (used by the
    recommender's calibration report).
"""
from __future__ import annotations

from typing import Any, Sequence

import numpy as np
from scipy import optimize, stats


# ---------------------------------------------------------------------------
# Clustered bootstrap.
# ---------------------------------------------------------------------------


def bootstrap_ci(
    values: Sequence[float],
    cluster_ids: Sequence[Any] | None = None,
    *,
    seed: int = 0,
    n_boot: int = 1000,
    ci: float = 0.95,
    statistic=np.mean,
) -> dict[str, float]:
    """Percentile bootstrap CI for a statistic (default: the mean).

    When ``cluster_ids`` is provided the resampling unit is the CLUSTER, not the row: on
    each replicate we draw whole clusters with replacement and pool their rows. This
    respects intra-cluster correlation (design effect 1+(m-1)rho), giving nominal coverage
    where the i.i.d. row bootstrap under-covers.
    """
    values = np.asarray(values, dtype=float)
    rng = np.random.default_rng(seed)
    n = values.shape[0]
    alpha = 1.0 - ci
    lo_q, hi_q = 100 * (alpha / 2), 100 * (1 - alpha / 2)

    if cluster_ids is None:
        boot = np.empty(n_boot)
        for b in range(n_boot):
            idx = rng.integers(0, n, size=n)
            boot[b] = statistic(values[idx])
    else:
        cluster_ids = np.asarray(cluster_ids)
        uniq = np.unique(cluster_ids)
        # Pre-index rows per cluster so each replicate just concatenates.
        members = {c: np.flatnonzero(cluster_ids == c) for c in uniq}
        n_clusters = uniq.shape[0]
        boot = np.empty(n_boot)
        for b in range(n_boot):
            chosen = rng.integers(0, n_clusters, size=n_clusters)
            rows = np.concatenate([members[uniq[c]] for c in chosen])
            boot[b] = statistic(values[rows])

    lo, hi = np.percentile(boot, [lo_q, hi_q])
    return {"lo": float(lo), "hi": float(hi), "point": float(statistic(values))}


# ---------------------------------------------------------------------------
# Benjamini-Hochberg FDR.
# ---------------------------------------------------------------------------


def bh_fdr(pvals: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg adjusted p-values.

    adj p_(i) = min_{j>=i} ( m/j * p_(j) ), enforced monotone non-decreasing in rank and
    clipped to [0,1]. Elementwise >= the raw p, so the corrected gate is never looser.
    """
    p = np.asarray(pvals, dtype=float)
    m = p.shape[0]
    if m == 0:
        return []
    order = np.argsort(p)
    ranked = p[order]
    ranks = np.arange(1, m + 1)
    adj_sorted = ranked * m / ranks
    # running minimum from the largest rank down
    adj_sorted = np.minimum.accumulate(adj_sorted[::-1])[::-1]
    adj_sorted = np.clip(adj_sorted, 0.0, 1.0)
    out = np.empty(m)
    out[order] = adj_sorted
    return out.tolist()


# ---------------------------------------------------------------------------
# Match-noise floor.
# ---------------------------------------------------------------------------


def noise_floor_variance(outcomes: Sequence[float]) -> float:
    """Run-to-run outcome variance of an A/A (self-play) cell.

    The per-match outcome is Bernoulli; for a fair A/A cell p=0.5 and Var=p(1-p)=0.25.
    We return the (population) sample variance of the observed outcomes.
    """
    x = np.asarray(outcomes, dtype=float)
    if x.size == 0:
        return 0.0
    return float(np.var(x))


# ---------------------------------------------------------------------------
# Power -> minimum N.
# ---------------------------------------------------------------------------


def n_min_for_power(
    *,
    effect_size: float,
    noise_var: float,
    power: float = 0.8,
    alpha: float = 0.05,
) -> int:
    """Minimum number of matches to detect ``effect_size`` at the target power.

    Two-sided z-based sample size for a difference of means with per-observation variance
    ``noise_var``:  N = (z_{1-a/2} + z_{power})^2 * (2 * noise_var) / effect^2  (per arm).

    N scales as variance/effect^2, so halving the effect quadruples N and higher noise
    raises N — the value is a FUNCTION of its inputs, never a constant.
    """
    if effect_size <= 0:
        raise ValueError("effect_size must be > 0")
    z_alpha = stats.norm.ppf(1.0 - alpha / 2.0)
    z_power = stats.norm.ppf(power)
    n = (z_alpha + z_power) ** 2 * (2.0 * noise_var) / (effect_size ** 2)
    return int(np.ceil(n))


# ---------------------------------------------------------------------------
# Intransitivity detector.
# ---------------------------------------------------------------------------


def _bradley_terry_fit(items: list[str], wins: dict[tuple[str, str], float],
                       games: dict[tuple[str, str], int]) -> dict[str, float]:
    """Fit Bradley-Terry ratings by penalized MLE. Returns per-item rating (theta)."""
    idx = {it: i for i, it in enumerate(items)}
    k = len(items)

    def negll(theta):
        ll = 0.0
        for (x, y), n in games.items():
            w = wins[(x, y)]
            d = theta[idx[x]] - theta[idx[y]]
            p = 1.0 / (1.0 + np.exp(-d))
            p = min(max(p, 1e-12), 1 - 1e-12)
            ll += w * np.log(p) + (n - w) * np.log(1 - p)
        # tiny ridge for identifiability (anchor scale)
        ll -= 1e-6 * float(theta @ theta)
        return -ll

    res = optimize.minimize(negll, np.zeros(k), method="L-BFGS-B")
    theta = res.x - res.x.mean()
    return {it: float(theta[idx[it]]) for it in items}


def intransitivity_statistic(results: list[tuple[str, str, float]]) -> dict[str, Any]:
    """Residual of a transitive (Bradley-Terry) fit to the observed pairwise win rates.

    Fit a single rating vector; the BT model can only represent a transitive order. The
    statistic is the RMS gap between the observed pairwise win rate and the BT-predicted
    rate. A transitive tournament is reproduced (residual -> 0); a true 3-cycle cannot be
    represented by any rating vector, so its residual stays bounded away from 0.
    """
    # Aggregate directed wins per unordered pair.
    items = sorted({p for r in results for p in (r[0], r[1])})
    wins: dict[tuple[str, str], float] = {}
    games: dict[tuple[str, str], int] = {}
    for x, y, s in results:
        wins[(x, y)] = wins.get((x, y), 0.0) + s
        wins[(y, x)] = wins.get((y, x), 0.0) + (1.0 - s)
        games[(x, y)] = games.get((x, y), 0) + 1
        games[(y, x)] = games.get((y, x), 0) + 1

    # Symmetric game counts for the fit (each unordered pair once).
    seen = set()
    fit_wins: dict[tuple[str, str], float] = {}
    fit_games: dict[tuple[str, str], int] = {}
    for (x, y), n in games.items():
        key = tuple(sorted((x, y)))
        if key in seen:
            continue
        seen.add(key)
        a, b = key
        fit_games[(a, b)] = games[(a, b)]
        fit_wins[(a, b)] = wins[(a, b)]

    theta = _bradley_terry_fit(items, fit_wins, fit_games)

    # Residual between observed and BT-predicted win rate on each directed pair.
    sq = []
    for (a, b), n in fit_games.items():
        if n == 0:
            continue
        obs = fit_wins[(a, b)] / n
        d = theta[a] - theta[b]
        pred = 1.0 / (1.0 + np.exp(-d))
        sq.append((obs - pred) ** 2)
    statistic = float(np.sqrt(np.mean(sq))) if sq else 0.0

    # Threshold: a genuine transitive fit reproduces rates to within sampling noise
    # (~1/sqrt(n_per_pair) ~ a few %). A cycle leaves a large structural residual. 0.1
    # sits well above sampling noise for the tested N and well below the cyclic residual.
    threshold = 0.1
    return {"statistic": statistic, "flagged": statistic > threshold,
            "threshold": threshold}


# ---------------------------------------------------------------------------
# Reliability / expected calibration error.
# ---------------------------------------------------------------------------


def reliability_ece(probs: Sequence[float], outcomes: Sequence[float],
                    *, n_bins: int = 10) -> dict[str, Any]:
    """Expected Calibration Error and per-bin reliability of predicted probabilities."""
    p = np.asarray(probs, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if p.size == 0:
        return {"ece": 0.0, "bins": []}
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    bins = []
    for i in range(n_bins):
        lo, hi = edges[i], edges[i + 1]
        mask = (p >= lo) & (p < hi) if i < n_bins - 1 else (p >= lo) & (p <= hi)
        if not mask.any():
            continue
        conf = float(p[mask].mean())
        acc = float(y[mask].mean())
        weight = float(mask.mean())
        ece += weight * abs(acc - conf)
        bins.append({"lo": float(lo), "hi": float(hi), "conf": conf,
                     "acc": acc, "n": int(mask.sum())})
    return {"ece": float(ece), "bins": bins}
