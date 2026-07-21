"""Paired, side-balanced match scheduler (gap G1).

Pure planning: given a set of harnesses and games, emit the plan of head-to-head
matches with *balanced sides*. This is a PLAN — no scores, no execution, no I/O,
no LLM calls. It is fully deterministic under a fixed ``seed``.

Side-balance rule
-----------------
For each unordered pair ``{c0, c1}`` (canonical sorted order) and each game, the
pair plays ``repeats`` matches. The A/B seat alternates across those repeats:

    side_index = expansion_index % 2

so the A seat and B seat are shared equally. When a pair plays an *even* number
of matches per game the seats are exactly balanced; when *odd*, they differ by at
most one locally, and the alternation is threaded across the whole expansion so
the *global* seat-A / seat-B counts remain exactly equal (for even totals).

``side_index == 0`` seats ``c0`` in the A seat; ``side_index == 1`` swaps them.
"""
from __future__ import annotations

import dataclasses
import itertools
import random


@dataclasses.dataclass(frozen=True)
class Match:
    """One scheduled (planned) head-to-head match — no outcome yet.

    ``side_index`` is 0 or 1: 0 seats the canonically-first harness in the A seat,
    1 swaps the seats. Field naming mirrors ``rating.RatingMatch`` (harness_a/b).
    """

    game: str
    harness_a: str
    harness_b: str
    side_index: int
    repeat_index: int


def build_paired_schedule(
    harnesses,
    games,
    *,
    seed: int = 0,
    repeats: int = 1,
) -> list[Match]:
    """Build a side-balanced round-robin plan.

    Every unordered pair of harnesses plays every game ``repeats`` times with
    alternating A/B seats. Returns an empty list when fewer than two harnesses,
    no games, or ``repeats <= 0``.

    Deterministic: identical ``seed`` yields an identical ordered schedule; a
    different seed may reorder the matches but preserves the balance and total
    invariants.
    """
    harnesses = list(harnesses)
    games = list(games)
    if len(harnesses) < 2 or not games or repeats <= 0:
        return []

    matches: list[Match] = []
    expansion_index = 0
    for game in games:
        for c0, c1 in itertools.combinations(harnesses, 2):
            for repeat_index in range(repeats):
                side_index = expansion_index % 2
                if side_index == 0:
                    a, b = c0, c1
                else:
                    a, b = c1, c0
                matches.append(
                    Match(
                        game=game,
                        harness_a=a,
                        harness_b=b,
                        side_index=side_index,
                        repeat_index=repeat_index,
                    )
                )
                expansion_index += 1

    # Deterministic reorder: seed changes the presentation order, not the
    # side assignment carried on each Match, so balance/total are invariant.
    rng = random.Random(seed)
    rng.shuffle(matches)
    return matches
