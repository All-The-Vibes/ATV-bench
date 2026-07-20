"""Local match runner + replay builder behind `atv-bench play`.

This is the UX the demo was missing: run a *real* refereed match locally — your chosen
player against a chosen opponent from the bot series (or your own harness-built bot
file) — and SEE it. It reuses the exact same trusted engine + referee as the sandboxed
arena (`atv_bench.arena`), so the outcome is adjudicated from real gameplay, never
mocked. The match records frames (see `referee.run_match(record=True)`), which we render
as ASCII and as a self-contained animated HTML replay.

A `Contestant` is either a named bot (`key`) from the registry, or a harness-built bot
file (`bot_path`) driven as an untrusted move-only subprocess — the same transport the
arena uses for submissions. Determinism holds: same seed → same frames → same replay.
"""
from __future__ import annotations

import html as _html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atv_bench.arena.engine import Direction, TronEngine
from atv_bench.arena.referee import MoveSource, SubprocessMoveSource, run_match
from atv_bench.bots import make_bot
from atv_bench.games import assert_playable

# Trusted, fixed local-match parameters — mirror the sandboxed arena so a local result
# is comparable to a submitted one.
_BOARD_W = 25
_BOARD_H = 25
_MAX_TURNS = 400
_PER_TURN_TIMEOUT = 2.0


@dataclass(frozen=True)
class Contestant:
    """One side of a local match: a named bot OR a harness-built bot file.

    Exactly one of `key` / `bot_path` should be set. `label` overrides the display name
    (defaults to the bot key or the file's parent/name).
    """

    key: str | None = None
    bot_path: str | None = None
    label: str | None = None

    def display(self) -> str:
        if self.label:
            return self.label
        if self.key:
            return self.key
        if self.bot_path:
            return Path(self.bot_path).stem or "bot"
        return "player"

    def move_source(self, player: str) -> MoveSource:
        if self.bot_path:
            import sys
            return SubprocessMoveSource([sys.executable, self.bot_path],
                                        per_turn_timeout=_PER_TURN_TIMEOUT)
        if self.key:
            return make_bot(self.key, player=player)  # raises ValueError if unknown
        raise ValueError("contestant needs a bot key or a bot_path")


def _engine() -> TronEngine:
    # Same board size / turn cap / timeout as the sandboxed arena, so a local match runs
    # under the same engine and rules a submission is judged by. Start geometry differs
    # deliberately: rows 9 and 15 are symmetric about the center (12) on a 25-tall board —
    # a fair but OFFSET start (vs the arena's head-on y=12) so bots don't just rush
    # straight into a turn-1 mutual crash. Longer, decisive, watchable matches.
    return TronEngine(
        width=_BOARD_W, height=_BOARD_H,
        start_a=(1, 9), start_b=(_BOARD_W - 2, 15),
        dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=_MAX_TURNS,
    )


def run_local_match(*, game: str, player: Contestant, opponent: Contestant,
                    seed: int = 0) -> dict[str, Any]:
    """Run a refereed local match and return the recorded, adjudicated result.

    Fails closed (ValueError) on a non-playable game or an unknown bot key, before
    spawning anything — the same integrity gate `submit` uses.
    """
    assert_playable(game)
    # Only lightcycles has an engine today; `assert_playable` lets any live game through,
    # so guard explicitly rather than silently run a lightcycles board mislabeled as
    # another game once a second arena ships.
    if game != "lightcycles":
        raise ValueError(
            f"game {game!r} has no local engine yet — only 'lightcycles' can be played "
            f"locally. (see `atv-bench games`)."
        )
    engine = _engine()
    # Construct BOTH move sources inside the ExitStack: if the opponent (e.g. an unknown
    # bot key) raises after the player subprocess has already spawned, the stack still
    # closes the player — no orphaned `python main.py` child left blocked on stdin.
    from contextlib import ExitStack
    with ExitStack() as stack:
        a = player.move_source("a")
        stack.callback(a.close)
        b = opponent.move_source("b")
        stack.callback(b.close)
        result = run_match(
            engine, a, b,
            player_a=player.display(), player_b=opponent.display(),
            match_id=f"local-{seed}", game=game, seed=seed, record=True,
        )
    return result


def humanize_outcome(result: dict[str, Any]) -> str:
    """Human-readable result naming the winner, from the internal a/b/forfeit token."""
    o = result.get("outcome")
    pa, pb = result.get("player_a", "A"), result.get("player_b", "B")
    if o in ("a_wins", "forfeit_b"):
        return f"{pa} wins" + (" (opponent forfeit)" if o == "forfeit_b" else "")
    if o in ("b_wins", "forfeit_a"):
        return f"{pb} wins" + (" (opponent forfeit)" if o == "forfeit_a" else "")
    return "draw"


def render_ascii(result: dict[str, Any]) -> str:
    """Render the final recorded frame as an ASCII board (trusted-outcome header)."""
    board = result.get("board", {"width": _BOARD_W, "height": _BOARD_H})
    w, h = board["width"], board["height"]
    frames = result.get("frames") or []
    grid = [["." for _ in range(w)] for _ in range(h)]
    if frames:
        last = frames[-1]
        for (x, y) in last["a"]["trail"]:
            grid[y][x] = "A"
        for (x, y) in last["b"]["trail"]:
            grid[y][x] = "B"
        ax, ay = last["a"]["pos"]
        bx, by = last["b"]["pos"]
        grid[ay][ax] = "@"
        grid[by][bx] = "#"
    turns = frames[-1]["turn"] if frames else 0
    header = (
        f"{result['player_a']} (A/@)  vs  {result['player_b']} (B/#)\n"
        f"Result: {humanize_outcome(result)}  (outcome={result['outcome']}, turns={turns})"
    )
    body = "\n".join("".join(row) for row in grid)
    return f"{header}\n{body}"


_REPLAY_TEMPLATE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ATV-bench replay — __PA__ vs __PB__</title>
<style>
  :root{--bg:oklch(21% 0.02 265);--surface:oklch(26% 0.02 265);--text:oklch(96% 0.01 265);
    --muted:oklch(72% 0.02 265);--a:oklch(78% 0.16 155);--b:oklch(75% 0.15 30);
    --line:oklch(38% 0.02 265);--mono:ui-monospace,Menlo,monospace;
    --sans:"Inter",system-ui,sans-serif;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font-family:var(--sans);
    display:flex;flex-direction:column;align-items:center;gap:1rem;padding:2rem 1rem;min-height:100vh}
  h1{font-size:1.5rem;margin:0;letter-spacing:-0.02em}
  .meta{font-family:var(--mono);color:var(--muted);font-size:.85rem}
  .players{display:flex;gap:1.5rem;font-family:var(--mono);font-size:.9rem}
  .players .a{color:var(--a)} .players .b{color:var(--b)}
  canvas{background:var(--surface);border:1px solid var(--line);border-radius:12px;
    box-shadow:0 8px 40px oklch(0% 0 0 / .4);image-rendering:pixelated;max-width:92vw;height:auto}
  .controls{display:flex;gap:.6rem;align-items:center;font-family:var(--mono);font-size:.85rem}
  button{background:var(--surface);color:var(--text);border:1px solid var(--line);
    border-radius:8px;padding:.45rem .9rem;font-family:var(--mono);cursor:pointer}
  button:hover{border-color:var(--a)}
  .outcome{font-family:var(--mono);font-size:1rem;padding:.4rem .8rem;border-radius:8px;
    background:var(--surface);border:1px solid var(--line)}
  input[type=range]{accent-color:var(--a)}
</style></head>
<body>
  <h1>🏁 ATV-bench replay</h1>
  <div class="players"><span class="a">▲ __PA__ (A)</span><span class="b">■ __PB__ (B)</span></div>
  <canvas id="c" width="500" height="500"></canvas>
  <div class="controls">
    <button id="playpause">⏸ Pause</button>
    <button id="restart">⟲ Restart</button>
    <input id="scrub" type="range" min="0" value="0"/>
    <span id="turn" class="meta">turn 0</span>
  </div>
  <div class="outcome" id="outcome"></div>
  <div class="meta">game=__GAME__ · seed=__SEED__ · match=__MATCH__</div>
<script id="match" type="application/json">__MATCH_JSON__</script>
<script>
const M = JSON.parse(document.getElementById('match').textContent);
const frames = M.frames || [];
const W = (M.board&&M.board.width)||25, H=(M.board&&M.board.height)||25;
const cv = document.getElementById('c'), ctx = cv.getContext('2d');
const cell = Math.floor(cv.width / W);
const COL_A='oklch(78% 0.16 155)', COL_B='oklch(75% 0.15 30)';
const scrub = document.getElementById('scrub');
scrub.max = Math.max(0, frames.length-1);
let i = 0, playing = true, timer = null;

function drawTrail(cells, color){
  ctx.fillStyle = color;
  for(const [x,y] of cells){ ctx.fillRect(x*cell, y*cell, cell-1, cell-1); }
}
function drawHead(pos, color){
  const [x,y]=pos; ctx.fillStyle='#fff';
  ctx.fillRect(x*cell-1, y*cell-1, cell+1, cell+1);
  ctx.fillStyle=color; ctx.fillRect(x*cell+1, y*cell+1, cell-3, cell-3);
}
function render(idx){
  ctx.clearRect(0,0,cv.width,cv.height);
  ctx.fillStyle='oklch(30% 0.02 265)';
  for(let g=0; g<=W; g++){ ctx.fillRect(g*cell,0,1,H*cell); }
  for(let g=0; g<=H; g++){ ctx.fillRect(0,g*cell,W*cell,1); }
  const f = frames[idx]; if(!f) return;
  drawTrail(f.a.trail, COL_A); drawTrail(f.b.trail, COL_B);
  drawHead(f.a.pos, COL_A); drawHead(f.b.pos, COL_B);
  document.getElementById('turn').textContent = 'turn '+f.turn;
  scrub.value = idx;
}
function step(){
  render(i);
  if(i >= frames.length-1){ playing=false; pp.textContent='▶ Play'; showOutcome(); return; }
  i++;
}
function showOutcome(){
  const o = M.outcome, pa=M.player_a, pb=M.player_b;
  let txt;
  if(o==='a_wins'||o==='forfeit_b') txt='🏆 '+pa+' wins'+(o==='forfeit_b'?' (opponent forfeit)':'');
  else if(o==='b_wins'||o==='forfeit_a') txt='🏆 '+pb+' wins'+(o==='forfeit_a'?' (opponent forfeit)':'');
  else txt='🤝 draw';
  document.getElementById('outcome').textContent = txt;
}
const pp = document.getElementById('playpause');
function tick(){ if(playing){ step(); } }
function start(){ if(timer) clearInterval(timer); timer=setInterval(tick, 90); }
pp.onclick=()=>{ playing=!playing; pp.textContent=playing?'⏸ Pause':'▶ Play';
  if(playing && i>=frames.length-1){ i=0; } };
document.getElementById('restart').onclick=()=>{ i=0; playing=true; pp.textContent='⏸ Pause';
  document.getElementById('outcome').textContent=''; };
scrub.oninput=(e)=>{ playing=false; pp.textContent='▶ Play'; i=+e.target.value; render(i); };
render(0); showOutcome(); start();
</script>
</body></html>
"""


def build_replay_html(result: dict[str, Any], out_dir: str | Path,
                      *, game: str | None = None, seed: int | None = None) -> Path:
    """Write a self-contained animated replay HTML for `result` into `out_dir`.

    The match JSON (frames + outcome + board) is embedded inline, so the page needs no
    server and no network — open it with a browser or `file://` directly.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    # The embedded match JSON is read by JSON.parse from a <script type="application/json">
    # block. A player label like `</script>...` would otherwise break out of that block, so
    # escape `<`/`>`/`&` in the serialized JSON (still valid JSON, un-parseable-breakout).
    # The visible text substitutions (__PA__ etc.) are HTML-escaped independently.
    match_json = (
        json.dumps(result, separators=(",", ":"))
        .replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    )

    def esc(v: Any) -> str:
        return _html.escape(str(v), quote=True)

    html_text = (
        _REPLAY_TEMPLATE
        .replace("__PA__", esc(result.get("player_a", "A")))
        .replace("__PB__", esc(result.get("player_b", "B")))
        .replace("__GAME__", esc(game or result.get("game", "lightcycles")))
        .replace("__SEED__", esc(seed if seed is not None else result.get("seed", 0)))
        .replace("__MATCH__", esc(result.get("match_id", "local")))
        .replace("__MATCH_JSON__", match_json)
    )
    path = out / "replay.html"
    # Evidence bytes must not depend on the host locale or newline convention.
    # Normalize the source template to canonical LF, then write UTF-8 bytes directly
    # so CP1252 Windows hosts neither reject emoji nor rewrite LF to CRLF.
    canonical_html = html_text.replace("\r\n", "\n").replace("\r", "\n")
    path.write_bytes(canonical_html.encode("utf-8"))
    return path
