"""`atv-bench run` orchestration (Lane C) — host-subprocess harness-vs-harness.

Flow: validate args → preflight → register the pinned CodeClash integration → run one
edit/compete/feedback tournament trial → collect nested round evidence → emit an
unverified local record. Scientific ranking consumes tournament outcomes through a
tie-aware Bradley-Terry design, never sequential League rating updates.

The Docker/live parts are isolated in `run_match` and only exercised in the E2E step;
the host-side logic here is unit-tested (preflight, record building, arg validation).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from atv_bench.adapters.contract import Budget
from atv_bench import preflight as pf
from atv_bench.config import GAME_SPECS, build_pvp_config, resolve_game
from atv_bench.match_record import MatchRecord, PlayerRecord
from atv_bench.players import ADAPTATION_ITERATIVE, ADAPTATION_MODES
from atv_bench.run_envelope import RunError

ADAPTER_VERSION = "1.0.0"
# Which CLI binary each harness needs on PATH (no-fake preflight).
_HARNESS_BINARY = {"claude-code": "claude", "copilot-cli": "copilot"}


@dataclasses.dataclass
class RunConfig:
    game: str
    a: str
    b: str
    model: str
    rounds: int
    adaptation: str = ADAPTATION_ITERATIVE
    budget: Budget = dataclasses.field(default_factory=Budget)
    adapter_version: str = ADAPTER_VERSION
    protocol_version: str = "atv.harness/v1"

    def validate(self) -> None:
        if self.game not in GAME_SPECS:
            valid = ", ".join(sorted(GAME_SPECS))
            raise RunError("usage", f"unknown game {self.game!r}. Valid games: {valid}.",
                           fix=f"use one of: {valid}")
        for side, h in (("--a", self.a), ("--b", self.b)):
            if h not in _HARNESS_BINARY:
                valid = ", ".join(sorted(_HARNESS_BINARY))
                raise RunError("usage",
                               f"unknown harness {h!r} for {side}. Valid: {valid}.",
                               fix=f"use one of: {valid}")
        if not isinstance(self.rounds, int) or self.rounds < 1:
            raise RunError("usage", f"--rounds must be a positive integer, got {self.rounds!r}",
                           fix="pass --rounds 1 or higher")
        if self.adaptation not in ADAPTATION_MODES:
            raise RunError(
                "usage",
                f"--adaptation must be one of {', '.join(ADAPTATION_MODES)}, "
                f"got {self.adaptation!r}",
                fix="use --adaptation iterative for paper-faithful CodeClash",
            )


def preflight_or_raise(cfg: RunConfig, *, require_docker: bool = True,
                       require_codeclash: bool = True) -> None:
    """Fail-closed preflight (no-fake guard, DX-4 aggregate). Raises the FIRST blocking
    RunError but its message reports ALL failures at once."""
    cfg.validate()
    checks = []
    # each harness CLI must be on PATH — a missing CLI NEVER substitutes a fake bot.
    for h in (cfg.a, cfg.b):
        binary = _HARNESS_BINARY[h]
        checks.append(("missing_cli", pf.check_cli_on_path(binary)))
    if require_docker:
        checks.append(("docker_unavailable", pf.check_docker()))
    if require_codeclash:
        checks.append(("codeclash_dep", pf.check_codeclash()))

    failures = [(code, c) for code, c in checks if not c.ok]
    if not failures:
        return
    # Aggregate every failure into one message; map exit code to the first blocker.
    detail = "; ".join(f"{c.detail}" + (f" (fix: {c.fix})" if c.fix else "")
                       for _code, c in failures)
    first_code = failures[0][0]
    raise RunError(first_code, f"preflight failed: {detail}",
                   fix="run `atv-bench doctor` for a full prerequisite report")


def build_match_record(
    cfg: RunConfig, *, outcome: dict[str, Any],
    player_models: dict[str, tuple[str, str]],
    player_fingerprints: dict[str, str],
    replay_path: str,
    verified: bool = False,
) -> MatchRecord:
    """Assemble the schema-v2 record. Phase 1 is verified=False → never publishes."""
    from atv_bench.codeclash_env import CODECLASH_VERSION

    spec = resolve_game(cfg.game)
    players = []
    for h in (cfg.a, cfg.b):
        model, source = player_models.get(h, ("unknown", "parsed"))
        players.append(PlayerRecord(
            harness=h,
            model=model,
            model_source=source,
            verified=verified,
            tools=[],
            nested_skills=[],
            fingerprint_sha256=player_fingerprints.get(h, "0" * 64),
            adapter_version=ADAPTER_VERSION,
        ))
    return MatchRecord(
        game=cfg.game,
        game_version=spec.version,
        prompt_version="edit@1",
        codeclash_version=CODECLASH_VERSION,
        rounds=cfg.rounds,
        outcome=outcome,
        replay_path=replay_path,
        players=players,
        verified=verified,
        adaptation=cfg.adaptation,
        trial_unit="tournament",
        rounds_nested=True,
        round_evidence=list(outcome.get("round_evidence", [])),
        rating_method="bradley-terry-tournament-outcomes",
        ranked=False,
    )


def fingerprint_harness_repo(harness: str, home: Path | None) -> tuple[str, dict]:
    """Fingerprint a harness config root; return (sha256, manifest).

    The manifest is leak-safe (secrets scrubbed) and its sha256 is the identity anchor
    in the schema-v2 key. `home` points at a cloned repo's config root for the repo-
    harness showcase, or None to auto-detect the local harness dir.
    """
    import hashlib
    import json

    from atv_bench.fingerprint import probe

    result = probe.probe(home=home, harness=harness)
    manifest = result.manifest
    blob = json.dumps(manifest, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest(), manifest


def summarize_tournament(raw: dict, cfg: RunConfig) -> tuple[dict, dict]:
    """Extract (outcome, player_models) from a CodeClash tournament metadata dict.

    Rounds are nested evidence. The tournament contributes one tie-aware outcome to a
    Bradley-Terry matchup matrix. Requested model labels remain unverified.
    """
    meta = raw.get("metadata", {})
    round_stats = meta.get("round_stats", {})
    name_to_harness = {
        str(player.get("name")): str(player.get("agent"))
        for player in raw.get("pvp_config", {}).get("players", [])
        if isinstance(player, dict)
    }
    decisive: list[str] = []
    if isinstance(round_stats, dict):
        for rnum, rs in sorted(
            round_stats.items(),
            key=lambda item: _round_order_key(item[0]),
        ):
            if str(rnum) == "0":
                continue  # identical-seed control round
            w = rs.get("winner")
            if w and w != "Tie":
                decisive.append(name_to_harness.get(str(w), str(w)))
    winner = _majority_winner(decisive)
    bt_summary = build_bradley_terry_summary(
        [
            {
                "player_a": cfg.a,
                "player_b": cfg.b,
                "winner": winner,
            }
        ]
    )
    outcome = {
        "winner": winner,
        "tournament_outcome": winner,
        "round_winners": decisive,
        "round_stats": _compact_round_stats(round_stats),
        "round_evidence": _extract_round_evidence(meta),
        "trial_unit": "tournament",
        "round_observation_unit": "nested-round",
        "rounds_nested": True,
        "adaptation": cfg.adaptation,
        "rating_method": "bradley-terry-tournament-outcomes",
        "bradley_terry": bt_summary,
        "ranking_published": False,
        "requested_model_verified": False,
    }
    models = {cfg.a: (cfg.model, "recording"), cfg.b: (cfg.model, "recording")}
    return outcome, models


def _compact_round_stats(round_stats: dict) -> dict:
    out = {}
    if isinstance(round_stats, dict):
        for rnum, rs in round_stats.items():
            out[str(rnum)] = {"winner": rs.get("winner"), "scores": rs.get("scores", {})}
    return out


def _round_order_key(round_number: object) -> tuple[int, int | str]:
    """Sort numeric round identifiers chronologically, with stable text fallback."""
    try:
        return (0, int(str(round_number)))
    except ValueError:
        return (1, str(round_number))


def _majority_winner(decisive: list[str]) -> str:
    """Apply CodeClash's round-win rule, including its last-win tie-break."""
    if not decisive:
        return "tie"
    counts = {player: decisive.count(player) for player in set(decisive)}
    best = max(counts.values())
    winners = sorted(player for player, count in counts.items() if count == best)
    return winners[0] if len(winners) == 1 else decisive[-1]


def build_bradley_terry_summary(
    tournament_outcomes: list[dict[str, str]],
) -> dict[str, Any]:
    """Build tie-aware tournament-unit matchup counts for CodeClash's BT fitter.

    One tournament contributes exactly one outcome: [1,0], [0,1], or [0.5,0.5].
    This local record is unranked; downstream official analysis may feed the matrix to
    the pinned ``BradleyTerryFitter`` when its scientific dependencies are installed.
    """

    matrix: dict[tuple[str, str], list[float]] = {}
    for row in tournament_outcomes:
        a, b, winner = row["player_a"], row["player_b"], row["winner"]
        pair = tuple(sorted((a, b)))
        counts = matrix.setdefault(pair, [0.0, 0.0])
        if winner in {"tie", "Tie", ""}:
            counts[0] += 0.5
            counts[1] += 0.5
        elif winner == pair[0]:
            counts[0] += 1.0
        elif winner == pair[1]:
            counts[1] += 1.0
        else:
            raise ValueError(f"winner {winner!r} is not in matchup {pair!r}")
    return {
        "method": "bradley-terry",
        "independent_unit": "tournament",
        "tie_handling": "half-win-each",
        "win_matrix": {
            f"{a}::{b}": counts
            for (a, b), counts in sorted(matrix.items())
        },
        "sequential_league_updates": False,
        "fit_status": "deferred-to-official-analysis",
    }


def _extract_round_evidence(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    for agent in metadata.get("agents", []) if isinstance(metadata, dict) else []:
        atv = agent.get("atv", {}) if isinstance(agent, dict) else {}
        rounds = atv.get("rounds", {}) if isinstance(atv, dict) else {}
        for round_number, value in rounds.items() if isinstance(rounds, dict) else []:
            evidence.append(
                {
                    "player": agent.get("name"),
                    "round": int(round_number),
                    **dict(value),
                }
            )
    return sorted(
        evidence,
        key=lambda value: (value.get("round", 0), str(value.get("player", ""))),
    )


def run_live_match(cfg: RunConfig, *, output_dir: Path,
                   homes: dict[str, Path | None] | None = None) -> dict[str, Any]:  # pragma: no cover - Docker + live CLIs
    """Drive a real host-subprocess harness-vs-harness match via CodeClash (Docker).

    Registers the monkeypatch, builds the pvp config, runs the tournament, and returns
    the raw outcome + per-player parsed model tags. Isolated here so unit tests never
    need Docker; exercised in the E2E proof step.
    """
    from atv_bench import integration
    from atv_bench.codeclash_env import import_codeclash

    cfg.validate()
    preflight_or_raise(cfg)
    import_codeclash()
    integration.register()
    try:
        identities: dict[str, dict[str, Any]] = {}
        for harness in {cfg.a, cfg.b}:
            home = (homes or {}).get(harness)
            try:
                config_digest, manifest = fingerprint_harness_repo(harness, home)
            except Exception:
                config_digest, manifest = "0" * 64, {}
            raw_manifest_digest = manifest.get("manifest_digest")
            if isinstance(raw_manifest_digest, dict):
                raw_manifest_digest = raw_manifest_digest.get("value")
            manifest_digest = (
                raw_manifest_digest
                if isinstance(raw_manifest_digest, str)
                and len(raw_manifest_digest) == 64
                else config_digest
            )
            raw_capabilities = manifest.get("capabilities")
            capabilities = (
                dict(raw_capabilities)
                if isinstance(raw_capabilities, dict)
                else {"resumable": False}
            )
            identities[harness] = {
                "config_digest": config_digest,
                "manifest_digest": manifest_digest,
                "capabilities": capabilities,
            }
        pvp_config = build_pvp_config(
            game=cfg.game,
            a=cfg.a,
            b=cfg.b,
            model=cfg.model,
            rounds=cfg.rounds,
            adaptation=cfg.adaptation,
            harness_identities=identities,
            adapter_version=cfg.adapter_version,
            protocol_version=cfg.protocol_version,
            budget=cfg.budget.to_dict(),
        )
        from codeclash.tournaments.pvp import PvpTournament

        output_dir.mkdir(parents=True, exist_ok=True)
        tournament = PvpTournament(pvp_config, output_dir=output_dir)
        tournament.run()
        metadata = tournament.get_metadata()
        return {
            "metadata": metadata,
            "pvp_config": pvp_config,
            "harness_identities": identities,
        }
    finally:
        integration.unregister()

