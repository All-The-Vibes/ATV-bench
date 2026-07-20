"""SECTION 0 game-registry tests: a protocol census of every CodeClash arena.

RED-first: docs/arenas.md does not exist yet, so test_protocol_census_complete
must fail. test_enumerate_codeclash_games pins the arena count at 22.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

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


def test_enumerate_codeclash_games() -> None:
    """The number of arena directories must equal 22, pinning the census scope."""
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
