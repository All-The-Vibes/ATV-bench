"""Browser SSE live-match server — the demo's Act 2 (live Tron feed) + Act 3 (board).

Streams a real two-bot Tron match to the browser over Server-Sent Events (SSE): one
`data:` frame per turn as the match plays, then a terminal `result` event, then a `board`
event carrying the leaderboard rows + insights. Stdlib only (`http.server`) — no web
framework, no websocket dependency; a one-way server→browser feed is exactly SSE's shape.

Reuses the existing engine/referee/board machinery:
  - `run_match(engine, source_a, source_b, observer=)` streams each `GameState`.
  - `frame_to_dict(...)` turns a state into a JSON frame the browser canvas draws.
  - the throwaway-store → `build_site` → `build_insights` path builds Act 3 (identical to
    the terminal demo's board), so the browser shows the SAME honest, decisive result.
"""
from __future__ import annotations

import functools
import http.server
import json
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

from atv_bench.arena.engine import Direction, TronEngine
from atv_bench.arena.referee import SubprocessMoveSource, run_match
from atv_bench.arena.render import frame_to_dict

# Board geometry mirrors the terminal demo (asymmetric starts so the match is decisive,
# not a mirrored draw — see the PR that fixed the flat-line board).
_BOARD_W = _BOARD_H = 25


def _live_html() -> str:
    """The bundled live page (view/live.html), read from the installed package."""
    here = Path(__file__).resolve().parent.parent  # .../atv_bench
    return (here / "view" / "live.html").read_text(encoding="utf-8")


def _build_board(a_name: str, b_name: str, result: dict, a_label: str, b_label: str) -> dict:
    """Record the just-played match into a throwaway store and build Act 3.

    Returns {"rows": [...], "insights": [...]} — the same rows the static board shows,
    plus the heuristic insight lines. The winner is whatever the match adjudicated; the
    fingerprint labels are harness-neutral sample-bot metadata (never the adjudicator).
    """
    import shutil
    import tempfile
    from datetime import datetime, timezone

    from atv_bench.demo import build_demo_store
    from atv_bench.publish import build_site
    from atv_bench.leaderboard import build_insights
    # Reuse the CLI's honest recorder so browser + terminal Act 3 stay identical.
    from atv_bench.cli import _record_demo_match

    tmp_store = Path(tempfile.mkdtemp(prefix="atv-live-store-"))
    out_dir = Path(tempfile.mkdtemp(prefix="atv-live-board-"))
    try:
        build_demo_store(str(tmp_store))
        _record_demo_match(str(tmp_store), result, a_name, b_name, a_label, b_label)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        site = build_site(str(out_dir), store_dir=str(tmp_store), updated_at=now)
        doc = json.loads((site / "leaderboard.json").read_text())
        rows = doc.get("rows", [])
        return {"rows": rows, "insights": build_insights(rows)}
    finally:
        shutil.rmtree(tmp_store, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)


class LiveMatchServer:
    """Serve one browser-streamed demo match on an ephemeral localhost port.

    `start()` binds and serves in a background thread; `url` is the base address. Each
    GET /events runs a fresh match and streams it (so a browser reload replays the demo).
    `stop()` shuts the server down. `turn_delay` throttles the feed so it's watchable in
    a browser; tests pass 0.0 for no sleeps.
    """

    def __init__(self, *, a_bot: str, b_bot: str, a_name: str, b_name: str,
                 seed: int = 0, turn_delay: float = 0.12) -> None:
        self.a_bot = a_bot
        self.b_bot = b_bot
        self.a_name = a_name
        self.b_name = b_name
        self.a_label = Path(a_bot).stem
        self.b_label = Path(b_bot).stem
        self.seed = seed
        self.turn_delay = turn_delay
        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- match streaming -------------------------------------------------------

    def _engine(self) -> TronEngine:
        return TronEngine(
            width=_BOARD_W, height=_BOARD_H,
            start_a=(1, 1), start_b=(_BOARD_W - 2, _BOARD_H - 5),
            dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=400,
        )

    def _run_and_stream(self, write) -> None:
        """Run a match, calling write(event, data_dict) for each SSE event."""
        engine = self._engine()
        source_a = SubprocessMoveSource([sys.executable, self.a_bot], per_turn_timeout=2.0)
        source_b = SubprocessMoveSource([sys.executable, self.b_bot], per_turn_timeout=2.0)

        def _observe(state: Any) -> None:
            write("message", frame_to_dict(state, engine,
                                           label_a=self.a_name, label_b=self.b_name))
            if self.turn_delay:
                time.sleep(self.turn_delay)

        try:
            result = run_match(
                engine, source_a, source_b,
                player_a=self.a_name, player_b=self.b_name, match_id="demo-live",
                game="lightcycles", seed=self.seed, observer=_observe,
            )
        finally:
            source_a.close()
            source_b.close()

        write("result", {"outcome": result.get("outcome"),
                         "player_a": self.a_name, "player_b": self.b_name})
        board = _build_board(self.a_name, self.b_name, result, self.a_label, self.b_label)
        write("board", board)

    # -- HTTP ------------------------------------------------------------------

    def _handler(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            # Quiet the default stderr request logging.
            def log_message(self, *args):  # noqa: D401
                return

            def do_GET(self):  # noqa: N802
                if self.path in ("/", "/index.html"):
                    self._serve_html()
                elif self.path.startswith("/events"):
                    self._serve_events()
                else:
                    self.send_error(404)

            def _serve_html(self):
                body = _live_html().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _serve_events(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                def write(event: str, data: dict) -> None:
                    chunk = ""
                    if event and event != "message":
                        chunk += f"event: {event}\n"
                    chunk += f"data: {json.dumps(data)}\n\n"
                    try:
                        self.wfile.write(chunk.encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        raise

                try:
                    server._run_and_stream(write)
                except (BrokenPipeError, ConnectionResetError):
                    return  # browser navigated away mid-stream — fine.

        return Handler

    def start(self) -> None:
        handler = self._handler()
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server not started"
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


def serve_live_match(*, a_bot: str, b_bot: str, a_name: str, b_name: str,
                     seed: int = 0, turn_delay: float = 0.12,
                     open_browser: bool = True, echo=print) -> LiveMatchServer:
    """Start the live server, optionally open a browser, and block until interrupted.

    Returns the running server (already serving). With open_browser=True this launches
    the system browser at the match page and blocks (Ctrl-C to stop) — the demo surface.
    With open_browser=False it starts, prints the URL, and returns without blocking (for
    headless/tests).
    """
    srv = LiveMatchServer(a_bot=a_bot, b_bot=b_bot, a_name=a_name, b_name=b_name,
                          seed=seed, turn_delay=turn_delay)
    srv.start()
    echo(f"  Live match: {srv.url}/  (Ctrl-C to stop)")
    if not open_browser:
        return srv
    import webbrowser
    try:
        webbrowser.open(f"{srv.url}/")
    except Exception:
        pass
    try:
        # Block the foreground until the user stops it.
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        echo("\nStopped.")
        srv.stop()
    return srv
