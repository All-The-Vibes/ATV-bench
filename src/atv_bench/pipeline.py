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
    ``referee_nondeterminism_rate`` — CANNOT be measured from scored rows at all: an
    infrastructure crash or a non-deterministic-referee match never produced a scored row, so
    it is absent from this corpus by construction. A per-row flag like
    ``infrastructure_error: false`` on a SCORED row is therefore NOT a measurement of the
    corpus-wide rate (the failures that matter are the rows that never appear here), so we do
    NOT derive these signals from row flags. They are emitted ONLY when the caller supplies a
    measured value (it knows the total attempt count / re-run agreement). If the caller
    supplies nothing the signals are OMITTED — never fabricated as 0.0 — so
    ``evaluate_quality_gates`` fails CLOSED on the missing signal (per its missing-signal
    contract) instead of a corpus silently passing a gate that never actually ran.
    """
    n = len(rows)
    cells: Counter = Counter()
    for r in rows:
        pair = tuple(sorted((str(r.get("harness_a")), str(r.get("harness_b")))))
        cells[(pair, str(r.get("game", "")))] += 1

    stats: dict[str, Any] = {
        "eligible_n": n,
        "min_trials_per_cell": min(cells.values()) if cells else 0,
    }
    # infra-error + referee-nondeterminism rates are emitted ONLY from an explicit measured
    # value supplied by the caller (which knows the full attempt population); otherwise ABSENT
    # so the gate fails closed. A scored row's own flag is never treated as the corpus rate.
    if infrastructure_error_rate is not None:
        stats["infrastructure_error_rate"] = infrastructure_error_rate
    if referee_nondeterminism_rate is not None:
        stats["referee_nondeterminism_rate"] = referee_nondeterminism_rate
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
