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
from atv_bench.runner import append_rating_row
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
    # Set by the CLI when it starts the live server; the engine itself never binds a port, so a
    # pure (headless / --yes / --json) engine run leaves this None.
    live_url: str | None = None

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
            "live_url": self.live_url,
        }


def _row_harness_score(row: dict[str, Any], harness: str) -> float | None:
    """The harness's score in a row (orientation-corrected), or None if not a participant."""
    sa = float(row.get("score_a", 0.5))
    if row.get("harness_a") == harness:
        return sa
    if row.get("harness_b") == harness:
        return 1.0 - sa
    return None


def _measure_referee_nondeterminism(rows, harness) -> float | None:
    """Observed referee-nondeterminism rate from REPEATED, SAME-ORIENTATION matchups.

    Groups rows by their ORDERED (player_a, player_b, game) cell — NOT the unordered pair. The
    trusted deterministic referee should return the same result for the exact same seating every
    time; the fraction of such multi-trial cells whose raw score_a values DISAGREE is the
    measured nondeterminism rate.

    Ordered seating matters: a seat-balanced schedule alternates the harness between seat A and
    seat B, and a harness with a real seat bias (e.g. first-move advantage) legitimately scores
    differently by seat. Bucketing by the UNORDERED pair would mislabel that genuine seat signal
    as referee flakiness and wrongly fail the credibility gate. Returns None when there is no
    repeated same-orientation cell to measure (nothing observed → gate fails closed).
    """
    from collections import defaultdict

    cells: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        ha, hb = str(r.get("harness_a")), str(r.get("harness_b"))
        if harness not in (ha, hb):
            continue
        # raw score_a in the ORDERED seating (no orientation flip): identical seatings must
        # yield an identical referee result.
        cells[(ha, hb, str(r.get("game", "")))].append(float(r.get("score_a", 0.5)))
    multi = [scores for scores in cells.values() if len(scores) >= 2]
    if not multi:
        return None
    disagreeing = sum(1 for scores in multi if len(set(scores)) > 1)
    return disagreeing / len(multi)


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
    seed: int = 0,
    n_boot: int = 500,
    build_board: bool = True,
    progress: ProgressCb | None = None,
) -> QuickstartResult:
    """Run the full harness-vs-bare eval over ``games`` and score it. See module docstring."""
    baseline = f"{BARE_PREFIX}{harness}"
    # Resolve to an absolute path: the scorecard link uses Path.as_uri(), which REQUIRES an
    # absolute path (a relative default like ./quickstart-league would otherwise raise and the
    # promised leaderboard link would silently vanish).
    store = Path(store).resolve()
    store.mkdir(parents=True, exist_ok=True)
    corpus_path = store / "rating_matches.jsonl"

    plan = build_paired_schedule([harness, baseline], list(games), seed=seed, repeats=repeats)

    from atv_bench.store import LeagueStore
    league = LeagueStore(store)

    failures: list[dict[str, Any]] = []
    n_attempted = 0
    run_rows: list[dict[str, Any]] = []  # ONLY this run's rows — scores must not blend prior runs
    for i, match in enumerate(plan):
        n_attempted += 1
        if progress:
            # Carry the EXACT per-match artifact dir + seat labels so the live watcher can bind to
            # the right directory (never a glob of store_dir, which would pick up stale rounds).
            # index0 = harness_a = blue (--a); index1 = harness_b = red (--b), per the seat contract.
            base_out = getattr(execute, "base_out", None)
            match_out = None if base_out is None else str(Path(base_out) / f"{match.game}-{i}")
            progress({"phase": "match", "index": i, "total": len(plan), "game": match.game,
                      "harness_a": match.harness_a, "harness_b": match.harness_b,
                      "match_out": match_out,
                      "seats": {"a": match.harness_a, "b": match.harness_b,
                                "a_color": "blue", "b_color": "red"}})
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
        run_rows.append(row)
        if progress:
            # match_end lets the live watcher poll the just-finished match dir to
            # quiescence and publish its final round (which may have landed only
            # moments before this call returned).
            progress({"phase": "match_end", "index": i, "game": match.game})
        # persist to the rating corpus (history/board) and the ELO store (board render)
        append_rating_row(corpus_path, row)
        try:
            league.append_match(_rating_row_to_elo_record(row))
        except Exception:
            pass  # board is best-effort; the scientific corpus is the source of truth

    # Score ONLY this run's rows — the on-disk corpus may hold prior runs (the store is reusable
    # and the board renders the full history), but the reported per-game/overall/gate scores and
    # the infra-error rate must all describe THIS invocation, not a blend of accumulated runs.
    rows = run_rows

    # --- scientific scores ---
    per_game = per_game_scores(rows, harness=harness, baseline=baseline, seed=seed)
    overall = overall_lift(rows, harness=harness, baseline=baseline, seed=seed, n_boot=n_boot)

    # --- credibility gate (fail closed) ---
    # infra-error rate: genuine match failures over all attempts.
    infra_rate = (len(failures) / n_attempted) if n_attempted else 1.0
    # referee-nondeterminism rate: MEASURED empirically from repeats. The trusted referees are
    # deterministic pure engines by design, so repeated (seat-a, seat-b, game) cells should
    # agree; the fraction of multi-trial cells whose harness-scores disagree is the observed
    # nondeterminism rate. With no repeats there is nothing to measure, so the signal is left
    # ABSENT and the gate fails closed on it (never a fabricated clean 0.0).
    nondet_rate = _measure_referee_nondeterminism(rows, harness)
    stats = corpus_stats(rows, infrastructure_error_rate=infra_rate,
                         referee_nondeterminism_rate=nondet_rate)
    gate_report = gate_corpus(stats)
    credible = gate_report.passed

    # --- local leaderboard link ---
    # Build the ELO board (best-effort) AND the self-contained scorecard, THEN construct the
    # result and persist it once — so quickstart_result.json and the returned object agree
    # (board_url is finalized before the artifact is written).
    board_path: Path | None = None
    board_url: str | None = None
    if build_board:
        try:
            from atv_bench.publish import build_site
            board_path = build_site(str(store / "_board"), store_dir=str(store))
        except Exception:
            board_path = None

    result = QuickstartResult(
        harness=harness, baseline=baseline, model=model, games=list(games),
        n_matches=len(rows), per_game=per_game, overall=overall,
        gate_report=gate_report, credible=credible, failures=failures,
        board_path=board_path, board_url=board_url, corpus_path=corpus_path,
    )

    # The self-contained scorecard page IS the leaderboard link — it renders the actual per-game
    # + overall scores (the ELO board needs published submissions a local run doesn't have).
    if build_board:
        try:
            scorecard = store / "scorecard.html"
            scorecard.write_text(_render_scorecard_html(result))
            result.board_url = scorecard.as_uri()  # store is absolute (resolved above)
            if result.board_path is None:
                result.board_path = store
        except Exception as exc:  # surface, don't swallow — the link is a headline promise
            if progress:
                progress({"phase": "scorecard_failed", "error": f"{type(exc).__name__}: {exc}"})

    (store / "quickstart_result.json").write_text(json.dumps(result.to_dict(), indent=2))
    return result


def _render_scorecard_html(res: "QuickstartResult") -> str:
    """A single self-contained HTML scorecard: overall lift + per-game breakdown + verdict."""
    import html

    o = res.overall
    if o is not None:
        verb = "helps" if o.lo > 0 else "hurts" if o.hi < 0 else "no measurable effect"
        overall_html = (f"<div class='big'>{'+' if o.lift >= 0 else ''}{o.lift:.3f}"
                        f"<span class='ci'>95% CI {o.lo:.3f} … {o.hi:.3f}</span></div>"
                        f"<div class='verb'>harness {verb} vs the bare model</div>")
    else:
        overall_html = "<div class='big'>—</div><div class='verb'>lift undefined (no bare baseline)</div>"
    verdict = "CREDIBLE" if res.credible else "PROVISIONAL — corpus too thin for a defensible rank"
    vclass = "ok" if res.credible else "warn"
    rows_html = ""
    for g in sorted(res.per_game, key=lambda x: x.win_rate, reverse=True):
        pct = g.win_rate * 100
        flag = " <span class='flag'>insufficient N</span>" if g.insufficient else ""
        rows_html += (f"<tr><td>{html.escape(g.game)}</td>"
                      f"<td class='num'>{pct:.1f}%</td><td class='num'>{g.n}</td>"
                      f"<td><div class='bar'><div class='fill' style='width:{pct:.0f}%'></div></div>{flag}</td></tr>")
    fail_html = ""
    if res.failures:
        games = ", ".join(sorted({html.escape(f['game']) for f in res.failures}))
        fail_html = f"<p class='warn'>⚠ {len(res.failures)} match(es) failed to run: {games} (counted as infrastructure error).</p>"
    return f"""<!doctype html><meta charset=utf-8>
<title>ATV-bench — {html.escape(res.harness)} scorecard</title>
<style>
  :root {{ --bg:#0b0e14; --card:#141924; --ink:#e8ecf5; --muted:#8b93a7; --accent:#7aa2ff; --ok:#6ce7be; --warn:#ffc45c; }}
  body {{ background:var(--bg); color:var(--ink); font:15px/1.5 ui-sans-serif,system-ui,sans-serif; margin:0; padding:40px; }}
  .wrap {{ max-width:720px; margin:0 auto; }}
  h1 {{ font-size:18px; font-weight:600; margin:0 0 4px; }} .sub {{ color:var(--muted); margin:0 0 24px; }}
  .card {{ background:var(--card); border-radius:14px; padding:24px 28px; margin-bottom:18px; }}
  .big {{ font-size:44px; font-weight:700; color:var(--ok); letter-spacing:-1px; }}
  .big .ci {{ font-size:14px; font-weight:400; color:var(--muted); margin-left:14px; }}
  .verb {{ color:var(--muted); }}
  .pill {{ display:inline-block; padding:3px 12px; border-radius:999px; font-size:12px; font-weight:600; }}
  .pill.ok {{ background:rgba(108,231,190,.15); color:var(--ok); }} .pill.warn {{ background:rgba(255,196,92,.15); color:var(--warn); }}
  table {{ width:100%; border-collapse:collapse; }} td {{ padding:8px 10px; border-bottom:1px solid #222839; }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; color:var(--muted); width:70px; }}
  .bar {{ background:#222839; border-radius:6px; height:10px; overflow:hidden; }}
  .fill {{ background:var(--accent); height:100%; }} .flag {{ color:var(--warn); font-size:11px; margin-left:6px; }}
  .warn {{ color:var(--warn); }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 12px; }}
</style>
<div class=wrap>
  <h1>{html.escape(res.harness)} <span style='color:var(--muted)'>on</span> {html.escape(res.model)}</h1>
  <p class=sub>harness lift over its bare model — {res.n_matches} matches across {len(res.games)} game(s)</p>
  <div class=card>
    <h2>Overall harness lift</h2>
    {overall_html}
    <p style='margin:16px 0 0'><span class='pill {vclass}'>{verdict}</span></p>
  </div>
  <div class=card>
    <h2>Per-game — win-rate vs bare control</h2>
    <table>{rows_html}</table>
    {fail_html}
  </div>
  <p class=sub>lift = θ(model+harness) − θ(model bare); the base-model term cancels, so this is a pure harness effect. Generated by <code>atv-bench quickstart</code>.</p>
</div>"""


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

    # Advertise the base artifact dir so the engine's match event can carry the EXACT match_out
    # dir (base_out / f"{game}-{index}") for the live watcher to bind to.
    execute.base_out = base_out
    return execute
