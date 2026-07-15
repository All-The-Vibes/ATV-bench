"""Deterministic, forfeit-safe, variance-gated ELO (eng T9-T12).

Recompute-from-history: the leaderboard is a pure function of the full match list.
Given the same matches it produces byte-identical JSON, independent of the order the
matches are passed in (we sort by a stable key before folding). This kills the
'flapping board on CI re-run' failure mode.

Scoring policy (single, written down): win=1, loss=0, draw=0.5. A forfeit is a loss
for the forfeiting side plus a recorded reason — never dropped, because a dropped
forfeit silently skews everyone's rating.
"""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from typing import Any

SEED_ELO = 1500
K_FACTOR = 32
# variance-gate teeth
_MIN_MATCHES = 10          # below this: not enough data to publish a pair delta
_MAX_CI_WIDTH = 200        # wider CI than this: too noisy to publish
_MIN_PUBLISH_SPREAD = 100  # identical-ish players stay under this spread


class Outcome(str, enum.Enum):
    A_WINS = "a_wins"
    B_WINS = "b_wins"
    DRAW = "draw"
    FORFEIT_A = "forfeit_a"  # player A forfeited -> A loses
    FORFEIT_B = "forfeit_b"  # player B forfeited -> B loses


class ForfeitReason(str, enum.Enum):
    TIMEOUT = "TIMEOUT"
    INVALID_DIFF = "INVALID_DIFF"
    NO_OP = "NO_OP"
    MODEL_UNREACHABLE = "MODEL_UNREACHABLE"
    AUTH_FAILED = "AUTH_FAILED"
    CRASH = "CRASH"


@dataclass(frozen=True)
class MatchResult:
    player_a: str
    player_b: str
    outcome: Outcome
    forfeit_reason: ForfeitReason | None = None
    seed: int = 0
    game: str = "battlesnake"
    match_id: str = ""

    def __post_init__(self) -> None:
        is_forfeit = self.outcome in (Outcome.FORFEIT_A, Outcome.FORFEIT_B)
        if is_forfeit and self.forfeit_reason is None:
            raise ValueError("forfeit outcome requires a forfeit_reason")
        if not is_forfeit and self.forfeit_reason is not None:
            raise ValueError("forfeit_reason set on a non-forfeit outcome")

    def _score_a(self) -> float:
        return {
            Outcome.A_WINS: 1.0,
            Outcome.B_WINS: 0.0,
            Outcome.DRAW: 0.5,
            Outcome.FORFEIT_A: 0.0,
            Outcome.FORFEIT_B: 1.0,
        }[self.outcome]

    def _sort_key(self) -> tuple:
        # stable, total order for order-independent recompute
        return (self.match_id, self.seed, self.game,
                self.player_a, self.player_b, self.outcome.value)


@dataclass
class _Rating:
    elo: float = float(SEED_ELO)
    matches: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    forfeits: int = 0


def _expected(ra: float, rb: float) -> float:
    return 1.0 / (1.0 + 10 ** ((rb - ra) / 400))


def _ci_width(match_count: int) -> float:
    """Confidence-interval half-width that narrows with more matches.

    Provisional players have a wide band; it shrinks ~1/sqrt(n). Bounded so a
    single match doesn't imply infinite uncertainty in the JSON.
    """
    if match_count <= 0:
        return float(_MAX_CI_WIDTH * 2)
    return round(350.0 / math.sqrt(match_count), 2)


def compute_leaderboard(
    matches: list[MatchResult],
    entrants: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Pure recompute of the whole board from match history.

    `entrants` seeds players who have zero matches yet (zero-opponent provisional).
    Output is deterministic and order-independent.
    """
    ratings: dict[str, _Rating] = {}
    for name in entrants or []:
        ratings.setdefault(name, _Rating())

    # order-independent: fold matches in a stable, content-derived order
    for m in sorted(matches, key=lambda x: x._sort_key()):
        ra = ratings.setdefault(m.player_a, _Rating())
        rb = ratings.setdefault(m.player_b, _Rating())
        score_a = m._score_a()
        exp_a = _expected(ra.elo, rb.elo)
        delta = K_FACTOR * (score_a - exp_a)
        ra.elo += delta
        rb.elo -= delta
        ra.matches += 1
        rb.matches += 1
        if m.outcome == Outcome.DRAW:
            ra.draws += 1
            rb.draws += 1
        elif score_a == 1.0:
            ra.wins += 1
            rb.losses += 1
        else:
            ra.losses += 1
            rb.wins += 1
        if m.outcome == Outcome.FORFEIT_A:
            ra.forfeits += 1
        elif m.outcome == Outcome.FORFEIT_B:
            rb.forfeits += 1

    board: dict[str, dict[str, Any]] = {}
    for name in sorted(ratings):
        r = ratings[name]
        rated = r.matches > 0
        elo = round(r.elo, 2)
        half = _ci_width(r.matches)
        board[name] = {
            "elo": elo,
            "rated": rated,
            "match_count": r.matches,
            "wins": r.wins,
            "losses": r.losses,
            "draws": r.draws,
            "forfeits": r.forfeits,
            "ci": {"lo": round(elo - half, 2), "hi": round(elo + half, 2)},
            "status": "rated" if rated else "waiting_for_opponent",
        }
    return board


def variance_gate(
    matches: list[MatchResult],
    player_pair: tuple[str, str],
) -> dict[str, Any]:
    """A/A control: is the ELO delta between two players publishable, or noise?

    Numeric teeth: require >= _MIN_MATCHES between the pair, CI width under
    _MAX_CI_WIDTH, and (for near-identical players) spread over _MIN_PUBLISH_SPREAD.
    Identical bots split ~50/50 -> spread stays small -> not publishable.
    """
    a, b = player_pair
    pair_matches = [
        m for m in matches
        if {m.player_a, m.player_b} == {a, b}
    ]
    n = len(pair_matches)
    board = compute_leaderboard(matches)
    elo_a = board.get(a, {}).get("elo", SEED_ELO)
    elo_b = board.get(b, {}).get("elo", SEED_ELO)
    spread = abs(elo_a - elo_b)
    ci_width = max(_ci_width(board.get(a, {}).get("match_count", 0)),
                   _ci_width(board.get(b, {}).get("match_count", 0))) * 2

    result: dict[str, Any] = {
        "player_pair": [a, b],
        "n_matches": n,
        "elo_spread": round(spread, 2),
        "ci_width": round(ci_width, 2),
        "threshold": _MIN_PUBLISH_SPREAD,
        "publishable": False,
        "reason": "",
    }
    if n < _MIN_MATCHES:
        result["reason"] = "insufficient_matches"
        return result
    if ci_width > _MAX_CI_WIDTH:
        result["reason"] = "ci_too_wide"
        return result
    if spread < _MIN_PUBLISH_SPREAD:
        result["reason"] = "insufficient_signal"
        return result
    result["publishable"] = True
    result["reason"] = "ok"
    return result
