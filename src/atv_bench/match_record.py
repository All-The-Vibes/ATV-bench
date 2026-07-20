"""Run-pipeline match record + identity key (schema v2 — ENG-10 / gap #14).

Frozen in build step 0 as the shared contract between Lane A (fingerprint, which
produces model/tools/nested_skills/fingerprint_sha256) and Lane C (runner, which
records the live match). Kept SEPARATE from the v1 community-league leaderboard
(`leaderboard.py`, PR-submission flow) so the two evolve independently.

Verification honesty (CEO-2 / DX-8 / Eng-9): a Phase-1 host-subprocess result is an
integration milestone, not a publishable benchmark number. A row publishes a ranked
number ONLY when it is `verified=True` AND its model tag is a real parsed/gateway
model (never 'unknown'/'auto'). Phase 1 rows are `verified=False` and never publish.
"""
from __future__ import annotations

import dataclasses
from typing import Any

MATCH_RECORD_SCHEMA_VERSION = 2

# Model tags that can never back a published number (echoed input / unparseable).
_NONPUBLISHABLE_MODELS = {"unknown", "auto", ""}

# How a player's model tag was obtained, in increasing order of trust.
MODEL_SOURCES = ("recording", "parsed", "gateway")


@dataclasses.dataclass(frozen=True)
class PlayerRecord:
    """One player's provenance in a match record."""

    harness: str
    model: str
    model_source: str  # one of MODEL_SOURCES
    verified: bool  # gateway-authoritative model provenance (Phase 2); False in Phase 1
    tools: list[str]
    nested_skills: list[str]
    fingerprint_sha256: str
    adapter_version: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def identity_key(
    player: PlayerRecord, *, game_version: str, prompt_version: str
) -> tuple[str, str, str, str, str, str]:
    """Rating identity key (gap #14): NOT (game, harness, model).

    (game_version, prompt_version, harness, verified_model, fingerprint_sha256,
     adapter_version)
    """
    return (
        game_version,
        prompt_version,
        player.harness,
        player.model,
        player.fingerprint_sha256,
        player.adapter_version,
    )


def is_publishable(player: PlayerRecord) -> bool:
    """True only if this player may back a ranked, published number.

    Requires gateway/parsed verification AND a real model tag. Phase-1 (verified=False)
    rows never publish; an 'unknown'/'auto' tag never publishes even if flagged verified.
    """
    if not player.verified:
        return False
    if player.model.strip().lower() in _NONPUBLISHABLE_MODELS:
        return False
    return True


@dataclasses.dataclass
class MatchRecord:
    game: str
    game_version: str
    prompt_version: str
    codeclash_version: str
    rounds: int
    outcome: dict[str, Any]
    replay_path: str
    players: list[PlayerRecord] = dataclasses.field(default_factory=list)
    verified: bool | None = None
    adaptation: str = "iterative"
    trial_unit: str = "tournament"
    rounds_nested: bool = True
    round_evidence: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    rating_method: str = "bradley-terry-tournament-outcomes"
    ranked: bool = False

    def is_verified(self) -> bool:
        """A match is verified only if EVERY player is publishable."""
        return bool(self.players) and all(is_publishable(p) for p in self.players)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MATCH_RECORD_SCHEMA_VERSION,
            "game": self.game,
            "game_version": self.game_version,
            "prompt_version": self.prompt_version,
            "codeclash_version": self.codeclash_version,
            "rounds": self.rounds,
            "adaptation": self.adaptation,
            "trial_unit": self.trial_unit,
            "rounds_nested": self.rounds_nested,
            "round_observation_unit": "nested-round",
            "outcome": self.outcome,
            "round_evidence": self.round_evidence,
            "rating_method": self.rating_method,
            "ranked": self.ranked,
            "replay_path": self.replay_path,
            "players": [p.to_dict() for p in self.players],
            "verified": self.is_verified() if self.verified is None else self.verified,
        }
