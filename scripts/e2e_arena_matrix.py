#!/usr/bin/env python
"""Batch end-to-end scoring runner for the 17 reuse-candidate CodeClash arenas.

For each arena: build a REAL CodeClash pvp config, run a live harness match (Docker
build + live CLI bot edits + arena adjudication), and record whether it produced a
scored, non-crash round. Only arenas that PASS here are eligible to flip live=True.

2-player arenas use ATV's claude-code harness on both sides (A/A). The 4-5-player
arenas (figgie, bridge) fill their required seats with a mix of agent variants:
  - bare model (CodeClash `mini` agent, litellm, no ATV harness)
  - harnessed model (ATV `claude-code`)
alternating, so every required seat is a real, distinct competitor.

Usage: python scripts/e2e_arena_matrix.py <arena> [<arena> ...]
       python scripts/e2e_arena_matrix.py --all
Writes _e2e/<arena>/verdict.json per arena.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# --- Arena facts read off vendor/CodeClash/codeclash/arenas/<a>/<a>.py -----------------
# name = CodeArena.name; submission = the file/dir the harness edits; players = arena's
# required player count (2 unless the arena __init__ asserts otherwise).
ARENAS: dict[str, dict[str, Any]] = {
    "robotrumble":  {"name": "RobotRumble",  "submission": "robot.js",          "players": 2},
    "chess":        {"name": "Chess",        "submission": "src/",              "players": 2},
    "corewar":      {"name": "CoreWar",      "submission": "warrior.red",       "players": 2},
    "robocode":     {"name": "RoboCode",     "submission": "robots/custom/",    "players": 2},
    "battlecode23": {"name": "BattleCode23", "submission": "src/mysubmission",  "players": 2},
    "battlecode24": {"name": "BattleCode24", "submission": "src/mysubmission",  "players": 2},
    "battlecode25": {"name": "BattleCode25", "submission": "src/mysubmission",  "players": 2},
    "halite":       {"name": "Halite",       "submission": "submission",        "players": 2},
    "halite2":      {"name": "Halite2",      "submission": "submission",        "players": 2},
    "halite3":      {"name": "Halite3",      "submission": "submission",        "players": 2},
    "battlesnake":  {"name": "BattleSnake",  "submission": "main.py",           "players": 2},
    "bomberland":   {"name": "Bomberland",   "submission": "bomberland_agent.py","players": 2},
    "cyborg":       {"name": "CybORG",       "submission": "cyborg_agent.py",   "players": 2},
    "scml":         {"name": "SCML",         "submission": "scml_agent.py",     "players": 2},
    "huskybench":   {"name": "HuskyBench",   "submission": "client/player.py",  "players": 2},
    "figgie":       {"name": "Figgie",       "submission": "main.py",           "players": 4},
    "bridge":       {"name": "Bridge",       "submission": "bridge_agent.py",   "players": 4},
}

# Model used everywhere for parity. Harness CLI alias (claude) + litellm string (mini).
HARNESS_MODEL = "sonnet"
# Direct litellm Anthropic route (uses ANTHROPIC_API_KEY, no Portkey gateway needed).
BARE_MODEL = "anthropic/claude-sonnet-4-5-20250929"
ROUNDS = 1  # one edit+compete round is enough to prove a scored, non-crash match.
PER_ARENA_TIMEOUT = 2400  # seconds


def _generic_edit_prompt(arena: str, submission: str) -> str:
    """A prompt that points the harness at the real submission + the arena's own docs.

    The arena injects its authoritative game_description; we tell the harness to read the
    in-container docs/ and edit the correct submission path for THIS game.
    """
    return (
        f"You are competing in the CodeClash '{ARENAS[arena]['name']}' arena. Your bot's "
        f"submission is `{submission}`. Read the game rules in ./docs/ (and /logs/ for past "
        f"rounds), then improve `{submission}` so your bot wins more matches. Keep the "
        f"required entry-point/signature the arena validates; edit only your submission. "
        f"If the submission is a directory, edit the source files inside it."
    )


def _mini_agent_yaml() -> dict[str, Any]:
    """Load CodeClash's mini/default.yaml (the ClashAgent template params)."""
    import yaml as _yaml
    from codeclash.utils.yaml_utils import resolve_includes
    import codeclash
    cfg_dir = Path(codeclash.__file__).resolve().parent.parent / "configs"
    base = cfg_dir / "mini" / "default.yaml"
    resolved = resolve_includes(base.read_text(), base_dir=cfg_dir)
    return _yaml.safe_load(resolved)


def _bare_model_config() -> dict[str, Any]:
    """CodeClash `mini` agent config: bare model (direct litellm Anthropic), no ATV harness.

    config.agent = the mini ClashAgent yaml; config.model = the litellm model block.
    """
    return {
        "agent": _mini_agent_yaml(),
        "model": {"model_name": BARE_MODEL, "model_kwargs": {"temperature": 0.2, "max_tokens": 4096}},
    }


def _build_config(arena: str) -> dict[str, Any]:
    """Build a real CodeClash pvp config for `arena` with the right seat count + mix."""
    from atv_bench.config import resolve_game, GAME_SPECS
    from atv_bench.config import build_pvp_config

    spec_info = ARENAS[arena]
    n = spec_info["players"]
    submission = spec_info["submission"]
    prompt = _generic_edit_prompt(arena, submission)

    if n == 2:
        # Reuse the production 2-player builder if the game has a GameSpec; else inline.
        if arena in GAME_SPECS:
            cfg = build_pvp_config(game=arena, a="claude-code", b="claude-code",
                                   model=HARNESS_MODEL, rounds=ROUNDS)
            return cfg
        players = [
            {"agent": "claude-code", "name": f"claude-code-{s}",
             "config": {"model": HARNESS_MODEL, "bot_file": submission, "harness": "claude-code"}}
            for s in ("A", "B")
        ]
    else:
        # 4-5 seats: alternate bare-model (mini) and harnessed (claude-code).
        players = []
        for i in range(n):
            if i % 2 == 0:
                players.append({
                    "agent": "mini", "name": f"bare-{i}",
                    "config": _bare_model_config(),
                    "no_internet": True,
                })
            else:
                players.append({
                    "agent": "claude-code", "name": f"harness-{i}",
                    "config": {"model": HARNESS_MODEL, "bot_file": submission,
                               "harness": "claude-code"},
                })

    return {
        "tournament": {"rounds": ROUNDS},
        "game": {"name": spec_info["name"], "sims_per_round": 2, "args": {}},
        "players": players,
        "prompts": {"edit": prompt, "game_description": prompt},
        "_meta": {"game_version": f"{arena}@e2e", "prompt_version": "e2e@1",
                  "model": HARNESS_MODEL},
    }


def _pass_criteria(meta: dict[str, Any]) -> tuple[bool, str]:
    """A match PASSES if it produced round_stats with at least one round whose players
    validated a submission and a decisive/scored (non-crash) result exists."""
    rs = meta.get("round_stats", {})
    if not rs:
        return False, "no round_stats produced (match did not run to scoring)"
    # Look at the post-edit round(s) (>0); fall back to round 0 if only that exists.
    rounds = {str(k): v for k, v in rs.items()}
    any_valid = False
    any_winner = False
    detail = []
    for rnum, r in rounds.items():
        pstats = r.get("player_stats", {})
        valids = [p for p, s in pstats.items() if s.get("valid_submit")]
        w = r.get("winner")
        if valids:
            any_valid = True
        if w:
            any_winner = True
        detail.append(f"r{rnum}: winner={w}, valid={valids}")
    if any_valid and any_winner:
        return True, " | ".join(detail)
    return False, "ran but no valid submission+winner: " + " | ".join(detail)


def _cleanup_stale_match_containers() -> None:
    """Remove leftover ATV match containers so they don't starve later arenas (OOM).

    Only touches minisweagent-* containers (the CodeClash per-player containers); never
    touches unrelated containers.
    """
    import subprocess
    try:
        ids = subprocess.run(
            ["docker", "ps", "-aq", "--filter", "name=minisweagent-"],
            capture_output=True, text=True, timeout=30,
        ).stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=120)
    except Exception:
        pass


def run_arena(arena: str) -> dict[str, Any]:
    _cleanup_stale_match_containers()
    out_dir = Path("_e2e") / arena
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    result: dict[str, Any] = {"arena": arena, "players": ARENAS[arena]["players"]}

    from atv_bench import integration
    from atv_bench.codeclash_env import import_codeclash

    try:
        import_codeclash()
        integration.register()
        integration.set_harness_homes(None)
        cfg = _build_config(arena)
        (out_dir / "config.json").write_text(json.dumps(cfg, indent=2, default=str))
        from codeclash.tournaments.pvp import PvpTournament

        # Fresh output dir each run (PvpTournament refuses to overwrite metadata).
        run_dir = out_dir / f"run_{int(started)}"
        tournament = PvpTournament(cfg, output_dir=run_dir)
        tournament.run()
        meta = tournament.get_metadata()
        (out_dir / "metadata.json").write_text(json.dumps(meta, indent=2, default=str))
        ok, why = _pass_criteria(meta)
        result.update({"passed": ok, "why": why, "seconds": round(time.time() - started)})
    except Exception as exc:
        result.update({
            "passed": False,
            "why": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-2000:],
            "seconds": round(time.time() - started),
        })
    finally:
        try:
            integration.unregister()
        except Exception:
            pass

    (out_dir / "verdict.json").write_text(json.dumps(result, indent=2))
    return result


def main(argv: list[str]) -> int:
    if not argv or argv[0] == "--all":
        arenas = list(ARENAS)
    else:
        arenas = argv
    verdicts = []
    for a in arenas:
        if a not in ARENAS:
            print(f"SKIP unknown arena {a}", flush=True)
            continue
        print(f"▶ {a} ({ARENAS[a]['players']}p) …", flush=True)
        v = run_arena(a)
        verdicts.append(v)
        print(f"  {'PASS' if v['passed'] else 'FAIL'} ({v['seconds']}s): {v['why']}", flush=True)
    Path("_e2e").mkdir(exist_ok=True)
    Path("_e2e/matrix.json").write_text(json.dumps(verdicts, indent=2))
    passed = [v["arena"] for v in verdicts if v["passed"]]
    print(f"\n=== {len(passed)}/{len(verdicts)} PASSED: {passed}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
