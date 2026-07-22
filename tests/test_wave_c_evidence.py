"""Follow-up 4 (PR #19): Wave C live=True flags must be backed by committed live-match proof.

The earlier Wave C classification hardcoded a WAVE_C_LIVE set with no reproducible artifact
in the repo (`_e2e/FINAL_MATRIX.json` was referenced but never committed). This test makes the
claim falsifiable: every arena flagged ``live=True`` in games.py must have a PASS row in the
committed proof (docs/proof/wave-c/matrix.json), unless it is a known upstream-blocked arena.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from atv_bench.games import live_keys

PROOF = Path(__file__).resolve().parent.parent / "docs" / "proof" / "wave-c" / "matrix.json"

# Arenas whose CodeClash referee is reusable but which crash on an upstream bug
# (unguarded max() on an empty round). Not an architectural mismatch; excused from the
# live-proof requirement because they are NOT flagged live either.
UPSTREAM_BLOCKED = {"robocode", "battlecode25"}


def _load_proof() -> dict:
    assert PROOF.exists(), (
        f"missing committed Wave C proof {PROOF}; run "
        f"scripts/e2e_arena_matrix.py --all then scripts/consolidate_wave_c_proof.py"
    )
    return json.loads(PROOF.read_text())


def test_wave_c_proof_exists_and_nonempty():
    proof = _load_proof()
    assert proof, "Wave C proof matrix is empty"


def test_every_live_arena_has_passing_proof():
    """Each live=True arena has a PASS in the committed proof (AC4.2/4.3)."""
    proof = _load_proof()
    passed = {a for a, m in proof.items() if m.get("passed")}
    unproven = sorted(set(live_keys()) - passed - UPSTREAM_BLOCKED)
    assert not unproven, (
        f"these arenas are live=True but have no passing live-match proof: {unproven}. "
        f"Either commit passing evidence or downgrade them to live=False."
    )


def test_no_blocked_arena_is_live():
    """An upstream-blocked arena must never be flagged live (AC4.3)."""
    live = set(live_keys())
    wrongly_live = sorted(UPSTREAM_BLOCKED & live)
    assert not wrongly_live, f"upstream-blocked arenas flagged live: {wrongly_live}"
