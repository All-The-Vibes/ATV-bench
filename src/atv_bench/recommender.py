"""Config->outcome recommender for the rating engine (plan Section 5).

Given a rated corpus of head-to-head rows ``{config_a, config_b, profile, score_a}`` the
recommender fits, per profile, a Bradley-Terry logistic model over configs (each config's
strength identified up to an additive constant). For a query profile it ranks configs by
their expected win probability against a random opponent from the field, with a CI derived
from the fitted parameter covariance (parametric bootstrap). It is validated out-of-sample:
on held-out matches it beats the marginal baseline on Brier and log-loss (proper scoring
rules) and its predictions are calibrated.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from scipy import optimize

from atv_bench.stats import reliability_ece


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


class _ProfileModel:
    """Bradley-Terry logistic fit for a single profile."""

    def __init__(self, configs: list[str], theta: np.ndarray, cov: np.ndarray):
        self.configs = configs
        self.idx = {c: i for i, c in enumerate(configs)}
        self.theta = theta
        self.cov = cov

    def win_prob_vs_field(self, theta: np.ndarray) -> np.ndarray:
        """P(config beats a uniformly random distinct config) for every config."""
        k = len(self.configs)
        out = np.empty(k)
        for i in range(k):
            others = [j for j in range(k) if j != i]
            if not others:
                out[i] = 0.5
                continue
            out[i] = np.mean([_sigmoid(theta[i] - theta[j]) for j in others])
        return out

    def pairwise_prob(self, ca: str, cb: str) -> float:
        return float(_sigmoid(self.theta[self.idx[ca]] - self.theta[self.idx[cb]]))


class Recommender:
    """Config->outcome recommender fitted from head-to-head corpus rows."""

    def __init__(self, *, ridge: float = 1e-3, n_boot: int = 400, seed: int = 0):
        self._ridge = ridge
        self._n_boot = n_boot
        self._seed = seed
        self._models: dict[str, _ProfileModel] = {}
        self._base_rate: float = 0.5

    # -- fitting ----------------------------------------------------------

    def fit(self, rows: list[dict[str, Any]]) -> "Recommender":
        by_profile: dict[str, list[dict[str, Any]]] = {}
        scores = []
        for r in rows:
            by_profile.setdefault(r["profile"], []).append(r)
            scores.append(float(r["score_a"]))
        self._base_rate = float(np.mean(scores)) if scores else 0.5
        for prof, prows in by_profile.items():
            self._models[prof] = self._fit_profile(prows)
        return self

    def _fit_profile(self, rows: list[dict[str, Any]]) -> _ProfileModel:
        configs = sorted({c for r in rows for c in (r["config_a"], r["config_b"])})
        idx = {c: i for i, c in enumerate(configs)}
        k = len(configs)
        # Design: x_row = e_ca - e_cb ; target = score_a. Anchor identifiability with ridge.
        X = np.zeros((len(rows), k))
        y = np.zeros(len(rows))
        for t, r in enumerate(rows):
            X[t, idx[r["config_a"]]] += 1.0
            X[t, idx[r["config_b"]]] -= 1.0
            y[t] = float(r["score_a"])

        ridge = self._ridge

        def negll(theta):
            z = X @ theta
            p = _sigmoid(z)
            p = np.clip(p, 1e-12, 1 - 1e-12)
            ll = np.sum(y * np.log(p) + (1 - y) * np.log(1 - p))
            ll -= ridge * float(theta @ theta)
            return -ll

        def grad(theta):
            p = _sigmoid(X @ theta)
            g = X.T @ (p - y) + 2 * ridge * theta
            return g

        res = optimize.minimize(negll, np.zeros(k), jac=grad, method="L-BFGS-B")
        theta = res.x - res.x.mean()  # center (identified up to a constant)

        # Covariance from the Fisher information (Hessian of the neg-log-likelihood).
        p = _sigmoid(X @ theta)
        W = p * (1 - p)
        H = X.T @ (X * W[:, None]) + 2 * ridge * np.eye(k)
        try:
            cov = np.linalg.pinv(H)
        except np.linalg.LinAlgError:
            cov = np.eye(k) * 1e6
        return _ProfileModel(configs, theta, cov)

    # -- recommendation ---------------------------------------------------

    def recommend(self, *, profile: str) -> list[dict[str, Any]]:
        model = self._models.get(profile)
        if model is None:
            raise KeyError(f"unknown profile {profile!r}")
        k = len(model.configs)
        point = model.win_prob_vs_field(model.theta)

        # Parametric bootstrap: sample theta ~ N(theta_hat, cov), recompute win probs.
        rng = np.random.default_rng(self._seed)
        draws = rng.multivariate_normal(model.theta, model.cov, size=self._n_boot)
        draws -= draws.mean(axis=1, keepdims=True)
        boot = np.empty((self._n_boot, k))
        for b in range(self._n_boot):
            boot[b] = model.win_prob_vs_field(draws[b])
        lo = np.percentile(boot, 2.5, axis=0)
        hi = np.percentile(boot, 97.5, axis=0)

        entries = []
        for i, c in enumerate(model.configs):
            wp = float(point[i])
            entries.append({
                "config": c,
                "win_prob": wp,
                "ci": {"lo": float(min(lo[i], wp)), "hi": float(max(hi[i], wp))},
            })
        entries.sort(key=lambda e: e["win_prob"], reverse=True)
        return entries

    # -- evaluation -------------------------------------------------------

    def evaluate(self, test_rows: list[dict[str, Any]]) -> dict[str, Any]:
        preds = []
        ys = []
        for r in test_rows:
            model = self._models.get(r["profile"])
            if model is None or r["config_a"] not in model.idx or r["config_b"] not in model.idx:
                p = self._base_rate
            else:
                p = model.pairwise_prob(r["config_a"], r["config_b"])
            preds.append(p)
            ys.append(float(r["score_a"]))
        preds = np.clip(np.asarray(preds), 1e-12, 1 - 1e-12)
        ys = np.asarray(ys)
        base = float(np.clip(self._base_rate, 1e-12, 1 - 1e-12))

        brier = float(np.mean((preds - ys) ** 2))
        baseline_brier = float(np.mean((base - ys) ** 2))
        log_loss = float(-np.mean(ys * np.log(preds) + (1 - ys) * np.log(1 - preds)))
        baseline_log_loss = float(
            -np.mean(ys * np.log(base) + (1 - ys) * np.log(1 - base)))

        ci_coverage = self._calibration_coverage(preds, ys)
        ece = reliability_ece(preds, ys)["ece"]
        return {
            "brier": brier,
            "baseline_brier": baseline_brier,
            "log_loss": log_loss,
            "baseline_log_loss": baseline_log_loss,
            "ci_coverage": ci_coverage,
            "ece": float(ece),
        }

    @staticmethod
    def _calibration_coverage(preds: np.ndarray, ys: np.ndarray,
                              *, n_bins: int = 10, z: float = 1.96) -> float:
        """Fraction of prediction bins whose Wald CI around the empirical rate contains the
        mean predicted probability. A calibrated model covers ~all bins."""
        edges = np.linspace(0.0, 1.0, n_bins + 1)
        covered = 0
        total = 0
        for i in range(n_bins):
            lo, hi = edges[i], edges[i + 1]
            mask = (preds >= lo) & (preds < hi) if i < n_bins - 1 else (preds >= lo) & (preds <= hi)
            n = int(mask.sum())
            if n == 0:
                continue
            total += 1
            emp = float(ys[mask].mean())
            mean_pred = float(preds[mask].mean())
            se = np.sqrt(max(emp * (1 - emp), 1e-6) / n)
            if emp - z * se <= mean_pred <= emp + z * se:
                covered += 1
        return covered / total if total else 1.0
