"""Arena referee ENTRYPOINT — what the Dockerfile runs (FOLLOW_UPS item 1).

    ENTRYPOINT ["python3", "-m", "atv_bench.arena"]

Invoked as `python3 -m atv_bench.arena /work/main.py`. This process is the TRUSTED
referee. It:

  - reads the trusted match identity from env (ATV_SUBMITTER / ATV_OPPONENT /
    ATV_MATCH_ID) — the same context the workflow already exports; never bot stdout,
  - spawns the untrusted bot (`python3 /work/main.py`) as player_b (the submitter),
    inside the already-locked-down container,
  - plays it against the trusted in-process anchor (player_a),
  - prints exactly ONE line of adjudicated result JSON to stdout.

The bot's stdout is consumed by the referee as move tokens; the JSON printed here is
authored by the referee from the engine verdict. A bot cannot print the result — the
last line on stdout is always the trusted one.

Fail-closed: a missing bot file, a spawn error, or a bot that never makes a legal move
is scored as a submitter forfeit (never a crash of this process, never a dropped match).
The board size / seed are fixed here (trusted match parameters), not bot-supplied.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from atv_bench.arena.engine import Direction, TronEngine
from atv_bench.arena.referee import (
    ForfeitReason,
    SubprocessMoveSource,
    TrustedGreedyBot,
    run_match,
)

# Trusted, fixed match parameters. A wide board + turn cap gives a real game while
# staying well inside the container's cpu/time caps. These are match rules, not bot input.
BOARD_W = 25
BOARD_H = 25
MAX_TURNS = 400
PER_TURN_TIMEOUT = 2.0
SEED = 0


def _ids() -> tuple[str, str, str]:
    submitter = (os.environ.get("ATV_SUBMITTER") or "submitter").strip() or "submitter"
    opponent = (os.environ.get("ATV_OPPONENT") or "byok-anchor").strip() or "byok-anchor"
    match_id = (os.environ.get("ATV_MATCH_ID") or "local").strip() or "local"
    return submitter, opponent, match_id


def _forfeit(player_a: str, player_b: str, match_id: str,
             reason: ForfeitReason = ForfeitReason.CRASH) -> dict:
    return {
        "status": "ok",
        "player_a": player_a,
        "player_b": player_b,
        "outcome": "forfeit_b",  # the submitter (player_b) forfeited
        "forfeit_reason": reason.value,
        "match_id": match_id,
        "game": "lightcycles",
        "seed": SEED,
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bot_path = argv[0] if argv else "/work/main.py"
    submitter, opponent, match_id = _ids()

    # Anchor is player_a; the untrusted submitter bot is player_b. Opposite corners,
    # heading toward each other, so a passive/greedy match still produces a real game.
    engine = TronEngine(
        width=BOARD_W, height=BOARD_H,
        start_a=(1, BOARD_H // 2), start_b=(BOARD_W - 2, BOARD_H // 2),
        dir_a=Direction.RIGHT, dir_b=Direction.LEFT, max_turns=MAX_TURNS,
    )

    if not Path(bot_path).is_file():
        print(json.dumps(_forfeit(opponent, submitter, match_id)), flush=True)
        return 0

    anchor = TrustedGreedyBot(player="a")
    try:
        bot = SubprocessMoveSource([sys.executable, bot_path],
                                   per_turn_timeout=PER_TURN_TIMEOUT)
    except Exception:
        print(json.dumps(_forfeit(opponent, submitter, match_id)), flush=True)
        return 0

    try:
        result = run_match(
            engine, anchor, bot,
            player_a=opponent, player_b=submitter, match_id=match_id,
            game="lightcycles", seed=SEED,
        )
    except Exception:
        result = _forfeit(opponent, submitter, match_id)
    finally:
        bot.close()

    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
