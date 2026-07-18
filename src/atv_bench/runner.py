"""`atv-bench run` orchestration (Lane C) — host-subprocess harness-vs-harness.

Flow: validate args → preflight (fail-closed, no-fake guard) → register the CodeClash
monkeypatch → build the pvp config → run the tournament (Docker arena) → collect the
outcome + parsed model tags + fingerprints → build a schema-v2 match record (verified=
False in Phase 1, so no published number) → ELO (single-build anecdote, not a ranked
aggregate) + replay.

The Docker/live parts are isolated in `run_match` and only exercised in the E2E step;
the host-side logic here is unit-tested (preflight, record building, arg validation).
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

from atv_bench import preflight as pf
from atv_bench.config import GAME_SPECS, build_pvp_config, resolve_game
from atv_bench.match_record import MatchRecord, PlayerRecord
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

    `outcome.winner` is the harness that won the most non-tie rounds (excluding the
    identical-seed round 0). player_models maps harness → (model, source); Phase 1 uses
    the requested model as the parsed tag (best-effort) pending gateway verification.
    """
    meta = raw.get("metadata", {})
    round_stats = meta.get("round_stats", {})
    decisive: list[str] = []
    if isinstance(round_stats, dict):
        for rnum, rs in round_stats.items():
            if str(rnum) == "0":
                continue  # identical-seed control round
            w = rs.get("winner")
            if w and w != "Tie":
                decisive.append(w)
    winner = max(set(decisive), key=decisive.count) if decisive else "tie"
    outcome = {"winner": winner, "round_winners": decisive,
               "round_stats": _compact_round_stats(round_stats)}
    models = {cfg.a: (cfg.model, "parsed"), cfg.b: (cfg.model, "parsed")}
    return outcome, models


def _compact_round_stats(round_stats: dict) -> dict:
    out = {}
    if isinstance(round_stats, dict):
        for rnum, rs in round_stats.items():
            out[str(rnum)] = {"winner": rs.get("winner"), "scores": rs.get("scores", {})}
    return out


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
        pvp_config = build_pvp_config(
            game=cfg.game, a=cfg.a, b=cfg.b, model=cfg.model, rounds=cfg.rounds)
        from codeclash.tournaments.pvp import PvpTournament

        output_dir.mkdir(parents=True, exist_ok=True)
        tournament = PvpTournament(pvp_config, output_dir=output_dir)
        tournament.run()
        metadata = tournament.get_metadata()
        return {"metadata": metadata, "pvp_config": pvp_config}
    finally:
        integration.unregister()

