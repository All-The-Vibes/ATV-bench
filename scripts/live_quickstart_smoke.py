#!/usr/bin/env python
"""Live end-to-end smoke of `atv-bench quickstart` against the REAL CLIs.

Runs a genuine quickstart evaluation (real Docker arena + real harness CLI + bare control) for a
chosen harness/model over a small game set, and prints/persists the result — the human-runnable
proof that the live seam works end to end. NOT part of the hermetic suite.

Usage:
    python scripts/live_quickstart_smoke.py [--harness claude-code] [--model sonnet] \
        [--game dummy --game lightcycles] [--repeats 3] [--store DIR]

Requires: the harness CLI on PATH + auth, Docker, and the vendored CodeClash (`.[run]`).
Exits non-zero if no match scored (a real failure), 0 on a scored eval.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _preflight(harness: str) -> list[str]:
    problems = []
    binary = {"claude-code": "claude", "copilot-cli": "copilot", "codex": "codex"}.get(harness, harness)
    if not shutil.which(binary):
        problems.append(f"{binary} CLI not on PATH")
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode:
        problems.append("docker not available")
    try:
        import codeclash  # noqa: F401
    except Exception:
        problems.append("vendored CodeClash not importable (pip install -e '.[run]')")
    return problems


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--harness", default="claude-code")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--game", action="append", dest="games")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--store", default="./live-quickstart-smoke")
    args = ap.parse_args(argv)

    problems = _preflight(args.harness)
    if problems:
        print("PRECONDITIONS NOT MET:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 3

    from atv_bench.games import live_keys
    from atv_bench.quickstart import live_match_executor, run_quickstart_eval

    live = set(live_keys())
    games = args.games or [g for g in ("dummy", "lightcycles") if g in live] or [next(iter(live))]
    bad = [g for g in games if g not in live]
    if bad:
        print(f"not live: {bad}; live={sorted(live)}", file=sys.stderr)
        return 2

    print(f"▶ live quickstart smoke: {args.harness} vs bare:{args.harness} on {args.model} "
          f"over {games} × {args.repeats} = {len(games)*args.repeats} real matches\n", flush=True)

    def progress(ev):
        if ev.get("phase") == "match":
            print(f"  [{ev['index']+1}/{ev['total']}] {ev['game']}: {ev['harness_a']} vs {ev['harness_b']} …", flush=True)
        elif ev.get("phase") == "match_failed":
            print(f"      ✗ {ev['game']} failed: {ev.get('error','')[:100]}", flush=True)

    executor = live_match_executor(rounds=args.rounds)
    res = run_quickstart_eval(
        harness=args.harness, model=args.model, games=games, repeats=args.repeats,
        store=Path(args.store), execute=executor, progress=progress,
    )

    print("\n=== RESULT ===", flush=True)
    print(json.dumps(res.to_dict(), indent=2))
    print(f"\nScorecard: {res.board_url}", flush=True)
    if res.n_matches == 0:
        print("NO MATCH SCORED — live seam failed.", file=sys.stderr)
        return 1
    print(f"✓ {res.n_matches} matches scored; overall lift="
          f"{None if res.overall is None else round(res.overall.lift, 3)}; "
          f"credible={res.credible}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
