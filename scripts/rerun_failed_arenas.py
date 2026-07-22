#!/usr/bin/env python
"""Isolated sequential re-run of arenas that FAILED in the batch matrix.

The batch `e2e_arena_matrix.py --all` degrades Docker under sustained load (RWLayer-nil
races, OOM exit 137, transient "No such container"), so a FAIL there is not a verdict. This
driver re-runs each requested arena ONE AT A TIME, with an aggressive docker prune + settle
between arenas, and writes the true verdict to `_e2e/rerun/<arena>/verdict.json` (which
scripts/consolidate_wave_c_proof.py treats as the authoritative override).

Usage: python scripts/rerun_failed_arenas.py <arena> [<arena> ...]
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
E2E = ROOT / "_e2e"
RERUN = E2E / "rerun"


def _docker_settle() -> None:
    """Remove leftover match containers + prune, then pause so the daemon settles."""
    for cmd in (
        ["docker", "ps", "-aq", "--filter", "name=minisweagent-"],
    ):
        try:
            ids = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.split()
            if ids:
                subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120)
        except Exception:
            pass
    try:
        subprocess.run(["docker", "container", "prune", "-f"], capture_output=True, timeout=60)
    except Exception:
        pass
    time.sleep(8)  # let memory/storage settle before the next heavy build


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: rerun_failed_arenas.py <arena> [<arena> ...]", file=sys.stderr)
        return 2
    RERUN.mkdir(parents=True, exist_ok=True)
    from atv_bench.integration import register, unregister  # noqa: F401  (import smoke)

    # We reuse the batch runner's run_arena, but redirect its output dir by running each
    # arena in a subprocess with a per-arena _e2e override via cwd isolation is overkill —
    # instead call run_arena and then copy its verdict into _e2e/rerun/<arena>/.
    sys.path.insert(0, str(ROOT / "scripts"))
    import e2e_arena_matrix as m  # type: ignore

    results = []
    for arena in argv:
        if arena not in m.ARENAS:
            print(f"SKIP unknown arena {arena}", flush=True)
            continue
        _docker_settle()
        print(f"▶ RERUN {arena} ({m.ARENAS[arena]['players']}p) …", flush=True)
        v = m.run_arena(arena)
        # persist the authoritative override
        dest = RERUN / arena
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "verdict.json").write_text(json.dumps(v, indent=2))
        results.append(v)
        print(f"  {'PASS' if v['passed'] else 'FAIL'} ({v['seconds']}s): {v['why'][:120]}", flush=True)

    passed = [v["arena"] for v in results if v["passed"]]
    print(f"\n=== RERUN {len(passed)}/{len(results)} PASSED: {passed}", flush=True)
    (RERUN / "rerun_summary.json").write_text(json.dumps(results, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
