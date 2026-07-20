"""Harness LIFT over the bare model (plan Section 5.5).

We cannot factor the base model out of a MODEL-LOCKED roster by crossover, so Section 5.5
measures each harness against ITS OWN bare model, holding the base model M fixed:

    lift(H, M) = theta(M WITH harness H) - theta(M BARE)

Writing a player's launch skill as ``theta_H + phi_M`` (Section 5's decomposition), the
base-model term phi_M CANCELS in the subtraction:

    lift(H, M) = (theta_H + phi_M) - (theta_bare + phi_M) = theta_H - theta_bare.

That cancellation makes lift identifiable WITHOUT crossover — M is its own control — and makes
lifts COMPARABLE across harnesses on DIFFERENT base models even though the raw player thetas
are not (each lift subtracts its own bare baseline, so the incomparable phi term never
survives).

Two public surfaces:

  * ``fit_player_ratings`` — raw per-player (harness, model) Bradley-Terry thetas. NOT
    comparable across base models (phi dominates); the contrast the tests hold lift against.
  * ``compute_lift`` / ``fit_lift`` — per-harness lift = theta(M+H) - theta(bare M), with a
    percentile-bootstrap CI. Raises ``LiftError`` when the declared bare baseline was never
    run on the harness's base model (no baseline to subtract, so no defensible lift).

THE BARE MECHANISM: a bare run invokes the SAME model CLI but under ``isolated_home(None)``
(Section 2) — a fresh HOME seeded with NOTHING, so the harness scaffolding (skills / MCP /
plugins / agents) is physically absent. Probing that empty root yields empty scaffolding
fields; ``manifest_is_bare`` is the published predicate for "this fingerprint is genuinely a
model with its harness stripped, not a real harness relabelled." ``BareModelAdapter`` wraps any
adapter to force that stripped environment onto the run.
"""
from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

import numpy as np
from scipy import optimize

from atv_bench.isolation import isolated_home
from atv_bench.rating import RatingMatch

__all__ = [
    "LiftError",
    "LiftResult",
    "compute_lift",
    "fit_lift",
    "fit_player_ratings",
    "manifest_is_bare",
    "bare_run_env",
    "BareModelAdapter",
]


# ---------------------------------------------------------------------------
# The published "bare" predicate over a fingerprint manifest.
# ---------------------------------------------------------------------------

_SCAFFOLDING_FIELDS = ("skills", "nested_skills", "tools", "mcps", "plugins")


def manifest_is_bare(manifest: Mapping[str, Any]) -> bool:
    """True iff a fingerprint manifest carries ZERO harness scaffolding.

    Every scaffolding surface (skills, nested_skills, tools, mcps, plugins) must be empty,
    ``gstack`` must be False, and there must be no custom agents. This is the negative-space
    proof that a run is the base model with its harness removed — it cannot be faked by a
    manifest that merely relabels a populated harness as "bare" (any populated field ->
    False). ``cli_version`` is a RUNTIME surface, not harness scaffolding, so it is ignored.
    """
    for field_name in _SCAFFOLDING_FIELDS:
        if manifest.get(field_name):
            return False
    if manifest.get("gstack"):
        return False
    if manifest.get("custom_agents_count", 0):
        return False
    return True


# ---------------------------------------------------------------------------
# Bare-run environment + adapter wrapper (same model CLI, stripped harness).
# ---------------------------------------------------------------------------


@contextmanager
def bare_run_env() -> Iterator[dict]:
    """Yield an env dict for a BARE run: a fresh HOME seeded with NO harness config.

    ``isolated_home(None)`` points HOME/XDG at an empty per-run tmpdir with no settings.json,
    no skills/, no .claude.json — so the same model CLI finds no user/project scaffolding to
    load. Combined with the adapters' headless flags (``--permission-mode acceptEdits`` /
    ``--no-ask-user``), interactive project loading is already suppressed, so the run is the
    model with its harness genuinely removed.
    """
    with isolated_home(None) as env:
        yield env


@dataclasses.dataclass
class BareModelAdapter:
    """Wrap any adapter so every run executes under a stripped (bare) HOME.

    The wrapped adapter's own env is overridden with a ``bare_run_env`` per run: same model
    CLI, zero harness scaffolding. A fingerprint taken of that run's HOME is empty across
    every scaffolding field (``manifest_is_bare`` True) — the bare control the lift subtracts.
    """

    inner: Any

    def run(self, req: Any) -> Any:
        with bare_run_env() as env:
            bare_req = dataclasses.replace(req, env=env)
            return self.inner.run(bare_req)


# ---------------------------------------------------------------------------
# Lift result + error.
# ---------------------------------------------------------------------------


class LiftError(ValueError):
    """Raised when a harness's declared bare baseline was never run on its base model."""


@dataclasses.dataclass(frozen=True)
class LiftResult:
    """Lift of one harness over its bare baseline on a shared base model, with a CI."""

    harness: str
    bare_harness: str
    base_model: str
    lift: float
    lo: float
    hi: float


# ---------------------------------------------------------------------------
# Player-level Bradley-Terry fit (players are (harness, model) pairs).
# ---------------------------------------------------------------------------


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _build_player_design(matches: list[RatingMatch]):
    """Design for a per-PLAYER Bradley-Terry fit. Player = (harness, model).

    Drop-first coding on the player dummies (reference player -> all-zero row contribution);
    the reference theta is pinned to 0 then the full vector is recentered to mean 0.
    """
    players = sorted(
        {(m.harness_a, m.model_a) for m in matches}
        | {(m.harness_b, m.model_b) for m in matches}
    )
    pidx = {p: i for i, p in enumerate(players)}
    n_players = len(players)
    n_free = max(n_players - 1, 0)

    X = np.zeros((len(matches), n_free))
    y = np.zeros(len(matches))
    for t, m in enumerate(matches):
        ia = pidx[(m.harness_a, m.model_a)]
        ib = pidx[(m.harness_b, m.model_b)]
        if ia > 0:
            X[t, ia - 1] += 1.0
        if ib > 0:
            X[t, ib - 1] -= 1.0
        y[t] = float(m.score_a)
    return players, pidx, X, y


def _fit_theta(X: np.ndarray, y: np.ndarray, n_players: int, ridge: float = 1e-4) -> np.ndarray:
    """Penalized logistic MLE -> full, mean-centered per-player theta vector.

    A tiny ridge keeps the fit well-posed without materially biasing the logit gap the lift
    reads. The reference player's theta is pinned to 0 before centering.
    """
    n_free = X.shape[1]
    if n_free == 0:
        return np.zeros(n_players)

    def negll(beta):
        z = X @ beta
        pr = np.clip(_sigmoid(z), 1e-12, 1 - 1e-12)
        ll = np.sum(y * np.log(pr) + (1 - y) * np.log(1 - pr))
        ll -= ridge * float(beta @ beta)
        return -ll

    def grad(beta):
        pr = _sigmoid(X @ beta)
        return X.T @ (pr - y) + 2 * ridge * beta

    res = optimize.minimize(negll, np.zeros(n_free), jac=grad, method="L-BFGS-B")
    full = np.zeros(n_players)
    full[1:] = res.x
    full -= full.mean()
    return full


def fit_player_ratings(matches: list[RatingMatch]) -> dict[tuple[str, str], float]:
    """Raw per-player (harness, model) Bradley-Terry thetas.

    These are NOT comparable across base models — the base-model term phi_M dominates the
    cross-model gap — which is exactly why lift (a within-model subtraction) is needed. This
    is the baseline the identifiability test contrasts against the phi-cancelling lift.
    """
    players, _pidx, X, y = _build_player_design(matches)
    theta = _fit_theta(X, y, len(players))
    return {p: float(theta[i]) for i, p in enumerate(players)}


# ---------------------------------------------------------------------------
# Lift = theta(M+H) - theta(bare M), with a percentile-bootstrap CI.
# ---------------------------------------------------------------------------


def _base_model_for(harness: str, players: list[tuple[str, str]]) -> str | None:
    """The base model a harness ran on. Returns None if the harness never played."""
    models = sorted({m for (h, m) in players if h == harness})
    if not models:
        return None
    # Model-locked roster: a harness runs on exactly one base model. If (defensively) more
    # than one appears, the first is the canonical base model for the lift.
    return models[0]


def compute_lift(
    matches: list[RatingMatch],
    baselines: Mapping[str, str],
    *,
    seed: int = 0,
    n_boot: int = 1000,
    ci: float = 0.95,
) -> dict[str, LiftResult]:
    """Per-harness lift over its bare baseline: theta(M+H) - theta(bare M).

    ``baselines`` maps each harness to the name of its BARE control harness. For each harness
    H on base model M with bare control B, the point lift is theta[(H, M)] - theta[(B, M)];
    the phi_M term shared by both players cancels, so the number is a pure harness effect and
    is comparable across harnesses on different base models.

    CI: a percentile bootstrap that resamples whole MATCHES with replacement (each match is an
    independent run — the run-to-run nondeterminism lives at the row level) and refits the
    player thetas on each replicate, recomputing every lift. The seed pins the replicate draw.

    Raises ``LiftError`` if a harness never played, or if its declared bare baseline was never
    run on the same base model (theta(bare) undefined -> no baseline to subtract).
    """
    if not matches:
        raise LiftError("no matches supplied")

    players, pidx, X, y = _build_player_design(matches)
    n_players = len(players)

    # --- resolve (harness, base_model, baseline_player) up front, fail closed -----------
    plan: dict[str, dict[str, Any]] = {}
    for harness, bare_harness in baselines.items():
        base_model = _base_model_for(harness, players)
        if base_model is None:
            raise LiftError(f"harness {harness!r} has no matches to rate")
        baseline_player = (bare_harness, base_model)
        if baseline_player not in pidx:
            raise LiftError(
                f"no bare baseline {bare_harness!r} run on base model {base_model!r} for "
                f"harness {harness!r} — theta(bare) is undefined, cannot compute lift")
        harness_player = (harness, base_model)
        if harness_player not in pidx:
            raise LiftError(f"harness {harness!r} has no player on base model {base_model!r}")
        plan[harness] = {
            "bare_harness": bare_harness,
            "base_model": base_model,
            "h_idx": pidx[harness_player],
            "b_idx": pidx[baseline_player],
        }

    def _lifts_from_theta(theta: np.ndarray) -> dict[str, float]:
        return {
            h: float(theta[p["h_idx"]] - theta[p["b_idx"]]) for h, p in plan.items()
        }

    # --- point estimate -----------------------------------------------------------------
    theta_hat = _fit_theta(X, y, n_players)
    point = _lifts_from_theta(theta_hat)

    # --- bootstrap CI (resample matches i.i.d.; each match is an independent run) --------
    rng = np.random.default_rng(seed)
    n = X.shape[0]
    boot: dict[str, list[float]] = {h: [] for h in plan}
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        theta_b = _fit_theta(X[idx], y[idx], n_players)
        for h, val in _lifts_from_theta(theta_b).items():
            boot[h].append(val)

    alpha = 1.0 - ci
    lo_q, hi_q = 100 * (alpha / 2), 100 * (1 - alpha / 2)

    out: dict[str, LiftResult] = {}
    for harness, p in plan.items():
        draws = np.asarray(boot[harness], dtype=float)
        lo, hi = np.percentile(draws, [lo_q, hi_q])
        out[harness] = LiftResult(
            harness=harness,
            bare_harness=p["bare_harness"],
            base_model=p["base_model"],
            lift=point[harness],
            lo=float(lo),
            hi=float(hi),
        )
    return out


def fit_lift(
    matches: list[RatingMatch],
    baselines: Mapping[str, str],
    *,
    seed: int = 0,
    n_boot: int = 1000,
    ci: float = 0.95,
) -> dict[str, LiftResult]:
    """Documented alias of :func:`compute_lift`."""
    return compute_lift(matches, baselines, seed=seed, n_boot=n_boot, ci=ci)
