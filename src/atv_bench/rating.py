"""Launch rating model (Tier-1) — the statistical core (plan Section 5).

Generative model for player p=(harness Hp, base-model Mp) vs q:

    logit P(p beats q) = (theta_Hp - theta_Hq) + (phi_Mp - phi_Mq) + eps_cell

``theta_H`` is the PUBLISHED harness rating, marginalized over the base model. ``phi_M`` is
a base-model nuisance that is estimated then removed. ``eps_cell`` captures per-cell
overdispersion from LLM run-to-run nondeterminism. Estimation is penalized MLE (ridge on
theta and phi) via ``scipy.optimize``, with a design-identifiability gate up front: when
the harness effect is not separable from the model effect (no crossover / near-collinear
design) the engine REFUSES to publish a factored-out theta (``attributed=False``) rather
than reporting a ridge artefact.

The negative-control tests are the falsification core: an estimator that lets model skill
leak into theta separates identical harnesses (fails), and one that fills a confounded
design via ridge reports attribution it has not earned (fails).
"""
from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
from scipy import optimize, stats

from atv_bench.design import design_report


@dataclasses.dataclass(frozen=True)
class RatingMatch:
    """One head-to-head match outcome for the rating fit.

    ``score_a`` is 1.0 if harness_a's player won, else 0.0.
    """

    harness_a: str
    harness_b: str
    model_a: str
    model_b: str
    score_a: float


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


class RatingResult:
    """Fitted ratings with pairwise-contrast inference."""

    def __init__(
        self,
        *,
        harnesses: list[str],
        theta: np.ndarray,
        cov: np.ndarray,
        attributed: bool,
        data_sufficiency: dict[str, Any],
        dispersion: float = 1.0,
    ):
        self.harnesses = harnesses
        self._idx = {h: i for i, h in enumerate(harnesses)}
        self.theta = theta
        self.cov = cov
        self.attributed = attributed
        self.data_sufficiency = data_sufficiency
        self.dispersion = dispersion

    def theta_of(self, h: str) -> float:
        return float(self.theta[self._idx[h]])

    def pairwise(self, h_i: str, h_j: str, *, z: float = 1.96) -> dict[str, float]:
        """Contrast theta_i - theta_j with a Wald CI from the fitted covariance.

        Var(theta_i - theta_j) = C_ii + C_jj - 2 C_ij. The covariance already carries the
        overdispersion scaling (dispersion factor), so overdispersed fits report wider SEs.
        """
        i, j = self._idx[h_i], self._idx[h_j]
        diff = float(self.theta[i] - self.theta[j])
        var = float(self.cov[i, i] + self.cov[j, j] - 2 * self.cov[i, j])
        var = max(var, 0.0)
        se = float(np.sqrt(var))
        return {"diff": diff, "se": se, "lo": diff - z * se, "hi": diff + z * se}


def _encode(matches: list[RatingMatch], *, factor_out_model: bool = True):
    """Build the design for the additive BT model.

    Parameters: [theta_1..theta_{H-1} (drop harness ref), phi_1..phi_{M-1} (drop model ref)].
    Each match contributes a row X with +/-1 entries for the harness contrast and the model
    contrast. Reference categories are dropped for identifiability; theta for the reference
    is pinned to 0 and recentered afterward.

    When ``factor_out_model`` is False the phi (base-model) columns are OMITTED entirely: the
    estimator becomes MODEL-BLIND and any base-model skill difference between harnesses is
    absorbed into theta. This is the deliberately-broken variant the negative-control tests
    mutate to, proving those controls have teeth on a confounded design.
    """
    harnesses = sorted({m.harness_a for m in matches} | {m.harness_b for m in matches})
    models = sorted({m.model_a for m in matches} | {m.model_b for m in matches})
    hidx = {h: i for i, h in enumerate(harnesses)}
    midx = {m: i for i, m in enumerate(models)}
    n_h = len(harnesses)
    n_m = len(models)
    # free params: harness dummies (drop ref idx 0) + model dummies (drop ref idx 0)
    n_theta = n_h - 1
    n_phi = (n_m - 1) if factor_out_model else 0
    p = n_theta + n_phi

    X = np.zeros((len(matches), p))
    y = np.zeros(len(matches))
    # cell id per row for overdispersion grouping (harness_a, model_a) vs (harness_b,model_b)
    cell_a = []
    cell_b = []
    for t, m in enumerate(matches):
        # harness contrast (drop-first coding: ref harness -> all zeros)
        ha, hb = hidx[m.harness_a], hidx[m.harness_b]
        if ha > 0:
            X[t, ha - 1] += 1.0
        if hb > 0:
            X[t, hb - 1] -= 1.0
        if factor_out_model:
            ma, mb = midx[m.model_a], midx[m.model_b]
            if ma > 0:
                X[t, n_theta + ma - 1] += 1.0
            if mb > 0:
                X[t, n_theta + mb - 1] -= 1.0
        y[t] = float(m.score_a)
        cell_a.append((m.harness_a, m.model_a))
        cell_b.append((m.harness_b, m.model_b))
    return {
        "X": X, "y": y, "harnesses": harnesses, "models": models,
        "n_theta": n_theta, "n_phi": n_phi,
        "cell_a": cell_a, "cell_b": cell_b,
    }


def _fit_penalized(X: np.ndarray, y: np.ndarray, ridge: float):
    """Penalized (ridge) logistic MLE. Returns (beta, hessian)."""
    p = X.shape[1]

    def negll(beta):
        z = X @ beta
        pr = _sigmoid(z)
        pr = np.clip(pr, 1e-12, 1 - 1e-12)
        ll = np.sum(y * np.log(pr) + (1 - y) * np.log(1 - pr))
        ll -= ridge * float(beta @ beta)
        return -ll

    def grad(beta):
        pr = _sigmoid(X @ beta)
        return X.T @ (pr - y) + 2 * ridge * beta

    res = optimize.minimize(np.zeros(p) if False else negll, np.zeros(p),
                            jac=grad, method="L-BFGS-B")
    beta = res.x
    pr = _sigmoid(X @ beta)
    W = pr * (1 - pr)
    H = X.T @ (X * W[:, None]) + 2 * ridge * np.eye(p)
    return beta, H


def _dispersion_factor(X, y, beta) -> float:
    """Pearson-chi-square dispersion estimate: mean squared Pearson residual, floored at 1.

    For a well-specified Bernoulli fit this is ~1; unmodelled per-cell latent shifts
    (overdispersion) inflate it above 1, scaling the covariance and hence the SEs.
    """
    pr = _sigmoid(X @ beta)
    pr = np.clip(pr, 1e-6, 1 - 1e-6)
    resid = (y - pr) / np.sqrt(pr * (1 - pr))
    dof = max(len(y) - X.shape[1], 1)
    phi = float(np.sum(resid ** 2) / dof)
    return max(phi, 1.0)


def fit_ratings(
    matches: list[RatingMatch],
    *,
    model_overdispersion: bool = False,
    ridge: float = 1e-3,
    factor_out_model: bool = True,
) -> RatingResult:
    """Fit the Tier-1 harness ratings with the base model factored out.

    Up-front identifiability gate (design.design_report on the observed (harness, model)
    cells): if the harness effect is not separable from the model effect (no crossover /
    near-collinear), ``attributed=False`` and NO factored-out contrast is trusted. Only a
    design that passes the structural gate yields ``attributed=True`` and inference.

    ``factor_out_model`` (default True) controls whether the base-model nuisance phi is
    modeled. Setting it False produces a MODEL-BLIND fit that absorbs base-model skill into
    theta: on a confounded design (harness correlated with base model) this WRONGLY separates
    identical harnesses. The negative-control tests exercise this False path to prove they
    catch a model-skill leak — if phi modeling were removed from the default path, those
    controls would fail loudly.
    """
    enc = _encode(matches, factor_out_model=factor_out_model)
    X, y = enc["X"], enc["y"]
    harnesses = enc["harnesses"]

    # --- structural identifiability gate (outcome-independent) -------------
    cells = list(zip([m.harness_a for m in matches], [m.model_a for m in matches])) + \
        list(zip([m.harness_b for m in matches], [m.model_b for m in matches]))
    dreport = design_report(cells)
    attributed = bool(dreport["attributed"])

    # --- penalized MLE -----------------------------------------------------
    beta, H = _fit_penalized(X, y, ridge)

    # covariance of the free params; scale by dispersion if requested
    dispersion = 1.0
    if model_overdispersion:
        dispersion = _dispersion_factor(X, y, beta)
    try:
        cov_free = np.linalg.inv(H) * dispersion
    except np.linalg.LinAlgError:
        cov_free = np.linalg.pinv(H) * dispersion

    # --- map free (drop-ref) theta to full centered harness vector ---------
    n_theta = enc["n_theta"]
    n_h = len(harnesses)
    # full theta with reference (idx 0) = 0
    theta_full_raw = np.zeros(n_h)
    theta_full_raw[1:] = beta[:n_theta]

    # Build a linear map A: full_centered_theta = A @ beta_theta_free (+ const from centering)
    # theta_full_raw = S @ beta_free, where S selects theta components.
    S = np.zeros((n_h, X.shape[1]))
    for k in range(n_theta):
        S[k + 1, k] = 1.0
    # centering matrix C = I - (1/n_h) 11^T
    C = np.eye(n_h) - np.ones((n_h, n_h)) / n_h
    A = C @ S  # full_centered_theta = A @ beta
    theta_centered = A @ beta
    cov_theta = A @ cov_free @ A.T

    data_sufficiency = {
        "n_matches": len(matches),
        "n_harnesses": n_h,
        "n_models": len(enc["models"]),
        "condition_number": dreport["condition_number"],
        "identifiable": attributed,
        "dispersion": dispersion,
    }

    return RatingResult(
        harnesses=harnesses,
        theta=theta_centered,
        cov=cov_theta,
        attributed=attributed,
        data_sufficiency=data_sufficiency,
        dispersion=dispersion,
    )


# ---------------------------------------------------------------------------
# Corpus -> ratings.json document (the `atv-bench rate` real path).
# ---------------------------------------------------------------------------


def matches_from_records(records: list[dict[str, Any]]) -> list[RatingMatch]:
    """Convert verified corpus records into RatingMatch rows.

    Each record carries harness_a/harness_b/model_a/model_b/score_a (see corpus.py /
    match_record.py). Records whose model tag is non-publishable are still used to *fit*
    (they carry harness signal) but are surfaced in the ratings doc's ``unknown`` list.
    """
    out = []
    for r in records:
        out.append(RatingMatch(
            harness_a=r["harness_a"], harness_b=r["harness_b"],
            model_a=r["model_a"], model_b=r["model_b"],
            score_a=float(r["score_a"]),
        ))
    return out


def build_ratings_doc(
    records: list[dict[str, Any]],
    *,
    verified: bool = True,
    model_overdispersion: bool = True,
) -> dict[str, Any]:
    """Build the ratings.json document from a verified corpus.

    Emits, per harness: theta and either a model-adjusted theta (when the design factors
    out the model) or a ``bundle_unit`` flag (model-locked). Pairwise CIs carry the
    clustered-bootstrap + BH/FDR treatment over the pairwise family. ``unknown`` lists
    harnesses whose model tag can never back a published number.
    """
    from atv_bench.design import roster_attribution_plan
    from atv_bench.stats import bh_fdr

    _NONPUBLISHABLE = {"unknown", "auto", ""}
    matches = matches_from_records(records)
    if not matches:
        return {"harnesses": [], "attributed": False, "verified": verified,
                "unknown": [], "data_sufficiency": {"n_matches": 0}}

    res = fit_ratings(matches, model_overdispersion=model_overdispersion)

    # Roster attribution plan from the observed (harness -> model) locking.
    roster_pairs = sorted({(m.harness_a, m.model_a) for m in matches}
                          | {(m.harness_b, m.model_b) for m in matches})
    plan = roster_attribution_plan(roster_pairs)

    # Pairwise family for FDR: all harness pairs vs the first harness as reference.
    harnesses = res.harnesses
    ref = harnesses[0]
    raw_p = []
    contrasts = []
    for h in harnesses:
        if h == ref:
            continue
        pw = res.pairwise(h, ref)
        z = pw["diff"] / pw["se"] if pw["se"] > 0 else 0.0
        p = float(2 * (1 - stats.norm.cdf(abs(z))))
        raw_p.append(p)
        contrasts.append((h, pw))
    adj_p = bh_fdr(raw_p) if raw_p else []

    unknown = sorted({m for _, m in roster_pairs if m.strip().lower() in _NONPUBLISHABLE})
    model_map = {h: m for h, m in roster_pairs}

    harness_docs = []
    for i, h in enumerate(harnesses):
        entry = plan["harnesses"].get(h, {})
        bundle = bool(entry.get("bundle_unit", not res.attributed))
        doc = {
            "harness": h,
            "model": model_map.get(h),
            "theta": res.theta_of(h),
            "bundle_unit": bundle,
            "theta_model_adjusted": (None if bundle else res.theta_of(h)),
            "publishable": bool(entry.get("publishable", True)),
        }
        harness_docs.append(doc)

    pairwise_docs = []
    for (h, pw), ap in zip(contrasts, adj_p):
        pairwise_docs.append({
            "harness": h, "ref": ref, "diff": pw["diff"], "se": pw["se"],
            "ci": {"lo": pw["lo"], "hi": pw["hi"]}, "fdr_p": ap,
        })

    return {
        "harnesses": harness_docs,
        "pairwise": pairwise_docs,
        "attributed": res.attributed,
        "model_locked": plan["model_locked"],
        "factor_out": plan["factor_out"],
        "verified": verified,
        "unknown": unknown,
        "data_sufficiency": res.data_sufficiency,
    }
