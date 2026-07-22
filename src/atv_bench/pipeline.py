"""Live-pipeline wiring (PR #19 follow-up 3).

The scheduler (G1) and gates (G5/G6) shipped as library + tests but were never called from
the live CLI pipeline. This module is the thin seam that connects them:

  * ``corpus_stats`` derives the load-bearing gate signals from a set of scored rating rows.
  * ``gate_corpus`` runs ``gates.evaluate_quality_gates`` over those signals — the single
    fail-closed check the ``rate --enforce-gates`` path consults before publishing a board.

The scheduler is wired directly in the ``plan-schedule`` CLI command (it needs no derived
stats — it plans from a roster + game list).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from atv_bench.gates import GateThresholds, QualityGateReport, evaluate_quality_gates


def corpus_stats(
    rows: Sequence[Mapping[str, Any]],
    *,
    infrastructure_error_rate: float | None = None,
    referee_nondeterminism_rate: float | None = None,
) -> dict[str, Any]:
    """Derive the gate signals from scored rating rows.

    Each row is a ``{harness_a, harness_b, game?, score_a, ...}`` dict (the rating-corpus
    shape produced by ``runner.match_record_to_rating_row``). We compute the two signals a
    scored corpus CAN measure:

      * ``eligible_n``            count of scored rows.
      * ``min_trials_per_cell``   the minimum per-(unordered pair, game) trial count.

    The other two G6 signals — ``infrastructure_error_rate`` and
    ``referee_nondeterminism_rate`` — CANNOT be measured from scored rows alone: an
    infrastructure crash or a non-deterministic-referee match never produced a scored row, so
    it is absent from this corpus by construction. We therefore emit them ONLY when the caller
    supplies a measured value (it knows the total attempt count / re-run agreement), or when the
    rows themselves carry explicit ``infrastructure_error``/``crashed`` /
    ``referee_nondeterministic`` flags. If neither source is present the signals are OMITTED —
    NOT fabricated as 0.0 — so ``evaluate_quality_gates`` fails CLOSED on the missing signal
    (per its missing-signal contract) instead of a thin corpus silently passing a gate that
    never actually ran.
    """
    n = len(rows)
    cells: Counter = Counter()
    infra_flagged = 0
    nondet_flagged = 0
    have_row_flags = False
    for r in rows:
        pair = tuple(sorted((str(r.get("harness_a")), str(r.get("harness_b")))))
        cells[(pair, str(r.get("game", "")))] += 1
        if any(k in r for k in ("infrastructure_error", "crashed", "referee_nondeterministic")):
            have_row_flags = True
        if r.get("infrastructure_error") or r.get("crashed"):
            infra_flagged += 1
        if r.get("referee_nondeterministic"):
            nondet_flagged += 1

    stats: dict[str, Any] = {
        "eligible_n": n,
        "min_trials_per_cell": min(cells.values()) if cells else 0,
    }
    # infra-error rate: prefer an explicit measured value; else derive from row flags IF the
    # corpus actually carries them; else leave ABSENT so the gate fails closed.
    if infrastructure_error_rate is not None:
        stats["infrastructure_error_rate"] = infrastructure_error_rate
    elif have_row_flags and n:
        stats["infrastructure_error_rate"] = infra_flagged / n
    if referee_nondeterminism_rate is not None:
        stats["referee_nondeterminism_rate"] = referee_nondeterminism_rate
    elif have_row_flags and n:
        stats["referee_nondeterminism_rate"] = nondet_flagged / n
    return stats


def gate_corpus(
    stats: Mapping[str, Any],
    *,
    thresholds: GateThresholds | None = None,
) -> QualityGateReport:
    """Run the fail-closed quality gates over corpus stats (G5/G6).

    ``stats`` may be a pre-derived signal map (as from ``corpus_stats``) or any mapping that
    supplies the required signals. Thin pass-through so callers import ONE pipeline entry.
    """
    return evaluate_quality_gates(stats, thresholds=thresholds)
