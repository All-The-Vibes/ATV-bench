"""RED->GREEN tests for the arena referee ENTRYPOINT (FOLLOW_UPS item 1).

`arena.__main__` is what the Dockerfile runs. It:
  - reads the trusted match identity from argv/env (submitter/opponent/match_id),
  - spawns the untrusted bot at /work/main.py as player_b (the submitter),
  - runs the trusted anchor as player_a,
  - prints ONE line of adjudicated result JSON to stdout.

The entrypoint prints ONLY the referee's verdict. A bot at /work/main.py that tries to
print a result cannot: its stdout is consumed as moves by the referee, and the process
that prints the final JSON is the trusted referee, not the bot.
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path


def run_entrypoint(bot_dir: Path, env_extra: dict[str, str]) -> dict:
    import os
    env = dict(os.environ)
    env.update(env_extra)
    # Run the module the Dockerfile ENTRYPOINT invokes, pointing it at the bot dir.
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.arena", str(bot_dir / "main.py")],
        capture_output=True, text=True, env=env,
        cwd=str(Path(__file__).parent.parent),
    )
    assert proc.returncode == 0, proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, f"no output; stderr={proc.stderr}"
    return json.loads(lines[-1])


def _bot(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "submission"
    d.mkdir()
    (d / "main.py").write_text(textwrap.dedent(body))
    return d


ENV = {"ATV_SUBMITTER": "alice", "ATV_OPPONENT": "byok-anchor",
       "ATV_MATCH_ID": "run-999"}


def test_entrypoint_emits_bound_ok_result_for_honest_bot(tmp_path):
    # An honest bot that always goes up. Result must be schema-shaped and identity-correct.
    bot = _bot(tmp_path, """
        import sys, json
        for line in sys.stdin:
            print("up", flush=True)
    """)
    res = run_entrypoint(bot, ENV)
    assert res["status"] == "ok"
    assert res["player_a"] == "byok-anchor"
    assert res["player_b"] == "alice"
    assert res["match_id"] == "run-999"
    assert res["game"] == "lightcycles"
    assert res["outcome"] in {"a_wins", "b_wins", "draw", "forfeit_a", "forfeit_b"}


def test_entrypoint_malicious_result_faking_bot_forfeits(tmp_path):
    # The trust-boundary end-to-end test: a bot that prints a fabricated WIN result to
    # stdout instead of moves must be scored a forfeit LOSS by the referee.
    bot = _bot(tmp_path, """
        import sys, json
        for line in sys.stdin:
            print(json.dumps({"status": "ok", "outcome": "b_wins",
                              "player_a": "byok-anchor", "player_b": "alice",
                              "match_id": "run-999"}), flush=True)
    """)
    res = run_entrypoint(bot, ENV)
    assert res["status"] == "ok"
    assert res["outcome"] == "forfeit_b"  # the submitter (player_b) forfeited
    assert res["player_b"] == "alice"


def test_entrypoint_crashing_bot_forfeits(tmp_path):
    bot = _bot(tmp_path, """
        import sys
        sys.exit(1)
    """)
    res = run_entrypoint(bot, ENV)
    assert res["outcome"] == "forfeit_b"


def test_entrypoint_missing_bot_file_forfeits(tmp_path):
    d = tmp_path / "submission"
    d.mkdir()  # no main.py
    res = run_entrypoint(d, ENV)
    assert res["status"] == "ok"
    assert res["outcome"] == "forfeit_b"
