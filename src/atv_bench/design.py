"""Design / identifiability gates for the rating engine (plan Section 5).

The engine may only publish a factored-out harness rating when the design actually
IDENTIFIES the harness effect apart from the base model. These are STRUCTURAL gates
computed from the design matrix alone (no outcome data), so an estimator can never
manufacture attribution the design does not support.

Three gates:

  - ``crossover_rank(cells)``  : does the harness<->model incidence have enough crossover
    to separate the two effect families? (rank of the two-way design vs the rank a
    fully-identified design would reach).
  - ``design_report(cells)``   : VIF / condition-number of the ACTUAL centered design
    matrix — fires on near-collinearity regardless of the outcomes.
  - ``roster_attribution_plan(roster)`` : the GO/NO-GO for the real v1 roster. A
    model-locked roster (each harness pinned to one base model) has no crossover, so the
    engine emits, per harness, an HONEST harness+model *bundle* (``bundle_unit=True``,
    ``factor_out=False``) rather than a fabricated factored-out theta.
"""
from __future__ import annotations

from typing import Any

import numpy as np

# Non-publishable model tags mirror match_record._NONPUBLISHABLE_MODELS.
_NONPUBLISHABLE_MODELS = {"unknown", "auto", ""}

# Structural ceilings. A perfectly-confounded two-way design has an infinite VIF /
# condition number; a balanced crossover sits near 1. These separate the two regimes
# with wide margin and are properties of the design matrix, not tuned to outcomes.
_CONDITION_CEILING = 1e6
_VIF_CEILING = 10.0


def _index(labels: list[str]) -> dict[str, int]:
    return {lab: i for i, lab in enumerate(sorted(set(labels)))}


def _two_way_design(cells: list[tuple[str, str]]) -> tuple[np.ndarray, list[str], list[str]]:
    """Build the two-way indicator design matrix for the (harness, model) cells.

    Columns: [intercept, harness dummies (drop first), model dummies (drop first)].
    This is the design a Bradley-Terry-style additive model uses to separate the harness
    effect family from the model effect family; its rank tells us whether the two are
    jointly identifiable.
    """
    harnesses = sorted({h for h, _ in cells})
    models = sorted({m for _, m in cells})
    hidx = {h: i for i, h in enumerate(harnesses)}
    midx = {m: i for i, m in enumerate(models)}
    rows = []
    for h, m in cells:
        row = [1.0]
        # drop-first coding for harness and model
        row += [1.0 if hidx[h] == k else 0.0 for k in range(1, len(harnesses))]
        row += [1.0 if midx[m] == k else 0.0 for k in range(1, len(models))]
        rows.append(row)
    return np.asarray(rows, dtype=float), harnesses, models


def crossover_rank(cells: list[tuple[str, str]]) -> dict[str, Any]:
    """Structural crossover gate.

    The additive two-way model has ``1 + (H-1) + (M-1)`` free columns. It is identifiable
    only when the observed (harness, model) incidence spans all of them — i.e. the design
    matrix has full column rank. A model-locked chain (each harness on its own single
    model) leaves the harness and model dummies collinear, so the observed rank falls
    short of ``rank_needed``.
    """
    unique_cells = sorted(set(cells))
    design, harnesses, models = _two_way_design(unique_cells)
    rank = int(np.linalg.matrix_rank(design))
    rank_needed = 1 + (len(harnesses) - 1) + (len(models) - 1)
    return {
        "identifiable": rank >= rank_needed,
        "rank": rank,
        "rank_needed": rank_needed,
        "n_harnesses": len(harnesses),
        "n_models": len(models),
    }


def _vif(design_no_intercept: np.ndarray, col_names: list[str]) -> dict[str, float]:
    """Variance Inflation Factor per column: VIF_j = 1/(1-R_j^2) where R_j^2 regresses
    column j on the other columns. Perfect confounding drives R_j^2 -> 1, VIF -> inf."""
    n_cols = design_no_intercept.shape[1]
    vifs: dict[str, float] = {}
    for j in range(n_cols):
        y = design_no_intercept[:, j]
        X = np.delete(design_no_intercept, j, axis=1)
        if X.shape[1] == 0:
            vifs[col_names[j]] = 1.0
            continue
        # regress y on X (with intercept) via lstsq; R^2 from residuals
        X1 = np.column_stack([np.ones(X.shape[0]), X])
        beta, *_ = np.linalg.lstsq(X1, y, rcond=None)
        resid = y - X1 @ beta
        ss_res = float(resid @ resid)
        ss_tot = float(((y - y.mean()) ** 2).sum())
        if ss_tot <= 1e-12:
            # constant column carries no independent variance; treat as maximal inflation
            vifs[col_names[j]] = float("inf")
            continue
        r2 = 1.0 - ss_res / ss_tot
        r2 = min(max(r2, 0.0), 1.0 - 1e-15)
        vifs[col_names[j]] = 1.0 / (1.0 - r2)
    return vifs


def design_report(cells: list[tuple[str, str]]) -> dict[str, Any]:
    """VIF / condition-number report on the ACTUAL (row-per-observation) design matrix.

    Uses the full cell list (with repeats) so the empirical collinearity of the observed
    design is measured. attributed=True only when the design is well-conditioned AND the
    crossover rank gate passes.
    """
    design, harnesses, models = _two_way_design(cells)
    # Non-intercept columns for VIF (harness + model dummies).
    col_names = ([f"harness::{h}" for h in harnesses[1:]]
                 + [f"model::{m}" for m in models[1:]])
    non_intercept = design[:, 1:]

    # Condition number of the centered design (drop intercept, center columns).
    if non_intercept.shape[1] == 0:
        condition_number = 1.0
    else:
        centered = non_intercept - non_intercept.mean(axis=0, keepdims=True)
        svals = np.linalg.svd(centered, compute_uv=False)
        smax = float(svals[0]) if svals.size else 0.0
        smin = float(svals[-1]) if svals.size else 0.0
        condition_number = smax / smin if smin > 1e-12 else float("inf")

    vif = _vif(non_intercept, col_names) if non_intercept.shape[1] else {}
    max_vif = max(vif.values()) if vif else 1.0

    rank_rep = crossover_rank(cells)
    attributed = (
        rank_rep["identifiable"]
        and condition_number < _CONDITION_CEILING
        and max_vif < _VIF_CEILING
    )
    return {
        "attributed": attributed,
        "condition_number": condition_number,
        "condition_ceiling": _CONDITION_CEILING,
        "vif": vif,
        "vif_ceiling": _VIF_CEILING,
        "rank": rank_rep,
    }


def roster_attribution_plan(roster: list[tuple[str, str]]) -> dict[str, Any]:
    """GO/NO-GO attribution plan for a declared roster of (harness, model) pairs.

    A roster is *model-locked* when there is no crossover — i.e. the (harness, model)
    incidence does not identify the harness effect apart from the model (crossover_rank
    reports not-identifiable). For a locked roster the honest output is, per harness, a
    harness+model BUNDLE: ``bundle_unit=True``, ``factor_out=False``. When the roster has
    real crossover, ``factor_out`` flips True and bundles are unnecessary.

    A harness whose model tag is non-publishable ('unknown'/'auto'/'') can never back a
    published number (mirrors match_record.is_publishable), so ``publishable=False``.
    """
    rank_rep = crossover_rank(roster)
    identifiable = rank_rep["identifiable"]
    model_locked = not identifiable
    factor_out = identifiable

    harness_plans: dict[str, dict[str, Any]] = {}
    for h, m in roster:
        publishable = m.strip().lower() not in _NONPUBLISHABLE_MODELS
        harness_plans[h] = {
            "model": m,
            "bundle_unit": model_locked,
            "factor_out": factor_out,
            "publishable": publishable,
        }
    return {
        "model_locked": model_locked,
        "factor_out": factor_out,
        "harnesses": harness_plans,
        "rank": rank_rep,
    }
