"""Per-game + overall scientific scores for `atv-bench quickstart`.

Two views of the same corpus:
  * ``per_game_scores`` — one ``GameScore`` per arena: the harness's win-rate over its bare
    control in that game, flagged ``insufficient`` when the arena has too few trials for a
    defensible number.
  * ``overall_lift`` — the pooled harness-over-bare lift with a CLUSTERED bootstrap CI (games
    are the clusters), i.e. the headline Section-5.5 metric.

Fail closed: a thin game is flagged, not fabricated; an absent bare baseline yields ``None``
rather than a phantom 0.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Mapping, Sequence

from atv_bench.lift import LiftError, LiftResult, compute_lift
from atv_bench.rating import matches_from_records


@dataclasses.dataclass(frozen=True)
class GameScore:
    """One arena's harness-over-bare result."""

    game: str
    n: int
    win_rate: float          # harness win fraction in this game (orientation-corrected)
    # per-game SIGNED ADVANTAGE in [-1, 1]: 2*win_rate - 1 (0 == parity with bare). This is a
    # win-rate-derived point estimate for ONE arena, NOT the theta-difference lift that
    # `overall_lift`/`lift.compute_lift` fit across the pooled corpus — a single game is one
    # cluster and cannot carry a defensible bootstrap CI, so lo/hi stay None here.
    lift: float | None
    lo: float | None
    hi: float | None
    insufficient: bool       # too few trials for a defensible score


def _harness_score(row: Mapping[str, Any], harness: str) -> float | None:
    """The harness's score in a row (1 win / 0 loss / 0.5 tie), orientation-corrected.

    Returns None if the harness is not a participant in the row.
    """
    ha, hb = row.get("harness_a"), row.get("harness_b")
    sa = float(row.get("score_a", 0.0))
    if ha == harness:
        return sa
    if hb == harness:
        return 1.0 - sa
    return None


def per_game_scores(
    rows: Sequence[Mapping[str, Any]],
    harness: str,
    baseline: str,
    *,
    min_trials: int = 5,
    seed: int = 0,
) -> list[GameScore]:
    """Score each game the harness played against its bare control.

    ``min_trials`` is the fail-closed threshold: a game with fewer scored trials is reported
    (raw win-rate is still shown) but flagged ``insufficient`` so the caller never presents a
    thin arena as a defensible number.
    """
    by_game: dict[str, list[Mapping[str, Any]]] = {}
    for r in rows:
        # only harness-vs-its-bare-control rows contribute to this contrast
        pair = {r.get("harness_a"), r.get("harness_b")}
        if harness in pair and baseline in pair:
            by_game.setdefault(str(r.get("game", "")), []).append(r)

    out: list[GameScore] = []
    for game, grows in sorted(by_game.items()):
        scores = [s for r in grows if (s := _harness_score(r, harness)) is not None]
        n = len(scores)
        win_rate = (sum(scores) / n) if n else 0.0
        lift = lo = hi = None
        insufficient = n < min_trials
        if not insufficient:
            # a single game is one cluster; compute_lift needs >=2 clusters for a CI, so per
            # game we report the point lift only (win_rate-derived) and leave the CI to overall.
            lift = 2.0 * win_rate - 1.0  # signed advantage in [-1, 1]; 0 == parity with bare
        out.append(GameScore(game=game, n=n, win_rate=win_rate, lift=lift, lo=lo, hi=hi,
                             insufficient=insufficient))
    return out


def overall_lift(
    rows: Sequence[Mapping[str, Any]],
    harness: str,
    baseline: str,
    *,
    seed: int = 0,
    n_boot: int = 1000,
) -> LiftResult | None:
    """Pooled harness-over-bare lift with a clustered bootstrap CI (games are the clusters).

    Returns None ONLY when the contrast is genuinely undefined — the harness or its bare
    baseline never played, so there is no baseline to subtract. When the contrast DOES exist but
    has too few game-clusters for a clustered CI (e.g. a single-game quickstart), we fall back to
    the i.i.d.-row bootstrap so the caller still gets a real lift point + CI rather than a
    misleading "baseline never ran". A genuinely unrateable contrast (no baseline on the base
    model) still returns None.
    """
    contrast = [
        r for r in rows
        if {r.get("harness_a"), r.get("harness_b")} == {harness, baseline}
    ]
    if not contrast:
        return None
    matches = matches_from_records(list(contrast))
    cluster_ids = [str(r.get("game", "")) for r in contrast]
    n_game_clusters = len(set(cluster_ids))
    try:
        if n_game_clusters >= 2:
            result = compute_lift(matches, {harness: baseline}, seed=seed, n_boot=n_boot,
                                  cluster_ids=cluster_ids)
        else:
            # one game => one cluster: a clustered CI is undefined, but the i.i.d. bootstrap is
            # the correct estimator here (a single game's matches ARE the independent unit).
            result = compute_lift(matches, {harness: baseline}, seed=seed, n_boot=n_boot)
    except LiftError:
        # the contrast exists but the baseline was never run on the harness's base model, so
        # theta(bare) is undefined — genuinely unrateable.
        return None
    return result.get(harness)
