"""The `atv-bench quickstart` evaluation engine.

Given a harness + model, this orchestrates the whole scientific eval:

  plan (harness vs its bare control across N games)
    → execute each match in isolation (INJECTED executor; the real one wraps run_live_match)
    → persist a rating corpus (for lift/per-game) + an ELO store (for the board render)
    → score: overall harness-over-bare lift (clustered CI) + per-game breakdown
    → gate: the G5/G6 quality gates decide credible vs provisional (fail closed)
    → build a local leaderboard site and return the link.

The ``execute`` and ``progress`` seams are injected so the engine is hermetically testable
without Docker: a stub executor returns canned rating rows; the CLI passes the live executor.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any, Callable, Sequence

from atv_bench.adapters.contract import BARE_PREFIX
from atv_bench.gates import QualityGateReport
from atv_bench.lift import LiftResult
from atv_bench.pergame import GameScore, overall_lift, per_game_scores
from atv_bench.pipeline import corpus_stats, gate_corpus
from atv_bench.runner import append_rating_row, load_rating_rows
from atv_bench.scheduler import build_paired_schedule

# The executor contract: run ONE match and return a rating-corpus row (or raise).
MatchExecutor = Callable[..., dict[str, Any]]
ProgressCb = Callable[[dict[str, Any]], None]


@dataclasses.dataclass
class QuickstartResult:
    """Everything the quickstart UX needs to present + a machine-readable summary."""

    harness: str
    baseline: str
    model: str
    games: list[str]
    n_matches: int
    per_game: list[GameScore]
    overall: LiftResult | None
    gate_report: QualityGateReport | None
    credible: bool
    failures: list[dict[str, Any]]
    board_path: Path | None
    board_url: str | None
    corpus_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "harness": self.harness,
            "baseline": self.baseline,
            "model": self.model,
            "games": self.games,
            "n_matches": self.n_matches,
            "credible": self.credible,
            "overall": (
                None if self.overall is None
                else {"lift": self.overall.lift, "lo": self.overall.lo, "hi": self.overall.hi,
                      "n_boot_used": self.overall.n_boot_used}
            ),
            "per_game": [dataclasses.asdict(g) for g in self.per_game],
            "gate": None if self.gate_report is None else self.gate_report.to_dict(),
            "failures": self.failures,
            "board_path": None if self.board_path is None else str(self.board_path),
            "board_url": self.board_url,
            "corpus_path": str(self.corpus_path),
        }


def _rating_row_to_elo_record(row: dict[str, Any]) -> dict[str, Any]:
    """Map a rating row (harness_a/score_a) to the ELO store's match record for the board."""
    score_a = float(row.get("score_a", 0.5))
    if score_a >= 0.75:
        outcome = "a_wins"
    elif score_a <= 0.25:
        outcome = "b_wins"
    else:
        outcome = "draw"
    return {
        "player_a": row["harness_a"], "player_b": row["harness_b"],
        "outcome": outcome, "match_id": row["match_id"],
    }


def run_quickstart_eval(
    *,
    harness: str,
    model: str,
    games: Sequence[str],
    repeats: int = 1,
    store: Path,
    execute: MatchExecutor,
    homes: dict[str, Path | None] | None = None,
    seed: int = 0,
    n_boot: int = 500,
    build_board: bool = True,
    progress: ProgressCb | None = None,
) -> QuickstartResult:
    """Run the full harness-vs-bare eval over ``games`` and score it. See module docstring."""
    baseline = f"{BARE_PREFIX}{harness}"
    store = Path(store)
    store.mkdir(parents=True, exist_ok=True)
    corpus_path = store / "rating_matches.jsonl"

    plan = build_paired_schedule([harness, baseline], list(games), seed=seed, repeats=repeats)

    from atv_bench.store import LeagueStore
    league = LeagueStore(store)

    failures: list[dict[str, Any]] = []
    n_attempted = 0
    for i, match in enumerate(plan):
        n_attempted += 1
        if progress:
            progress({"phase": "match", "index": i, "total": len(plan), "game": match.game,
                      "harness_a": match.harness_a, "harness_b": match.harness_b})
        try:
            row = execute(
                harness_a=match.harness_a, harness_b=match.harness_b,
                game=match.game, model=model, seed=seed, index=i,
            )
        except Exception as exc:  # a single arena failing must not abort the whole eval
            failures.append({"game": match.game, "index": i, "error": f"{type(exc).__name__}: {exc}"})
            if progress:
                progress({"phase": "match_failed", "index": i, "game": match.game,
                          "error": str(exc)})
            continue
        # persist to the rating corpus (lift/per-game) and the ELO store (board render)
        append_rating_row(corpus_path, row)
        try:
            league.append_match(_rating_row_to_elo_record(row))
        except Exception:
            pass  # board is best-effort; the scientific corpus is the source of truth

    rows = load_rating_rows(corpus_path)

    # --- scientific scores ---
    per_game = per_game_scores(rows, harness=harness, baseline=baseline, seed=seed)
    overall = overall_lift(rows, harness=harness, baseline=baseline, seed=seed, n_boot=n_boot)

    # --- credibility gate (fail closed): measure infra-error rate over ALL attempts ---
    infra_rate = (len(failures) / n_attempted) if n_attempted else 1.0
    stats = corpus_stats(rows, infrastructure_error_rate=infra_rate)
    gate_report = gate_corpus(stats)
    credible = gate_report.passed

    # --- local leaderboard link ---
    board_path: Path | None = None
    board_url: str | None = None
    if build_board:
        try:
            from atv_bench.publish import build_site
            board_path = build_site(str(store / "_board"), store_dir=str(store))
            board_url = (board_path / "index.html").as_uri()
        except Exception:
            board_path = None

    # persist the machine-readable result beside the board
    result = QuickstartResult(
        harness=harness, baseline=baseline, model=model, games=list(games),
        n_matches=len(rows), per_game=per_game, overall=overall,
        gate_report=gate_report, credible=credible, failures=failures,
        board_path=board_path, board_url=board_url, corpus_path=corpus_path,
    )
    (store / "quickstart_result.json").write_text(json.dumps(result.to_dict(), indent=2))
    return result


def live_match_executor(
    *, rounds: int = 3, out_dir: Path | None = None,
    homes: dict[str, Path | None] | None = None,
) -> MatchExecutor:  # pragma: no cover - Docker + live CLIs
    """The REAL executor: run one harness-vs-bare match in the arena and return a rating row.

    Wraps the same seam the `run` command uses (preflight → run_live_match → build_match_record
    → match_record_to_rating_row), so a quickstart match is byte-for-byte the vetted live path.
    Docker/CLI-gated, so it is excluded from the hermetic suite; the engine is tested with a
    stub executor instead.
    """
    import tempfile

    from atv_bench.runner import (
        RunConfig, build_match_record, fingerprint_harness_repo,
        match_record_to_rating_row, preflight_or_raise, run_live_match, summarize_budgets,
    )

    base_out = Path(out_dir) if out_dir else Path(tempfile.mkdtemp(prefix="atv-quickstart-"))
    homes = homes or {}

    def execute(*, harness_a, harness_b, game, model, seed, index):
        cfg = RunConfig(game=game, a=harness_a, b=harness_b, model=model, rounds=rounds)
        preflight_or_raise(cfg)
        match_out = base_out / f"{game}-{index}"
        match_homes = {harness_a: homes.get(harness_a), harness_b: homes.get(harness_b)}
        raw = run_live_match(cfg, output_dir=match_out, homes=match_homes)
        fps: dict[str, str] = {}
        manifests: dict[str, dict] = {}
        for h, home in match_homes.items():
            try:
                sha, manifest = fingerprint_harness_repo(h, home)
                fps[h], manifests[h] = sha, manifest
            except Exception:
                fps[h] = "0" * 64
        from atv_bench.cli import _summarize_tournament
        outcome, models = _summarize_tournament(raw, cfg)
        budgets = summarize_budgets(raw, cfg)
        rec = build_match_record(
            cfg, outcome=outcome, player_models=models, player_fingerprints=fps,
            player_manifests=manifests, player_budgets=budgets,
            replay_path=str(match_out), verified=False,
        )
        row = match_record_to_rating_row(rec)
        row.setdefault("game", game)
        row["match_id"] = f"{game}-{index}"
        return row

    return execute
