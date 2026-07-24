"""T1-liveview: background daemon poll-server + filesystem round watcher.

A `LiveView` starts a `ThreadingHTTPServer` on a background daemon thread that
serves `live.html`, `status.json`, and per-round JSON files out of a private
live dir. A watcher polls the *exact* match dirs it was handed via
`match_start` (never a glob of `store_dir`) for new `round_N.tar.gz`; before
parsing, it applies a mid-write gate (the tar opens cleanly AND contains an
in-tar `results.json` member AND its size is stable across two polls,
parse-with-retry) so a half-written tarball is skipped and retried, never
crashing the watcher.

Rounds are the watcher's job only — the executor emits `match_start` /
`match_end` / `finish`. `finish()` flips `status.json` to `complete` and keeps
serving (daemon thread) so a user can scrub finished rounds until the CLI exits.

status.json schema (drives the live.html UI):
  - per-round winner + seat color (D1)
  - lift value + CI + confidence bucket (D2), populated at finish()

Immutability, small files, explicit errors (project rules). Parsing lives in
frames.py; this module owns lifecycle + the on-disk status contract.
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from atv_bench.frames import (
    Round,
    RoundIncomplete,
    read_round,
)

# --- seat colors (D1/D3) ----------------------------------------------------
# Contract: seat 0 = claude-code = HARNESS = blue (--a);
#           seat 1 = bare-claude-code = CONTROL = red (--b).
_SEAT_COLORS = ("blue", "red")

# Canonical on-disk seat names, in seat-index order. results.json winner is a
# name from this set (or the seat *index* as an int). The display forms may
# carry a "bare:" prefix (BARE_PREFIX) which we normalize before matching.
_CANONICAL_SEAT_NAMES = ("claude-code", "bare-claude-code")

# Poll cadence for the background thread (seconds). The mid-write gate needs two
# observations of a stable size; tests drive `poll_once()` directly instead.
_POLL_INTERVAL_S = 0.25


def _atomic_write_text(path: Path, text: str) -> None:
    """Publish `text` at `path` atomically so a poller never reads a partial file.

    Write to a sibling temp file in the same directory, flush+fsync, then
    os.replace() — a same-filesystem rename is atomic, so a browser polling the
    path sees either the old bytes or the whole new file, never a truncation.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def confidence_bucket(ci_lo: float, ci_hi: float) -> str:
    """Bucket a lift CI into low/medium/high (D2 confidence meter).

    - low: the interval straddles 0 (result not distinguishable from no effect).
    - high: interval excludes 0 and is tight (width < 2.0).
    - medium: excludes 0 but wide.
    """
    if ci_lo <= 0.0 <= ci_hi:
        return "low"
    return "high" if (ci_hi - ci_lo) < 2.0 else "medium"


def _normalize_seat_name(name: str) -> str:
    """Normalize a seat/winner name to its on-disk canonical form.

    The display form carries a "bare:" prefix (e.g. "bare:claude-code") while the
    on-disk arena name uses a dash ("bare-claude-code"). Normalize so a winner
    read from results.json matches whichever seat form the match was bound with.
    """
    return name.replace("bare:", "bare-", 1) if name.startswith("bare:") else name


def _winner_seat_index(winner, seats: tuple[str, str]) -> int | None:
    """Resolve a results.json winner to a seat index (0/1), or None for tie/unknown.

    `winner` may be an int seat index, or a player name. Names are matched against
    the bound seats first, then the canonical seat convention — both normalized
    for the display "bare:" prefix — so bare-control (seat 1) always maps to 1.
    """
    if winner is None:
        return None
    if isinstance(winner, bool):  # guard: bool is an int subclass
        return None
    if isinstance(winner, int):
        return winner if winner in (0, 1) else None
    name = str(winner)
    if name.lower() == "tie":
        return None
    target = _normalize_seat_name(name)
    norm_seats = tuple(_normalize_seat_name(s) for s in seats)
    if target in norm_seats:
        return norm_seats.index(target)
    if target in _CANONICAL_SEAT_NAMES:
        return _CANONICAL_SEAT_NAMES.index(target)
    return None


def _color_for_winner(winner, seats: tuple[str, str]) -> str | None:
    """Map a round winner (name or seat index) to its seat color, None for a tie."""
    idx = _winner_seat_index(winner, seats)
    return _SEAT_COLORS[idx] if idx is not None else None


@dataclass
class _MatchState:
    game: str
    index: int
    match_dir: Path
    seats: tuple[str, str]
    rounds: dict[int, dict] = field(default_factory=dict)
    # round_index -> last observed tar size, for the stable-size gate.
    _sizes: dict[int, int] = field(default_factory=dict)
    _published: set[int] = field(default_factory=set)
    # round_index -> (size, mtime_ns) of a stable tar that failed to parse
    # permanently. A tar matching a cached failure is NOT reparsed every poll;
    # it is retried only once the file changes (size or mtime moves).
    _failed: dict[int, tuple[int, int]] = field(default_factory=dict)


class LiveView:
    """Background daemon server + round watcher for a quickstart run."""

    def __init__(self, store_dir: str, games: list[str], harness: str,
                 baseline: str, rounds_per_match: int = 3) -> None:
        self.store_dir = Path(store_dir)
        self.games = list(games)
        self.harness = harness
        self.baseline = baseline
        self.rounds_per_match = max(1, int(rounds_per_match))

        self.live_dir = self.store_dir / ".live"
        self.live_dir.mkdir(parents=True, exist_ok=True)
        self._copy_page()

        self._matches: list[_MatchState] = []
        self._active: _MatchState | None = None
        self._lock = threading.Lock()
        self._status: dict = {
            "status": "running",
            "harness": harness,
            "baseline": baseline,
            "games": self.games,
            "matches": [],
            "lift": None,
        }
        self._write_status()

        self._stop = threading.Event()
        self._httpd = self._make_server()
        self.port = self._httpd.server_address[1]
        self.url_base = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.url_base}/live.html"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    # --- server -------------------------------------------------------------

    def _make_server(self) -> http.server.ThreadingHTTPServer:
        handler = functools.partial(
            http.server.SimpleHTTPRequestHandler, directory=str(self.live_dir)
        )
        return http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)

    def _serve(self) -> None:
        self._httpd.serve_forever(poll_interval=0.2)

    def _copy_page(self) -> None:
        """Seed live.html (+ its shared canvas shell) into the served dir."""
        view = Path(__file__).parent / "view"
        src = view / "live.html"
        dest = self.live_dir / "live.html"
        if src.exists():
            dest.write_bytes(src.read_bytes())
        elif not dest.exists():
            dest.write_text("<!doctype html><title>live</title>")
        # live.html loads <script src="shell.js"> — seed it beside the page.
        shell = view / "shell.js"
        if shell.exists():
            (self.live_dir / "shell.js").write_bytes(shell.read_bytes())

    # --- lifecycle hooks ----------------------------------------------------

    def match_start(self, game: str, index: int, match_out: str,
                    seats: tuple[str, str]) -> None:
        """Bind the watcher to the exact dir this match writes rounds into."""
        state = _MatchState(
            game=game, index=index, match_dir=Path(match_out), seats=tuple(seats)
        )
        with self._lock:
            self._matches.append(state)
            self._active = state
            self._rebuild_matches_locked()
            self._write_status()

    def match_end(self, game: str, index: int) -> None:
        """Mark a match finished; poll until quiescent so a final round tar that
        appears just before match_end is still published (not lost).

        The mid-write gate needs TWO stable size observations, so a single
        poll_once() can miss a round whose tar landed this instant. Poll to
        quiescence: repeat until a tick publishes nothing, with a short sleep
        between observations so a still-growing tar is caught by the size gate.
        """
        self._poll_until_quiescent()

    def finish(self, lift: float | None = None, ci_lo: float | None = None,
               ci_hi: float | None = None,
               leaderboard_url: str | None = None) -> None:
        """Flip status to complete and record the final lift + CI (D2)."""
        self._poll_until_quiescent()
        with self._lock:
            self._status["status"] = "complete"
            if lift is not None and ci_lo is not None and ci_hi is not None:
                self._status["lift"] = {
                    "value": lift,
                    "ci_lo": ci_lo,
                    "ci_hi": ci_hi,
                    "confidence": confidence_bucket(ci_lo, ci_hi),
                }
            if leaderboard_url is not None:
                self._status["leaderboard_url"] = leaderboard_url
            self._write_status()

    def _poll_until_quiescent(self, max_ticks: int = 40) -> None:
        """Poll to quiescence for match_end/finish.

        A round tar that lands right before the hook fires has only one size
        observation, so a single poll_once() would never satisfy the two-stable-
        observations gate and the round would be lost. Keep polling — sleeping a
        poll interval between ticks so a still-growing tar is observed twice —
        until two consecutive ticks publish nothing. Bounded so a pathologically
        never-settling tar can't hang the CLI at shutdown.
        """
        idle = 0
        for _ in range(max_ticks):
            published = self.poll_once()
            if published:
                idle = 0
            else:
                idle += 1
                if idle >= 2:
                    return
            time.sleep(_POLL_INTERVAL_S)

    def start_watching(self) -> None:
        """Spawn the background poll loop for real runs.

        Tests drive `poll_once()` directly for deterministic gating, so the loop
        is opt-in rather than started in __init__. Idempotent.
        """
        if getattr(self, "_watch_thread", None) is not None:
            return
        self._watch_thread = threading.Thread(target=self._watch_loop, daemon=True)
        self._watch_thread.start()

    def _watch_loop(self) -> None:
        while not self._stop.wait(_POLL_INTERVAL_S):
            try:
                self.poll_once()
            except Exception:
                # A watcher tick must never take down the loop.
                continue

    def close(self) -> None:
        """Stop the server and join the daemon thread cleanly (no hang)."""
        self._stop.set()
        watch = getattr(self, "_watch_thread", None)
        if watch is not None:
            watch.join(timeout=5)
        try:
            self._httpd.shutdown()
        finally:
            self._httpd.server_close()
            self._thread.join(timeout=5)

    # --- watcher ------------------------------------------------------------

    def poll_once(self) -> list[str]:
        """One watcher tick. Returns the per-round JSON filenames published now.

        Applies the mid-write gate per candidate round tar: publish only when the
        tar size is stable across two polls AND it opens with an in-tar
        results.json member. Any parse failure is caught — the round is skipped
        and retried next tick, never crashing the watcher.
        """
        published: list[str] = []
        with self._lock:
            for state in self._matches:
                published.extend(self._poll_match_locked(state))
            if published:
                self._rebuild_matches_locked()
                self._write_status()
        return published

    def _poll_match_locked(self, state: _MatchState) -> list[str]:
        out: list[str] = []
        if not state.match_dir.is_dir():
            return out
        for tar in sorted(state.match_dir.glob("round_*.tar.gz")):
            round_index = _round_index(tar.name)
            if round_index is None or round_index in state._published:
                continue
            name = self._try_publish_round(state, tar, round_index)
            if name is not None:
                out.append(name)
        return out

    def _try_publish_round(self, state: _MatchState, tar: Path,
                           round_index: int) -> str | None:
        # Gate 1: tar size stable across two consecutive polls (still-growing
        # tars are caught here before we ever try to open them).
        try:
            st = tar.stat()
        except OSError:
            return None
        size, mtime_ns = st.st_size, st.st_mtime_ns
        prev = state._sizes.get(round_index)
        state._sizes[round_index] = size
        if prev is None or prev != size or size == 0:
            return None
        # Gate 2: a stable tar that already failed to parse is NOT reparsed every
        # poll — only retried once the file changes (size/mtime moves). This stops
        # a valid-but-malformed tar from being rescanned forever under the lock.
        if state._failed.get(round_index) == (size, mtime_ns):
            return None
        # Gate 3: open ONCE — parse artifacts + read results in a single pass.
        # An incomplete tar (no results member yet) is retried without caching; a
        # half-written/corrupt/malformed tar is a permanent failure cached by
        # (size, mtime_ns) so it isn't reparsed until the arena rewrites it.
        try:
            rnd, results = read_round(tar)
            # results.json may be valid JSON but not an object (list/str/number).
            # Guard here — inside the caught block — so a non-dict payload becomes
            # a cached permanent failure instead of an AttributeError escaping the
            # poll loop on every tick (correctness + DoS).
            if not isinstance(results, dict):
                raise ValueError(f"results.json is {type(results).__name__}, not an object")
        except RoundIncomplete:
            return None
        except Exception:
            state._failed[round_index] = (size, mtime_ns)
            return None
        return self._write_round(state, round_index, rnd, results)

    def _write_round(self, state: _MatchState, round_index: int, rnd: Round,
                     results: dict) -> str:
        winner = results.get("winner")
        color = _color_for_winner(winner, state.seats)
        turn = _round_turn(rnd)
        payload = {
            "game": rnd.game,
            "round": round_index,
            "winner": winner,
            "color": color,
            "seats": list(state.seats),
            "scores": results.get("scores", {}),
            "sims": [_sim_to_dict(s) for s in rnd.sims],
            "chess": [_chess_to_dict(g) for g in rnd.games],
        }
        fname = f"match_{state.index}_round_{round_index}.json"
        _atomic_write_text(self.live_dir / fname, json.dumps(payload))
        # Also publish the browser-shaped round file (round_<n>.json) that live.html
        # polls for the CURRENT match — the served poller reads THIS, not the
        # match-scoped file. Atomic so the canvas never fetches a truncated frame.
        browser = _browser_round(rnd, round_index)
        if browser is not None:
            _atomic_write_text(
                self.live_dir / f"round_{round_index}.json", json.dumps(browser)
            )
        state.rounds[round_index] = {
            "round": round_index,
            "winner": winner,
            "color": color,
            "turn": turn,
            "file": fname,
        }
        state._published.add(round_index)
        self._active = state
        return fname

    # --- status.json --------------------------------------------------------

    def _rebuild_matches_locked(self) -> None:
        self._status["matches"] = [
            {
                "game": s.game,
                "index": s.index,
                "seats": list(s.seats),
                "rounds": [s.rounds[k] for k in sorted(s.rounds)],
            }
            for s in self._matches
        ]

    def _write_status(self) -> None:
        _atomic_write_text(
            self.live_dir / "status.json", json.dumps(self._view_status())
        )

    def _view_status(self) -> dict:
        """Publish status.json in the live.html view contract, a SUPERSET of the
        hermetic `_status` keys.

        live.html polls: state (empty|running|complete), game, seats[{name,color}],
        score{a,b}, rounds[{round,status,winner,turn}], current, complete{...}.
        The engine-facing keys (status, harness, baseline, games, matches, lift)
        stay in place so the hermetic liveview tests keep reading them.
        """
        out = dict(self._status)  # keep engine-facing keys (matches/lift/...)
        active = self._active
        seats = tuple(active.seats) if active else (self.harness, self.baseline)
        out["seats"] = [
            {"name": seats[0], "color": "a"},
            {"name": seats[1], "color": "b"},
        ]
        out["game"] = active.game if active else (self.games[0] if self.games else None)

        rounds, score_a, score_b, current = self._active_rounds_view(active)
        out["rounds"] = rounds
        out["score"] = {"a": score_a, "b": score_b}
        out["current"] = current

        if self._status.get("status") == "complete":
            out["state"] = "complete"
            lift = self._status.get("lift") or {}
            out["complete"] = {
                "lift": lift.get("value"),
                "ci_lo": lift.get("ci_lo"),
                "ci_hi": lift.get("ci_hi"),
                "confidence": lift.get("confidence", "low"),
                "leaderboard_url": self._status.get("leaderboard_url"),
            }
        elif current is None:
            out["state"] = "empty"
        else:
            out["state"] = "running"
        return out

    def _active_rounds_view(self, active: _MatchState | None):
        """Build the D1 round strip for the ACTIVE match: landed rounds first,
        then a `current`/`pending` tail padded to rounds_per_match.

        Returns (rounds, score_a, score_b, current_round_or_None). Winner is the
        seat index (0/1) the strip colors by; None marks a tie.
        """
        rounds: list[dict] = []
        score_a = score_b = 0
        landed_idxs = sorted(active.rounds) if active else []
        for k in landed_idxs:
            r = active.rounds[k]
            winner = _seat_index(r.get("color"))
            if winner == 0:
                score_a += 1
            elif winner == 1:
                score_b += 1
            rounds.append({"round": k, "status": "landed",
                           "winner": winner, "turn": r.get("turn", 0)})
        next_idx = (landed_idxs[-1] + 1) if landed_idxs else 0
        # `current` is the round the canvas should show: the most recent landed
        # round if any (so the page renders gameplay), else the first pending.
        current = landed_idxs[-1] if landed_idxs else None
        if active is not None and next_idx < self.rounds_per_match:
            rounds.append({"round": next_idx, "status": "current",
                           "winner": None, "turn": 0})
            for k in range(next_idx + 1, self.rounds_per_match):
                rounds.append({"round": k, "status": "pending",
                               "winner": None, "turn": 0})
        return rounds, score_a, score_b, current


# --- serialization helpers --------------------------------------------------


def _seat_index(color: str | None) -> int | None:
    """Map a seat color ('blue'/'red') to its seat index (0/1), None for a tie."""
    if color == "blue":
        return 0
    if color == "red":
        return 1
    return None


def _round_turn(rnd: Round) -> int:
    """Final turn count of a parsed round (last sim frame's t, or ply count)."""
    if rnd.sims:
        frames = rnd.sims[0].frames
        return frames[-1].t if frames else 0
    if rnd.games:
        return len(rnd.games[0].fens)
    return 0


def _browser_round(rnd: Round, round_index: int) -> dict | None:
    """Shape a parsed round into the flat payload live.html's canvas consumes.

    live.html reads `round_<n>.json` = {game, round, width/height|rows/cols,
    obstacles, winner, frames:[...]} for sim games, or {game, frames:[{fen}]}
    for chess. Uses the first sim/game of the round (one canvas per round).
    """
    if rnd.sims:
        sim = rnd.sims[0]
        if sim.game == "ants":
            return {
                "game": "ants", "round": round_index,
                "rows": sim.rows, "cols": sim.cols,
                "obstacles": [list(w) for w in sim.water],
                "winner": sim.winner,
                "frames": [
                    {"t": f.t,
                     "ants": [list(a) for a in f.ants],
                     "hills": [list(h) for h in f.hills],
                     "food": [list(x) for x in f.food]}
                    for f in sim.frames
                ],
            }
        return {
            "game": "lightcycles", "round": round_index,
            "width": sim.width, "height": sim.height,
            "obstacles": [list(r) for r in sim.rocks],
            "winner": sim.winner,
            "frames": [
                {"t": f.t,
                 "heads": [list(h) for h in f.heads],
                 "trails": [[list(p) for p in trail] for trail in f.trails]}
                for f in sim.frames
            ],
        }
    if rnd.games:
        g = rnd.games[0]
        return {
            "game": "chess", "round": round_index,
            "winner": g.winner_index,
            "frames": [{"fen": fen} for fen in g.fens],
        }
    return None


def _round_index(name: str) -> int | None:
    stem = name.split(".", 1)[0]  # round_3.tar.gz -> round_3
    _, _, num = stem.partition("round_")
    return int(num) if num.isdigit() else None


def _sim_to_dict(sim) -> dict:
    """Serialize a parsed sim into the JSON the canvas consumes (lazy per round)."""
    if sim.game == "ants":
        return {
            "game": "ants",
            "rows": sim.rows,
            "cols": sim.cols,
            "water": [list(w) for w in sim.water],
            "winner": sim.winner,
            "frames": [
                {"t": f.t,
                 "ants": [list(a) for a in f.ants],
                 "hills": [list(h) for h in f.hills],
                 "food": [list(x) for x in f.food]}
                for f in sim.frames
            ],
        }
    return {
        "game": "lightcycles",
        "width": sim.width,
        "height": sim.height,
        "rocks": [list(r) for r in sim.rocks],
        "winner": sim.winner,
        "frames": [
            {"t": f.t,
             "heads": [list(h) for h in f.heads],
             "trails": [[list(p) for p in trail] for trail in f.trails]}
            for f in sim.frames
        ],
    }


def _chess_to_dict(game) -> dict:
    return {
        "white": game.white,
        "black": game.black,
        "result": game.result,
        "winner_index": game.winner_index,
        "winner_name": game.winner_name,
        "fens": list(game.fens),
    }
