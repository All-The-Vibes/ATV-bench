"""Publication decision & quality/futility gates (gaps G5 + G6).

These are the *rulebook* half of a credible benchmark, ported onto PR17's working execution
spine. PR17 already produces the raw statistics (infrastructure error rate, eligible N,
per-cell trial counts, referee determinism, per-contrast CI + direction stability). This
module turns those numbers into fail-closed decisions:

  * ``evaluate_quality_gates`` — refuse to publish a leaderboard when infrastructure noise or
    thin data dominate (G6 / futility). Mirrors PR16's ``max_infrastructure_error_rate``,
    ``min_eligible_tasks``, ``min_paired_trials_per_cell``, ``max_grader_nondeterminism_rate``.
  * ``decide_contrast`` — turn a single harness-vs-harness contrast (diff + CI) into a
    defensible verdict: ``A_wins`` / ``B_wins`` / ``equivalent`` / ``inconclusive``. A winner
    requires the CI to exclude the preregistered practical ``margin``, the bootstrap direction
    to be stable, and >=2 independent model policies to agree (PR16's ">=2 snapshots" rule).

Everything here is pure and deterministic: no live LLM, no Docker, no I/O.
"""
from __future__ import annotations

import dataclasses
import math
from typing import Any, Mapping


# --------------------------------------------------------------------------- #
# G6 — quality / futility gates
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class GateThresholds:
    """Fail-closed publication thresholds. Defaults follow PR16's charter values."""

    max_infrastructure_error_rate: float = 0.02   # <=2% crashes/timeouts/malformed
    min_eligible_n: int = 50                       # >=50 eligible scored trials
    min_trials_per_cell: int = 5                   # >=5 paired trials per (pair, game) cell
    max_referee_nondeterminism_rate: float = 0.001  # referee must be ~deterministic


@dataclasses.dataclass(frozen=True)
class QualityGateReport:
    passed: bool
    failures: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "failures": list(self.failures)}


def evaluate_quality_gates(
    stats: Mapping[str, Any],
    *,
    thresholds: GateThresholds | None = None,
) -> QualityGateReport:
    """Evaluate futility/quality gates over already-computed corpus statistics.

    ``stats`` must supply every load-bearing signal — the rulebook is FAIL-CLOSED, so a
    missing signal is a ``missing_signal`` failure, never a silent pass (absence of
    evidence is not evidence of quality):
      * ``infrastructure_error_rate``   float in [0,1]
      * ``eligible_n``                  int, count of eligible scored trials
      * ``min_trials_per_cell``         int, the MINIMUM per-cell trial count observed
      * ``referee_nondeterminism_rate`` float in [0,1]

    Returns a report; ``passed`` is True only when every required signal is present AND
    within threshold. Each failure records ``{gate, observed, threshold}`` (or, for an
    absent signal, ``{gate: 'missing_<name>', reason: ...}``).
    """
    t = thresholds or GateThresholds()
    failures: list[dict[str, Any]] = []

    required = (
        "infrastructure_error_rate",
        "eligible_n",
        "min_trials_per_cell",
        "referee_nondeterminism_rate",
    )
    for key in required:
        val = stats.get(key)
        if val is None:
            failures.append({
                "gate": f"missing_{key}",
                "observed": None,
                "threshold": None,
                "reason": f"required signal {key!r} absent; failing closed",
            })
        elif not math.isfinite(val):
            # NaN/inf must fail closed: NaN comparisons are always False, so an unchecked
            # non-finite metric would slip past every threshold below.
            failures.append({
                "gate": f"nonfinite_{key}",
                "observed": val,
                "threshold": None,
                "reason": f"signal {key!r} is non-finite ({val}); failing closed",
            })

    infra = stats.get("infrastructure_error_rate")
    if infra is not None and infra > t.max_infrastructure_error_rate:
        failures.append({
            "gate": "infrastructure_error_rate",
            "observed": infra,
            "threshold": t.max_infrastructure_error_rate,
        })

    eligible = stats.get("eligible_n")
    if eligible is not None and eligible < t.min_eligible_n:
        failures.append({
            "gate": "eligible_n",
            "observed": eligible,
            "threshold": t.min_eligible_n,
        })

    per_cell = stats.get("min_trials_per_cell")
    if per_cell is not None and per_cell < t.min_trials_per_cell:
        failures.append({
            "gate": "min_trials_per_cell",
            "observed": per_cell,
            "threshold": t.min_trials_per_cell,
        })

    nondet = stats.get("referee_nondeterminism_rate")
    if nondet is not None and nondet > t.max_referee_nondeterminism_rate:
        failures.append({
            "gate": "referee_nondeterminism_rate",
            "observed": nondet,
            "threshold": t.max_referee_nondeterminism_rate,
        })

    return QualityGateReport(passed=not failures, failures=failures)


# --------------------------------------------------------------------------- #
# G5 — winner / equivalence decision rule
# --------------------------------------------------------------------------- #
def decide_contrast(
    *,
    diff: float,
    lo: float,
    hi: float,
    margin: float,
    direction_stability: float,
    n_policies: int,
    min_direction_stability: float = 0.9,
    fit_excluded: bool = False,
) -> dict[str, Any]:
    """Decide a single harness-vs-harness contrast.

    ``diff`` is theta_A - theta_B with 95% CI ``[lo, hi]``. ``margin`` is the preregistered
    practical-equivalence half-width. A WINNER requires ALL of:
      * the CI lies entirely beyond the margin band (``lo > +margin`` for A, ``hi < -margin``
        for B) — i.e. the effect is both significant and practically meaningful;
      * ``direction_stability >= min_direction_stability`` — the bootstrap agrees on the sign;
      * ``n_policies >= 2`` — at least two independent model policies back the ranking;
      * ``not fit_excluded`` — the contrast is not explained away by excluded fits.

    ``equivalent`` when the whole CI sits inside the margin band ``(-margin, +margin)``.
    Everything else is ``inconclusive``.

    Returns ``{'verdict': ..., 'reason': ...}``.
    """
    if fit_excluded:
        return {"verdict": "inconclusive",
                "reason": "contrast is FIT_EXCLUDED; cannot attribute a winner"}

    within_band = lo > -margin and hi < margin
    if within_band:
        return {"verdict": "equivalent",
                "reason": f"CI [{lo:.3g}, {hi:.3g}] lies within +/-{margin:g} practical margin"}

    a_significant = lo > margin
    b_significant = hi < -margin

    if a_significant or b_significant:
        if n_policies < 2:
            return {"verdict": "inconclusive",
                    "reason": f"only {n_policies} model policy; need >=2 to declare a winner"}
        if direction_stability < min_direction_stability:
            return {"verdict": "inconclusive",
                    "reason": (f"direction stability {direction_stability:.3g} < "
                               f"{min_direction_stability:g}; sign not robust")}
        if a_significant:
            return {"verdict": "A_wins",
                    "reason": (f"CI [{lo:.3g}, {hi:.3g}] excludes +{margin:g}, direction "
                               f"stable ({direction_stability:.3g}), {n_policies} policies")}
        return {"verdict": "B_wins",
                "reason": (f"CI [{lo:.3g}, {hi:.3g}] excludes -{margin:g}, direction "
                           f"stable ({direction_stability:.3g}), {n_policies} policies")}

    return {"verdict": "inconclusive",
            "reason": (f"CI [{lo:.3g}, {hi:.3g}] neither excludes the +/-{margin:g} margin "
                       f"nor fits inside it")}
