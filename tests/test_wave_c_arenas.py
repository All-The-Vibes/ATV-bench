"""WAVE C — the 17 non-Wave-A CodeClash arenas, classified by REAL end-to-end results.

This supersedes the earlier (incorrect) Wave C conclusion that all 17 remaining arenas
were "unsupported / need a new referee". That was wrong: CodeClash already ships a working
referee for every arena, the harness edits the arena's own submission (source in any
language — the arena's Docker image compiles/runs it), and CodeClash's own `run_round`
reduces the match to a decisive 1-v-1 winner. Reassessment + a live end-to-end matrix
(Docker build + live harness bots + real arena adjudication, one match per arena) proved
it: 15 of the 17 produce real scored matches and are now live; 2 are blocked ONLY by an
upstream CodeClash bug (unguarded `max(scores)` on an empty round), not any architectural
mismatch.

See docs/arenas.md § "Wave C — end-to-end verification" and _e2e/FINAL_MATRIX.json.

This suite pins the empirically-verified classification: the e2e-proven arenas are live,
the two upstream-blocked arenas are not, and the census agrees.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from atv_bench.games import get_game, is_live, live_keys

# Wave A (single-main.py contract) — already live before Wave C.
WAVE_A_LIVE = {"lightcycles", "ants", "dummy", "gomoku", "paintvolley"}

# Wave C arenas proven live by a REAL end-to-end scored match (Docker + live harness +
# arena adjudication). Read off _e2e/FINAL_MATRIX.json (all passed=True).
WAVE_C_LIVE = {
    "corewar", "robotrumble", "battlesnake", "huskybench", "scml", "chess",
    "halite", "halite2", "halite3", "cyborg", "bomberland",
    "battlecode23", "battlecode24", "figgie", "bridge",
}

# Wave C arenas whose referee is reusable but which CRASH on an upstream CodeClash bug
# (get_results does `max(scores, key=...)` with no empty guard; a round with no decisive
# sim leaves scores empty -> ValueError). Their siblings battlecode23/24 guard it. Blocked
# until upstream fixes it — NOT an architectural mismatch.
WAVE_C_BLOCKED = {"robocode", "battlecode25"}

ALL_LIVE = WAVE_A_LIVE | WAVE_C_LIVE  # 20 live arenas


def _repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent, text=True,
    ).strip()
    return Path(out)


def test_live_set_is_wave_a_plus_proven_wave_c():
    """The live set is exactly Wave A (5) + the e2e-proven Wave C arenas (15) = 20."""
    assert set(live_keys()) == ALL_LIVE, (
        f"live set drift: {set(live_keys()) ^ ALL_LIVE}"
    )
    assert len(live_keys()) == 20, f"expected 20 live, got {len(live_keys())}"


@pytest.mark.parametrize("arena", sorted(WAVE_C_LIVE))
def test_wave_c_proven_arena_is_live(arena):
    """Every arena that passed a real end-to-end scored match is live with a GameSpec."""
    from atv_bench.config import GAME_SPECS

    assert is_live(arena), f"{arena} passed e2e — must be live"
    g = get_game(arena)
    assert g is not None and g.live
    assert arena in GAME_SPECS, f"{arena} must have a GameSpec"
    spec = GAME_SPECS[arena]
    assert spec.edit_prompt.strip(), f"{arena} needs a non-empty edit_prompt"
    # entrypoint/bot_file must be the arena's real submission path (may be a dir).
    assert g.entrypoint == spec.bot_file, (
        f"{arena}: games entrypoint {g.entrypoint!r} != GameSpec bot_file {spec.bot_file!r}"
    )


@pytest.mark.parametrize("arena", sorted(WAVE_C_BLOCKED))
def test_wave_c_upstream_blocked_arena_is_not_live(arena):
    """robocode + battlecode25 crash on the upstream empty-scores bug — not live."""
    assert not is_live(arena), (
        f"{arena} crashes on CodeClash's unguarded max(scores) — must NOT be live until "
        f"upstream guards the empty case (see docs/arenas.md § Wave C)."
    )
    g = get_game(arena)
    assert g is not None and g.live is False


@pytest.mark.integration
def test_submission_paths_match_codeclash_arena_modules():
    """Each live Wave-C arena's entrypoint equals the CodeClash arena's `submission` attr
    (read off the vendored module) — so the harness edits the file/dir the arena drives.

    Gated ``integration``: reads vendored CodeClash arena modules under vendor/CodeClash,
    which require the submodule to be checked out (absent in hermetic CI).
    """
    root = _repo_root()
    for arena in sorted(WAVE_C_LIVE):
        mod = root / "vendor" / "CodeClash" / "codeclash" / "arenas" / arena / f"{arena}.py"
        src = mod.read_text() if mod.exists() else ""
        m = re.search(r'submission:\s*str\s*=\s*"([^"]+)"', src)
        # halite2 inherits submission from HaliteArena — skip the direct-attr check there.
        if not m and arena == "halite2":
            continue
        assert m, f"could not read submission= from {arena}.py"
        submission = m.group(1)
        g = get_game(arena)
        # robot.js vs robot.py: robotrumble accepts either; we edit robot.py.
        if arena == "robotrumble":
            assert g.entrypoint in ("robot.py", "robot.js")
            continue
        assert g.entrypoint == submission, (
            f"{arena}: games entrypoint {g.entrypoint!r} != arena submission {submission!r}"
        )


def test_no_bespoke_referee_shipped():
    """Wave C reused CodeClash's referees — it did NOT add a per-game referee to
    src/atv_bench/arena/ (that would be out of scope + need its own honesty proof). The
    only shipped engine/referee remains lightcycles."""
    root = _repo_root()
    arena_dir = root / "src" / "atv_bench" / "arena"
    py = {p.name for p in arena_dir.glob("*.py")}
    unexpected = py - {
        "__init__.py", "__main__.py", "engine.py", "referee.py", "render.py",
        "live_server.py",
    }
    assert not unexpected, f"unexpected referee modules (Wave C should add none): {unexpected}"
