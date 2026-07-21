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


def corpus_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Derive the gate signals from scored rating rows.

    Each row is a ``{harness_a, harness_b, game?, score_a, ...}`` dict (the rating-corpus
    shape produced by ``runner.match_record_to_rating_row``). We compute:

      * ``eligible_n``                  count of scored rows.
      * ``min_trials_per_cell``         the minimum per-(unordered pair, game) trial count.
      * ``infrastructure_error_rate``   fraction of rows flagged as an infra crash/timeout.
      * ``referee_nondeterminism_rate`` fraction flagged referee-nondeterministic.

    Absent infra/nondeterminism flags default to 0.0 (rows that scored cleanly).
    """
    n = len(rows)
    cells: Counter = Counter()
    infra = 0
    nondet = 0
    for r in rows:
        pair = tuple(sorted((str(r.get("harness_a")), str(r.get("harness_b")))))
        cells[(pair, str(r.get("game", "")))] += 1
        if r.get("infrastructure_error") or r.get("crashed"):
            infra += 1
        if r.get("referee_nondeterministic"):
            nondet += 1
    return {
        "eligible_n": n,
        "min_trials_per_cell": min(cells.values()) if cells else 0,
        "infrastructure_error_rate": (infra / n) if n else 0.0,
        "referee_nondeterminism_rate": (nondet / n) if n else 0.0,
    }


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
