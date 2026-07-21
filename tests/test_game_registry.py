"""SECTION 0 game-registry tests: a protocol census of every CodeClash arena.

RED-first: docs/arenas.md does not exist yet, so test_protocol_census_complete
must fail. test_enumerate_codeclash_games pins the arena count at 22.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

# The 22 CodeClash arenas (dirs under vendor/CodeClash/codeclash/arenas).
CODECLASH_ARENAS = [
    "ants",
    "battlecode23",
    "battlecode24",
    "battlecode25",
    "battlesnake",
    "bomberland",
    "bridge",
    "chess",
    "corewar",
    "cyborg",
    "dummy",
    "figgie",
    "gomoku",
    "halite",
    "halite2",
    "halite3",
    "huskybench",
    "lightcycles",
    "paintvolley",
    "robocode",
    "robotrumble",
    "scml",
]

PROTOCOLS = {"one-shot", "iterative", "simultaneous"}
SUPPORT_STATUSES = {"supported", "unsupported", "experimental"}


def _repo_root() -> Path:
    out = subprocess.check_output(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(__file__).resolve().parent,
        text=True,
    ).strip()
    return Path(out)


def test_protocol_census_complete() -> None:
    """docs/arenas.md must classify all 22 arenas by protocol and support status."""
    root = _repo_root()
    arenas_doc = root / "docs" / "arenas.md"
    assert arenas_doc.exists(), "docs/arenas.md does not exist"

    text = arenas_doc.read_text().lower()

    for arena in CODECLASH_ARENAS:
        # Find the line(s) mentioning this arena.
        arena_lines = [
            line for line in text.splitlines() if re.search(rf"\b{re.escape(arena)}\b", line)
        ]
        assert arena_lines, f"arena {arena!r} not mentioned in docs/arenas.md"

        joined = " ".join(arena_lines)
        assert any(p in joined for p in PROTOCOLS), (
            f"arena {arena!r} has no classified protocol "
            f"(one of {sorted(PROTOCOLS)}) on its line"
        )
        assert any(s in joined for s in SUPPORT_STATUSES), (
            f"arena {arena!r} has no support status "
            f"(one of {sorted(SUPPORT_STATUSES)}) on its line"
        )


@pytest.mark.integration
def test_enumerate_codeclash_games() -> None:
    """The number of arena directories must equal 22, pinning the census scope.

    Gated ``integration``: reads the vendored ``vendor/CodeClash/codeclash/arenas``
    working tree, which requires ``git submodule update --init``. Hermetic CI does not
    check out submodules, so this content check runs in the submodule-aware lane.
    """
    root = _repo_root()
    arenas_dir = root / "vendor" / "CodeClash" / "codeclash" / "arenas"
    assert arenas_dir.is_dir(), f"arenas dir missing: {arenas_dir}"

    excluded = {"__init__.py", "arena.py", "__pycache__"}
    dirs = sorted(
        p.name
        for p in arenas_dir.iterdir()
        if p.is_dir() and p.name not in excluded
    )
    assert len(dirs) == 22, f"expected 22 arena dirs, found {len(dirs)}: {dirs}"
    assert set(dirs) == set(CODECLASH_ARENAS), (
        f"arena dir set mismatch: {set(dirs) ^ set(CODECLASH_ARENAS)}"
    )


# --- The census `supported` set must equal the live set (bidirectional invariant). ------
# After Wave A (5) + Wave C e2e verification (15), the live set is 20 arenas. This is kept
# in sync with docs/arenas.md: a game is `supported` in the census iff it is live in
# games.py. (robocode + battlecode25 are `unsupported` — upstream empty-scores crash.)

EXPECTED_LIVE = {
    # Wave A — single-main.py contract
    "ants", "dummy", "gomoku", "lightcycles", "paintvolley",
    # Wave C — reuse CodeClash's referee, proven by a real e2e scored match
    "corewar", "robotrumble", "battlesnake", "huskybench", "scml", "chess",
    "halite", "halite2", "halite3", "cyborg", "bomberland",
    "battlecode23", "battlecode24", "figgie", "bridge",
}


def test_supported_census_arenas_are_live() -> None:
    """Every arena the census marks `supported` must be live in games.py, and vice
    versa — the live set is exactly the census's supported set."""
    from atv_bench.games import live_keys

    root = _repo_root()
    text = (root / "docs" / "arenas.md").read_text().lower()

    supported = []
    for arena in CODECLASH_ARENAS:
        row = next(
            (ln for ln in text.splitlines()
             if re.search(rf"\|\s*{re.escape(arena)}\s*\|", ln)),
            None,
        )
        if row and re.search(r"\|\s*supported\s*\|", row):
            supported.append(arena)

    assert set(supported) == EXPECTED_LIVE, (
        f"census supported set drifted from live set: {set(supported) ^ EXPECTED_LIVE}"
    )
    assert set(live_keys()) == EXPECTED_LIVE, (
        f"live_keys() must equal census supported set; "
        f"diff: {set(live_keys()) ^ EXPECTED_LIVE}"
    )

