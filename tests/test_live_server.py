"""Tests for the browser SSE live-match server (demo Act 2 + Act 3 in the browser).

`serve_live_match` streams a real two-bot Tron match to the browser over Server-Sent
Events: per-turn `data:` frames, a terminal `result` event, then a `board` event with the
leaderboard rows + insights. These tests drive it with a stdlib HTTP client (no browser,
no Playwright) so they are fully hermetic.
"""
from __future__ import annotations

import json
import threading
import urllib.request
from pathlib import Path

import pytest

from atv_bench.arena.live_server import LiveMatchServer


def _sample_bots() -> tuple[str, str]:
    from atv_bench.arena import sample_bots
    base = Path(sample_bots.__file__).parent
    return str(base / "greedy_survivor.py"), str(base / "wall_hugger.py")


@pytest.fixture
def server():
    a, b = _sample_bots()
    srv = LiveMatchServer(
        a_bot=a, b_bot=b, a_name="ATV-StarterKit", b_name="ATV-Phoenix",
        seed=0, turn_delay=0.0,   # no sleeps in tests
    )
    srv.start()
    try:
        yield srv
    finally:
        srv.stop()


def _get(url: str) -> tuple[int, str, str]:
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode()


def test_index_serves_html(server):
    status, ctype, body = _get(server.url + "/")
    assert status == 200
    assert "text/html" in ctype
    assert "EventSource" in body            # the page wires up the SSE client
    assert "canvas" in body.lower()          # Act 2 arena canvas


def test_events_streams_frames_result_and_board(server):
    status, ctype, body = _get(server.url + "/events")
    assert status == 200
    assert "text/event-stream" in ctype

    # Parse SSE: collect (event, data) pairs.
    events = []
    cur_event = "message"
    for line in body.splitlines():
        if line.startswith("event:"):
            cur_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            events.append((cur_event, line.split(":", 1)[1].strip()))
            cur_event = "message"

    frames = [json.loads(d) for (e, d) in events if e == "message"]
    results = [json.loads(d) for (e, d) in events if e == "result"]
    boards = [json.loads(d) for (e, d) in events if e == "board"]

    # Act 2: many per-turn frames, strictly increasing turn numbers.
    assert len(frames) >= 2
    turns = [f["turn"] for f in frames]
    assert turns == sorted(turns)
    assert frames[0]["turn"] == 0
    assert frames[-1]["terminal"] is True

    # Terminal result: a decisive outcome for these two distinct bots.
    assert len(results) == 1
    assert results[0]["outcome"] in ("a_wins", "b_wins", "draw")

    # Act 3: a board event listing BOTH live players with their ELO + insights.
    assert len(boards) == 1
    board = boards[0]
    identities = {row["identity"] for row in board["rows"]}
    assert "ATV-StarterKit" in identities and "ATV-Phoenix" in identities
    assert isinstance(board["insights"], list) and board["insights"]


def test_decisive_match_gives_distinct_elo(server):
    _, _, body = _get(server.url + "/events")
    board = None
    cur = "message"
    for line in body.splitlines():
        if line.startswith("event:"):
            cur = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and cur == "board":
            board = json.loads(line.split(":", 1)[1].strip())
            cur = "message"
    assert board is not None
    elo = {r["identity"]: round(float(r["elo"])) for r in board["rows"]
           if r["identity"] in ("ATV-StarterKit", "ATV-Phoenix")}
    assert set(elo) == {"ATV-StarterKit", "ATV-Phoenix"}
    assert elo["ATV-StarterKit"] != elo["ATV-Phoenix"]   # real head-to-head, not a draw
