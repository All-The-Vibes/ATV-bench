/* Shared canvas shell for ATV-bench viewers (T4-canvas).
 *
 * ONE source of the canvas + playback machinery, consumed by BOTH:
 *   - play.py's _REPLAY_TEMPLATE (inlined at build time -> self-contained replay.html)
 *   - view/live.html (loaded as a sibling <script src>)
 * There is no third copy of the draw loop. Per-game draw fns are the only variation:
 * grid+trails for lightcycles, grid+ants for ants, board+pieces for chess.
 *
 * All functions are pure w.r.t. the DOM except they paint into a passed 2d context.
 * Colors are passed in (resolved from CSS tokens by the caller) so the shell never
 * reaches for --a/--b itself.
 */
(function (global) {
  "use strict";

  // --- grid games (lightcycles, ants) ------------------------------------
  function fitGrid(canvas, W, H, maxPx) {
    const target = Math.min(maxPx || 600, W * 24, H * 24, 600);
    const cell = Math.max(4, Math.floor(target / Math.max(W, H)));
    canvas.width = W * cell;
    canvas.height = H * cell;
    return cell;
  }

  function paintGridBg(ctx, W, H, cell, colors) {
    ctx.fillStyle = colors.surface;
    ctx.fillRect(0, 0, W * cell, H * cell);
    ctx.strokeStyle = colors.line;
    ctx.globalAlpha = 0.22;
    ctx.lineWidth = 1;
    for (let x = 0; x <= W; x++) {
      ctx.beginPath();
      ctx.moveTo(x * cell, 0);
      ctx.lineTo(x * cell, H * cell);
      ctx.stroke();
    }
    for (let y = 0; y <= H; y++) {
      ctx.beginPath();
      ctx.moveTo(0, y * cell);
      ctx.lineTo(W * cell, y * cell);
      ctx.stroke();
    }
    ctx.globalAlpha = 1;
  }

  function paintObstacles(ctx, obstacles, cell, colors) {
    if (!obstacles || !obstacles.length) return;
    ctx.fillStyle = colors.line;
    ctx.globalAlpha = 0.85;
    for (const o of obstacles) ctx.fillRect(o[0] * cell, o[1] * cell, cell, cell);
    ctx.globalAlpha = 1;
  }

  function cellBlock(ctx, x, y, cell, color, alpha) {
    ctx.fillStyle = color;
    ctx.globalAlpha = alpha == null ? 1 : alpha;
    ctx.fillRect(x * cell + 1, y * cell + 1, cell - 2, cell - 2);
    ctx.globalAlpha = 1;
  }

  function head(ctx, pos, cell, color) {
    ctx.shadowColor = color;
    ctx.shadowBlur = 12;
    ctx.fillStyle = color;
    ctx.fillRect(pos[0] * cell + 1, pos[1] * cell + 1, cell - 2, cell - 2);
    ctx.shadowBlur = 0;
  }

  // Normalized lightcycles frame: {trails:[cellsA,cellsB], heads:[posA,posB]}
  function drawLightcycles(canvas, frame, meta) {
    const { W, H, colors, obstacles } = meta;
    const cell = canvas.__cell || (canvas.__cell = fitGrid(canvas, W, H, meta.maxPx));
    const ctx = canvas.getContext("2d");
    paintGridBg(ctx, W, H, cell, colors);
    paintObstacles(ctx, obstacles, cell, colors);
    const seat = [colors.a, colors.b];
    (frame.trails || []).forEach((cells, i) => {
      for (const c of cells) cellBlock(ctx, c[0], c[1], cell, seat[i] || colors.a, 0.5);
    });
    (frame.heads || []).forEach((pos, i) => {
      if (pos) head(ctx, pos, cell, seat[i] || colors.a);
    });
  }

  // Normalized ants frame: {ants:[[x,y,p]], hills:[[x,y,p]], food:[[x,y]]}
  function drawAnts(canvas, frame, meta) {
    const { W, H, colors, obstacles } = meta;
    const cell = canvas.__cell || (canvas.__cell = fitGrid(canvas, W, H, meta.maxPx));
    const ctx = canvas.getContext("2d");
    paintGridBg(ctx, W, H, cell, colors);
    paintObstacles(ctx, obstacles, cell, colors);
    const seat = [colors.a, colors.b];
    for (const f of frame.food || []) cellBlock(ctx, f[0], f[1], cell, colors.food, 0.9);
    for (const h of frame.hills || []) {
      ctx.strokeStyle = seat[h[2]] || colors.a;
      ctx.globalAlpha = 0.9;
      ctx.lineWidth = 2;
      ctx.strokeRect(h[0] * cell + 1.5, h[1] * cell + 1.5, cell - 3, cell - 3);
      ctx.globalAlpha = 1;
    }
    for (const a of frame.ants || []) head(ctx, [a[0], a[1]], cell, seat[a[2]] || colors.a);
  }

  // --- chess -------------------------------------------------------------
  const GLYPH = {
    K: "♔", Q: "♕", R: "♖", B: "♗", N: "♘", P: "♙",
    k: "♚", q: "♛", r: "♜", b: "♝", n: "♞", p: "♟",
  };

  function fenToBoard(fen) {
    const rows = fen.split(" ")[0].split("/");
    const grid = [];
    for (const row of rows) {
      const line = [];
      for (const ch of row) {
        if (/\d/.test(ch)) for (let i = 0; i < +ch; i++) line.push(null);
        else line.push(ch);
      }
      grid.push(line);
    }
    return grid;
  }

  // Normalized chess frame: {fen}
  function drawChess(canvas, frame, meta) {
    const colors = meta.colors;
    const size = Math.min(meta.maxPx || 480, 480);
    if (canvas.width !== size) {
      canvas.width = size;
      canvas.height = size;
    }
    const ctx = canvas.getContext("2d");
    const sq = size / 8;
    const grid = fenToBoard(frame.fen || "8/8/8/8/8/8/8/8");
    for (let r = 0; r < 8; r++) {
      for (let c = 0; c < 8; c++) {
        ctx.fillStyle = (r + c) % 2 === 0 ? colors.light : colors.dark;
        ctx.fillRect(c * sq, r * sq, sq, sq);
        const piece = grid[r] && grid[r][c];
        if (piece) {
          const white = piece === piece.toUpperCase();
          ctx.fillStyle = white ? colors.a : colors.b;
          ctx.font = `${Math.floor(sq * 0.78)}px serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";
          ctx.fillText(GLYPH[piece] || "?", c * sq + sq / 2, r * sq + sq * 0.56);
        }
      }
    }
  }

  const DRAW = {
    lightcycles: drawLightcycles,
    ants: drawAnts,
    chess: drawChess,
  };

  // --- playback controller ----------------------------------------------
  // Generic play/pause/scrub/restart over a frame array, calling a draw fn each tick.
  function Playback(opts) {
    const canvas = opts.canvas;
    const frames = opts.frames || [];
    const draw = opts.draw;
    const meta = opts.meta || {};
    const onTurn = opts.onTurn || function () {};
    const scrub = opts.scrub;
    const interval = opts.interval || 90;
    let i = 0;
    let playing = frames.length > 1;
    let timer = null;

    if (scrub) scrub.max = Math.max(0, frames.length - 1);

    function render(idx) {
      const f = frames[idx];
      if (!f) return;
      draw(canvas, f, meta);
      if (scrub) scrub.value = idx;
      onTurn(f.turn != null ? f.turn : f.t != null ? f.t : idx, idx, frames.length);
    }
    function tick() {
      if (!playing) return;
      render(i);
      if (i >= frames.length - 1) {
        playing = false;
        if (opts.onEnd) opts.onEnd();
        return;
      }
      i++;
    }
    function start() {
      if (timer) clearInterval(timer);
      render(i);
      timer = setInterval(tick, interval);
    }
    function stop() {
      // Tear down the interval so switching rounds never stacks timers. Idempotent:
      // a stopped playback holds no timer, so a double stop() is a no-op.
      if (timer) {
        clearInterval(timer);
        timer = null;
      }
      playing = false;
    }
    return {
      start: start,
      stop: stop,
      destroy: stop,
      render: render,
      seek: function (idx) {
        playing = false;
        i = idx;
        render(idx);
      },
      toggle: function () {
        playing = !playing;
        if (playing && i >= frames.length - 1) i = 0;
        return playing;
      },
      restart: function () {
        i = 0;
        playing = true;
      },
      isPlaying: function () {
        return playing;
      },
      count: frames.length,
    };
  }

  global.ATVShell = {
    DRAW: DRAW,
    drawLightcycles: drawLightcycles,
    drawAnts: drawAnts,
    drawChess: drawChess,
    Playback: Playback,
    fenToBoard: fenToBoard,
  };
})(typeof window !== "undefined" ? window : this);
