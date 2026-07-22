"""Consolidate the live 22-arena e2e verdicts into a committed Wave C proof artifact and
reconcile them against the `live=True` flags in games.py (PR #19 follow-up 4).

Reads every `_e2e/<arena>/verdict.json` (written by scripts/e2e_arena_matrix.py), plus an
optional `_e2e/rerun/<arena>/verdict.json` override (an isolated re-run of an arena that hit
a transient Docker container-eviction error in the batch), and writes:

  docs/proof/wave-c/matrix.json   — {arena: {passed, seconds, why}} for every arena run.

It then prints a reconciliation table: for each arena flagged `live=True` in games.py, whether
the committed proof shows a PASS. Arenas that are `live=True` but did NOT pass (and are not in
the known upstream-blocked set) are printed as DRIFT — the caller downgrades them.

Usage: python scripts/consolidate_wave_c_proof.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The e2e verdicts are written by the batch/re-run drivers into the checkout that ran them,
# which may differ from this worktree. Allow an override so the proof can be consolidated
# from wherever the live matrix actually ran.
import os

E2E = Path(os.environ.get("ATV_E2E_DIR", ROOT / "_e2e"))
OUT = ROOT / "docs" / "proof" / "wave-c"


def _load_verdicts() -> dict[str, dict]:
    """Merge batch verdicts with isolated re-run overrides (re-run wins)."""
    verdicts: dict[str, dict] = {}
    for vf in sorted(E2E.glob("*/verdict.json")):
        try:
            v = json.loads(vf.read_text())
            verdicts[v["arena"]] = v
        except Exception:
            continue
    rerun = E2E / "rerun"
    if rerun.is_dir():
        for vf in sorted(rerun.glob("*/verdict.json")):
            try:
                v = json.loads(vf.read_text())
                verdicts[v["arena"]] = v  # isolated re-run supersedes the batch result
            except Exception:
                continue
    return verdicts


def main() -> int:
    verdicts = _load_verdicts()
    if not verdicts:
        print("no verdicts found under _e2e/*/verdict.json", file=sys.stderr)
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    matrix = {
        a: {"passed": bool(v.get("passed")), "seconds": v.get("seconds"),
            "why": (v.get("why") or "")[:200], "players": v.get("players")}
        for a, v in sorted(verdicts.items())
    }
    (OUT / "matrix.json").write_text(json.dumps(matrix, indent=2))
    n_pass = sum(1 for m in matrix.values() if m["passed"])
    print(f"wrote {OUT/'matrix.json'} — {n_pass}/{len(matrix)} arenas PASSED")

    # Reconcile against games.py live flags.
    sys.path.insert(0, str(ROOT / "src"))
    from atv_bench.games import live_keys  # noqa: E402

    live = set(live_keys())
    passed = {a for a, m in matrix.items() if m["passed"]}
    drift = sorted(live - passed)
    print(f"\nlive=True arenas: {len(live)} | passed in proof: {len(live & passed)}")
    if drift:
        print("DRIFT — live=True but NOT passing in the committed proof (downgrade these):")
        for a in drift:
            print(f"  {a}: {matrix.get(a, {}).get('why', 'no verdict')}")
    else:
        print("no drift — every live=True arena has a passing proof row.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
