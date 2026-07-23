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
from atv_bench.match_record import BudgetVector, MatchRecord, PlayerRecord
from atv_bench.run_envelope import RunError

ADAPTER_VERSION = "1.0.0"
# Which CLI binary each harness needs on PATH (no-fake preflight).
_HARNESS_BINARY = {"claude-code": "claude", "copilot-cli": "copilot", "codex": "codex"}
# Composite bare-control prefix (mirrors adapters.contract.BARE_PREFIX): `bare:<inner>` runs
# the SAME inner CLI under a stripped HOME, so it needs the inner harness's binary on PATH.
_BARE_PREFIX = "bare:"


def harness_binary_for(harness: str) -> str:
    """Resolve a harness key (leaf or ``bare:<inner>``) to the CLI binary it needs on PATH.

    The bare negative control runs the SAME model CLI as its inner harness (just under a
    stripped HOME), so ``bare:claude-code`` needs ``claude`` exactly like ``claude-code``.
    Raises ``KeyError`` if the (inner) harness is unknown.
    """
    inner = harness[len(_BARE_PREFIX):] if harness.startswith(_BARE_PREFIX) else harness
    return _HARNESS_BINARY[inner]


# ---------------------------------------------------------------------------
# Executor↔lift seam (PR #19 follow-up 1): a finished MatchRecord flows into the
# lift corpus end-to-end. The record's ``outcome['winner']`` is a harness key; the
# rating corpus wants a row of (harness_a, harness_b, model_a, model_b, score_a).
# ---------------------------------------------------------------------------

def match_record_to_rating_row(rec: MatchRecord) -> dict[str, Any]:
    """Convert a finished ``MatchRecord`` into a rating-corpus row.

    ``score_a`` is derived from the referee-authored ``outcome['winner']`` (a harness key),
    NOT bot stdout: 1.0 if player_a won, 0.0 if player_b won, 0.5 on an EXPLICIT tie/draw. The
    row carries the two players' harness + model tags so ``rating.matches_from_records`` (and in
    turn ``lift.compute_lift``) can consume it directly.

    Fails closed (raises ``ValueError``): a record without exactly two players, two players that
    share a harness key (winner attribution would be ambiguous), a MISSING or BLANK ``winner``
    (a malformed outcome is never silently scored as a draw — only an explicit ``tie``/``draw``
    token scores 0.5), or a winner that is neither player.
    """
    if len(rec.players) != 2:
        raise ValueError(
            f"match_record_to_rating_row needs exactly 2 players, got {len(rec.players)}"
        )
    a, b = rec.players[0], rec.players[1]
    ha, hb = (a.harness or "").strip(), (b.harness or "").strip()
    if not ha or not hb or ha == hb:
        raise ValueError(
            f"players must have distinct non-empty harness keys, got "
            f"({a.harness!r}, {b.harness!r}) — winner attribution would be ambiguous"
        )
    if "winner" not in rec.outcome:
        raise ValueError("outcome has no 'winner' key — malformed record, refusing to score")
    raw_winner = rec.outcome.get("winner")
    if raw_winner is None:
        raise ValueError("outcome 'winner' is None — malformed record, refusing to score")
    winner = str(raw_winner).strip()
    if not winner:
        raise ValueError("outcome 'winner' is blank — malformed record, refusing to score")
    if winner.lower() in {"tie", "draw"}:
        score_a = 0.5
    elif winner == ha:
        score_a = 1.0
    elif winner == hb:
        score_a = 0.0
    else:
        raise ValueError(
            f"outcome winner {winner!r} is neither player ({ha!r}, {hb!r})"
        )
    return {
        "harness_a": a.harness, "harness_b": b.harness,
        "model_a": a.model, "model_b": b.model,
        "score_a": score_a,
        # keep a stable identity for dedup by downstream consumers.
        "match_id": rec.outcome.get("match_id"),
        "game": rec.game, "game_version": rec.game_version,
    }


def append_rating_row(corpus_path, row: dict[str, Any]) -> None:
    """Append one rating-corpus row as a JSON line to ``corpus_path`` (creating it)."""
    import json

    p = Path(corpus_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def load_rating_rows(corpus_path) -> list[dict[str, Any]]:
    """Load rating-corpus rows from a JSONL file (empty list if it does not exist)."""
    import json

    p = Path(corpus_path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def persist_rating_row_from_record(rec: MatchRecord, corpus_path) -> dict[str, Any]:
    """Convert a finished record to a rating row and append it to the lift corpus.

    The single seam the CLI ``run --persist <path>`` calls after a live match, so a real
    match flows into ``compute_lift`` end-to-end. Returns the appended row.
    """
    row = match_record_to_rating_row(rec)
    append_rating_row(corpus_path, row)
    return row


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
            try:
                harness_binary_for(h)
            except KeyError:
                valid = ", ".join(sorted(_HARNESS_BINARY))
                raise RunError("usage",
                               f"unknown harness {h!r} for {side}. Valid: {valid} "
                               f"(or 'bare:<inner>' for the bare control).",
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
        binary = harness_binary_for(h)
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


def _manifest_tool_names(manifest: dict[str, Any]) -> list[str]:
    """Extract tool NAMES from a fingerprint manifest's `tools` list.

    The manifest emits tools as {name, source, enabled} dicts (probe._tool_entries);
    the row records the ordered, de-duplicated names. Tolerates a plain list of str
    for forward-compat. Never fabricates — missing/malformed → [].
    """
    raw = manifest.get("tools") or []
    names: list[str] = []
    seen: set[str] = set()
    for t in raw:
        name = t.get("name") if isinstance(t, dict) else t
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _budget_for(player_budgets: dict[str, Any] | None, harness: str) -> BudgetVector:
    """Build a BudgetVector from a per-harness budget dict (best-effort, no fabrication)."""
    if not player_budgets:
        return BudgetVector()
    b = player_budgets.get(harness)
    if isinstance(b, BudgetVector):
        return b
    if not isinstance(b, dict):
        return BudgetVector()
    return BudgetVector(
        tokens=b.get("tokens"),
        tool_calls=b.get("tool_calls"),
        wall_time_s=b.get("wall_time_s"),
    )


def build_match_record(
    cfg: RunConfig, *, outcome: dict[str, Any],
    player_models: dict[str, tuple[str, str]],
    player_fingerprints: dict[str, str],
    replay_path: str,
    player_manifests: dict[str, dict[str, Any]] | None = None,
    player_budgets: dict[str, Any] | None = None,
    verified: bool = False,
) -> MatchRecord:
    """Assemble the schema-v2 record. Phase 1 is verified=False → never publishes.

    `player_manifests` carries each harness's leak-safe fingerprint manifest so the
    moat surface (tools + nested_skills) is PERSISTED into the row (ENG-F) instead of
    dropped to []. `player_budgets` carries the per-harness budget vector (G10:
    tokens/tool_calls/wall_time_s) so outspending is disclosed.
    """
    from atv_bench.codeclash_env import CODECLASH_VERSION

    spec = resolve_game(cfg.game)
    manifests = player_manifests or {}
    players = []
    for h in (cfg.a, cfg.b):
        model, source = player_models.get(h, ("unknown", "parsed"))
        manifest = manifests.get(h) or {}
        players.append(PlayerRecord(
            harness=h,
            model=model,
            model_source=source,
            verified=verified,
            tools=_manifest_tool_names(manifest),
            nested_skills=list(manifest.get("nested_skills") or []),
            fingerprint_sha256=player_fingerprints.get(h, "0" * 64),
            adapter_version=ADAPTER_VERSION,
            budget=_budget_for(player_budgets, h),
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


def budget_from_usage(usage: Any | None) -> BudgetVector:
    """Map an adapter's measured ``Usage`` to a G10 ``BudgetVector`` (no fabrication).

    ``wall_time_s`` comes from ``Usage.seconds`` — the wall clock each adapter measures
    around its CLI subprocess (``elapsed = time.time() - start``), so in a real run it is
    a real number, not None. ``tokens`` is recorded only when the CLI actually reported it
    (Claude's ``modelUsage`` sum); a harness that emits no token payload leaves it 0, which
    we honestly surface as None. ``tool_calls`` is not measured by either adapter today, so
    it stays None rather than being faked from the build's turn count.
    """
    if usage is None:
        return BudgetVector()
    tokens = getattr(usage, "tokens", 0) or None
    seconds = getattr(usage, "seconds", None)
    wall = float(seconds) if seconds else None
    return BudgetVector(tokens=tokens, tool_calls=None, wall_time_s=wall)


def collect_player_budgets(cfg: RunConfig) -> dict[str, BudgetVector]:
    """Source each player's budget from the REAL adapter run, not from dead metadata.

    The build-once cache (``players._ARTIFACT_CACHE``) holds the authoritative
    ``AdapterResult`` from the ONE model-driven build per player, keyed by player name.
    Its ``.usage`` (tokens/seconds measured inside the adapter) IS the budget for that
    player in the match. We map cfg.a/cfg.b → their distinct player names → cached result
    → BudgetVector. A player with no cached build (never ran) yields an all-None vector.
    """
    from atv_bench.config import _distinct_names
    from atv_bench.players import _ARTIFACT_CACHE

    names = _distinct_names(cfg.a, cfg.b)
    # Index the build-once cache by player name (id), taking the first build per player.
    by_name: dict[str, Any] = {}
    for (player_id, _game, _pv), (_tree, result, _diff) in _ARTIFACT_CACHE.items():
        by_name.setdefault(player_id, result)
    out: dict[str, BudgetVector] = {}
    for harness, name in zip((cfg.a, cfg.b), names):
        result = by_name.get(name)
        usage = getattr(result, "usage", None) if result is not None else None
        out[harness] = budget_from_usage(usage)
    return out


def summarize_budgets(raw: dict, cfg: RunConfig) -> dict[str, BudgetVector]:
    """Per-harness budget vector (G10) sourced from the REAL adapter Usage.

    Previously this read ``metadata.budgets[harness]`` — a key NO producer (atv_bench or
    vendored CodeClash) ever writes, so every match recorded an all-None vector and G10
    was non-functional. The authoritative source is the build-once artifact cache: each
    player's single ``AdapterResult.usage`` carries the tokens/wall-clock the adapter
    measured. ``raw`` is accepted for call-site compatibility but no longer read.
    """
    return collect_player_budgets(cfg)



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
    from atv_bench.integration import BUILDER_HARNESSES
    from atv_bench.isolation import aa_lock
    from contextlib import nullcontext

    cfg.validate()
    preflight_or_raise(cfg)
    import_codeclash()
    integration.register()
    # Thread the per-harness config roots so each isolated HOME is seeded from the
    # right harness config (skills/plugins/MCP) instead of the shared host $HOME.
    integration.set_harness_homes(homes)
    try:
        pvp_config = build_pvp_config(
            game=cfg.game, a=cfg.a, b=cfg.b, model=cfg.model, rounds=cfg.rounds)
        from codeclash.tournaments.pvp import PvpTournament

        output_dir.mkdir(parents=True, exist_ok=True)
        tournament = PvpTournament(pvp_config, output_dir=output_dir)
        # A/A self-play: both sides drive the SAME builder harness. Serialize on a
        # per-(game, pair) filelock so two concurrent same-harness runs cannot
        # cross-contaminate a shared profile (per the isolation plan).
        is_aa = cfg.a == cfg.b and cfg.a in BUILDER_HARNESSES
        guard = aa_lock(cfg.game, (cfg.a, cfg.b)) if is_aa else nullcontext()
        with guard:
            tournament.run()
        metadata = tournament.get_metadata()
        return {"metadata": metadata, "pvp_config": pvp_config}
    finally:
        integration.unregister()

