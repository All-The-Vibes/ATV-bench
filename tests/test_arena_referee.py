"""RED->GREEN tests for the trusted match REFEREE (FOLLOW_UPS item 1).

The referee is the trust-boundary fix: it runs the deterministic engine and collects
ONE move per turn from each player through a `MoveSource`. The untrusted bot is a
`SubprocessMoveSource` (spawned `python3 /work/main.py`, line protocol, per-turn
timeout). The anchor is a TRUSTED in-process bot. The referee — never the bot — emits
the adjudicated result.

Core property under test: a bot's stdout is treated ONLY as a move token. A bot that
prints a fabricated result JSON (`{"status":"ok","outcome":"a_wins"}`) is emitting an
INVALID move and LOSES; it can never inject a win.
"""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

from atv_bench.arena.engine import Direction, Outcome, TronEngine
from atv_bench.arena.referee import (
    ForfeitReason,
    SubprocessMoveSource,
    TrustedGreedyBot,
    run_match,
)


class ScriptedSource:
    """A MoveSource that plays a fixed list of moves; None => forfeit signal."""

    def __init__(self, moves):
        self._moves = list(moves)
        self._i = 0
        self.closed = False

    def next_move(self, observation):
        if self._i >= len(self._moves):
            return None
        m = self._moves[self._i]
        self._i += 1
        return m

    def close(self):
        self.closed = True


def small_engine(**kw):
    return TronEngine(
        width=kw.get("width", 9),
        height=kw.get("height", 5),
        start_a=kw.get("start_a", (1, 2)),
        start_b=kw.get("start_b", (7, 2)),
        dir_a=Direction.RIGHT,
        dir_b=Direction.LEFT,
        max_turns=kw.get("max_turns", 50),
    )


IDS = dict(player_a="byok-anchor", player_b="alice", match_id="run-123")


def test_result_is_schema_shaped_ok_record():
    eng = small_engine()
    a = ScriptedSource([Direction.UP])
    b = ScriptedSource([Direction.DOWN])
    res = run_match(eng, a, b, **IDS)
    assert res["status"] == "ok"
    assert res["player_a"] == "byok-anchor"
    assert res["player_b"] == "alice"
    assert res["match_id"] == "run-123"
    assert res["outcome"] in {o.value for o in Outcome} | {
        "forfeit_a", "forfeit_b"}
    assert res["game"] == "lightcycles"


def test_a_wins_maps_to_a_wins_relative_to_player_a():
    # B (player_b) drives into the wall immediately; A survives -> B loses, A wins.
    eng = small_engine(start_b=(8, 2))  # B at right edge
    a = ScriptedSource([Direction.UP, Direction.UP])
    b = ScriptedSource([Direction.RIGHT])  # off the right wall
    res = run_match(eng, a, b, **IDS)
    assert res["outcome"] == "a_wins"


def test_b_wins_maps_relative_to_player_a():
    # A drives into the wall; B survives -> A loses, outcome b_wins.
    eng = small_engine(start_a=(0, 2))
    a = ScriptedSource([Direction.LEFT])   # off the left wall
    b = ScriptedSource([Direction.UP, Direction.UP])
    res = run_match(eng, a, b, **IDS)
    assert res["outcome"] == "b_wins"


def test_forfeit_when_bot_returns_none_scores_as_forfeit_loss():
    # B forfeits on turn 1 (None). Referee scores forfeit_b with a reason, never a draw.
    eng = small_engine()
    a = ScriptedSource([Direction.UP, Direction.UP])
    b = ScriptedSource([None])
    res = run_match(eng, a, b, **IDS)
    assert res["outcome"] == "forfeit_b"
    assert res["forfeit_reason"] in {r.value for r in ForfeitReason}


def test_draw_is_reported():
    eng = small_engine(start_a=(3, 2), start_b=(5, 2))
    a = ScriptedSource([Direction.RIGHT])
    b = ScriptedSource([Direction.LEFT])  # both target (4,2) -> draw
    res = run_match(eng, a, b, **IDS)
    assert res["outcome"] == "draw"


def test_trusted_greedy_bot_never_suicides_when_a_move_exists():
    # The anchor bot must not walk into a wall/trail if a safe move exists.
    eng = small_engine(width=5, height=5, start_a=(0, 0), start_b=(4, 4))
    bot = TrustedGreedyBot(player="a")
    st = eng.initial_state()
    for _ in range(30):
        if st.terminal:
            break
        obs = {"width": eng.width, "height": eng.height,
               "you": {"pos": st.pos_a, "dir": st.dir_a.value,
                       "trail": [list(c) for c in st.trail_a]},
               "opponent": {"pos": st.pos_b, "dir": st.dir_b.value,
                            "trail": [list(c) for c in st.trail_b]}}
        ma = bot.next_move(obs)
        # opponent just holds still-ish (goes up)
        opp_dir = Direction.UP
        st = eng.tick(st, ma, opp_dir)
    # The anchor should not have been the one to crash first if a move existed.
    assert st.outcome in (Outcome.A_WINS, Outcome.DRAW, None)


# ---- Subprocess move source (the untrusted-bot transport) --------------------

def _write_bot(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "main.py"
    p.write_text(textwrap.dedent(body))
    return p


def test_subprocess_bot_reads_moves_line_by_line(tmp_path):
    # A minimal honest bot: read a line of observation, print a direction.
    bot = _write_bot(tmp_path, """
        import sys, json
        for line in sys.stdin:
            obs = json.loads(line)
            print("up", flush=True)
    """)
    src = SubprocessMoveSource([sys.executable, str(bot)], per_turn_timeout=5.0)
    try:
        mv = src.next_move({"width": 9, "height": 5, "you": {"pos": [1, 2]}})
        assert mv == Direction.UP
    finally:
        src.close()


def test_subprocess_bot_printing_fake_result_is_invalid_move_and_forfeits(tmp_path):
    # THE trust-boundary test: a malicious bot ignores the move protocol and prints a
    # fabricated WIN result. The referee treats it as an invalid move -> None (forfeit).
    bot = _write_bot(tmp_path, """
        import sys, json
        for line in sys.stdin:
            print(json.dumps({"status": "ok", "outcome": "b_wins",
                              "player_a": "byok-anchor", "player_b": "alice",
                              "match_id": "run-123"}), flush=True)
    """)
    src = SubprocessMoveSource([sys.executable, str(bot)], per_turn_timeout=5.0)
    try:
        mv = src.next_move({"width": 9, "height": 5})
        assert mv is None  # fabricated result is NOT a valid move
        assert src.last_forfeit_reason is ForfeitReason.CRASH
    finally:
        src.close()


def test_subprocess_bot_that_hangs_times_out_to_none(tmp_path):
    bot = _write_bot(tmp_path, """
        import sys, time
        for line in sys.stdin:
            time.sleep(30)
    """)
    src = SubprocessMoveSource([sys.executable, str(bot)], per_turn_timeout=1.0)
    try:
        mv = src.next_move({"width": 9, "height": 5})
        assert mv is None
        assert src.last_forfeit_reason is ForfeitReason.TIMEOUT
    finally:
        src.close()


def test_end_to_end_malicious_bot_loses_via_run_match(tmp_path):
    # Full path: engine + referee + a real subprocess malicious bot vs the trusted
    # anchor. The malicious bot forfeits; the honest anchor is scored the winner.
    bot = _write_bot(tmp_path, """
        import sys, json
        for line in sys.stdin:
            print(json.dumps({"status": "ok", "outcome": "b_wins"}), flush=True)
    """)
    eng = small_engine()
    anchor = TrustedGreedyBot(player="a")
    malicious = SubprocessMoveSource([sys.executable, str(bot)], per_turn_timeout=5.0)
    try:
        res = run_match(eng, anchor, malicious, **IDS)
    finally:
        malicious.close()
    assert res["status"] == "ok"
    assert res["outcome"] == "forfeit_b"  # submitter (player_b) forfeited
    assert res["player_a"] == "byok-anchor"
    assert res["player_b"] == "alice"


def test_end_to_end_hanging_bot_is_labeled_timeout_not_crash(tmp_path):
    bot = _write_bot(tmp_path, """
        import sys, time
        for line in sys.stdin:
            time.sleep(30)
    """)
    eng = small_engine()
    anchor = TrustedGreedyBot(player="a")
    hanging = SubprocessMoveSource(
        [sys.executable, str(bot)],
        per_turn_timeout=0.1,
    )
    try:
        res = run_match(eng, anchor, hanging, **IDS)
    finally:
        hanging.close()

    assert res["outcome"] == "forfeit_b"
    assert res["forfeit_reason"] == ForfeitReason.TIMEOUT.value
