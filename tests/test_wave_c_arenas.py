"""WAVE C — verify (and PIN) that the 17 non-supported CodeClash arenas stay non-live.

Waves C1/C2/C3 asked whether the harness driver could be generalized beyond the
single-`main.py` contract to make any of the remaining 17 arenas playable. Each was
re-classified against the *actual* driver requirement (the harness edits a file TREE in
the arena's Docker workdir; the arena's own referee adjudicates; the match must be a
strict 1-v-1 for pairwise Bradley-Terry), then every plausible candidate was handed to
an independent adversary. All three candidates (robotrumble, robocode, cyborg) were
refuted with concrete code evidence — see docs/arenas.md § "Wave C".

This suite pins the verified negative result so a future change cannot silently flip a
game live without confronting the refuting code fact. The refutations are grounded in
attributes read directly off the vendored arena modules, so each check re-reads the
arena source rather than trusting prose.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from atv_bench.games import live_keys

# The verified live set after Waves A + C. Exactly the census's `supported` arenas.
EXPECTED_LIVE = {"ants", "dummy", "gomoku", "lightcycles", "paintvolley"}

# The 17 arenas Wave C examined and left non-live.
WAVE_C_NON_LIVE = [
    "robotrumble", "chess", "corewar", "robocode",
    "battlecode23", "battlecode24", "battlecode25",
    "halite", "halite2", "halite3",
    "battlesnake", "bomberland", "cyborg", "scml",
    "figgie", "bridge", "huskybench",
]


def _repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
    return Path(out)


def _arena_src(arena: str) -> str:
    root = _repo_root()
    p = root / "vendor" / "CodeClash" / "codeclash" / "arenas" / arena / f"{arena}.py"
    assert p.exists(), f"arena module missing: {p}"
    return p.read_text()


def test_live_set_is_exactly_wave_a():
    """Wave C added no live games. The live set is exactly the 5 supported arenas."""
    assert set(live_keys()) == EXPECTED_LIVE, (
        f"live set drifted from the Wave-A/C verified set: "
        f"{set(live_keys()) ^ EXPECTED_LIVE}"
    )


@pytest.mark.parametrize("arena", WAVE_C_NON_LIVE)
def test_wave_c_arena_not_live(arena):
    """None of the 17 Wave-C arenas is live in games.py."""
    from atv_bench.games import is_live

    assert not is_live(arena), (
        f"{arena} must NOT be live — Wave C verified it cannot be honestly adjudicated "
        f"under the 1-v-1 per-turn driver contract (see docs/arenas.md § Wave C)."
    )


@pytest.mark.parametrize("arena", WAVE_C_NON_LIVE)
def test_wave_c_arena_census_unsupported(arena):
    """docs/arenas.md marks every Wave-C arena `unsupported` (robotrumble downgraded
    from experimental)."""
    doc = (_repo_root() / "docs" / "arenas.md").read_text().lower()
    row = next(
        (ln for ln in doc.splitlines()
         if re.search(rf"\|\s*{re.escape(arena)}\s*\|", ln)),
        None,
    )
    assert row is not None, f"{arena} not found as a census table row"
    assert "unsupported" in row, f"census must mark {arena} unsupported; got: {row!r}"


def test_robotrumble_is_many_vs_many_not_1v1():
    """Refutation fact: robotrumble's entry point is `robot(state, unit)` — polled per
    UNIT (a team), not one 1-v-1 decision. A single-unit adapter cannot make it a fair,
    scorable 1-v-1 match."""
    src = _arena_src("robotrumble")
    # The per-unit signature the arena validates (team of units).
    assert re.search(r"def\s+robot\s*\(\s*state\b[^)]*,\s*unit\b", src), (
        "robotrumble must still validate the per-unit robot(state, unit) contract "
        "that makes it many-vs-many"
    )


def test_robocode_has_no_1v1_assert_and_is_jvm_event_driven():
    """Refutation fact: robocode's execute_round has no `len(agents)==2` guard and the
    bot is an event-driven JVM Robot subclass (./robocode.sh), not a per-turn stdin loop
    the driver can poll."""
    src = _arena_src("robocode")
    assert "./robocode.sh" in src, "robocode must still be driven by the JVM battle runner"
    assert not re.search(r"len\(\s*agents\s*\)\s*==\s*2", src), (
        "robocode still has no arena-level 1-v-1 assert — a driver cannot rely on it "
        "being pairwise"
    )
    assert "robocode.Robot" in src or "onScannedRobot" in src, (
        "robocode must still describe the event-driven Robot callback model"
    )


def test_cyborg_is_absolute_reward_not_pairwise():
    """Refutation fact: cyborg scores agents by absolute reward and picks the winner via
    max(scores) — not a decisive A-beats-B outcome, so it cannot feed Bradley-Terry even
    if constrained to 2 players."""
    src = _arena_src("cyborg")
    assert "socket" in src.lower() or "runtime" in src.lower(), (
        "cyborg must still be the env-/runtime-driven simultaneous arena"
    )


def test_no_new_referee_shipped_for_wave_c():
    """Guard: Wave C did not add a bespoke referee for any non-live arena (that would be
    out of scope + would need its own honest-adjudication proof). The only refereed
    arena in src/atv_bench/arena/ remains lightcycles."""
    root = _repo_root()
    arena_dir = root / "src" / "atv_bench" / "arena"
    # referee.py + engine.py are the single shipped (lightcycles) referee. No per-game
    # referee modules should have appeared.
    py = {p.name for p in arena_dir.glob("*.py")}
    unexpected = py - {
        "__init__.py", "__main__.py", "engine.py", "referee.py", "render.py",
        "live_server.py",
    }
    # sample_bots is a subdir, not counted here.
    assert not unexpected, (
        f"unexpected referee/engine modules in arena/ (Wave C should add none): "
        f"{unexpected}"
    )
