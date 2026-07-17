"""`atv-bench run --demo` — pure recording playback (DX-6 walking skeleton).

Loads the committed real recording (see scripts/build_demo_recording.py), exposes it
as a schema-v2 match record with honest recording provenance, and renders a replay —
all with ZERO Docker/auth/network. This is the first thing a new user runs and the
first line of the README quickstart.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atv_bench.codeclash_env import CODECLASH_VERSION
from atv_bench.match_record import MatchRecord, PlayerRecord
from atv_bench.run_envelope import ok_envelope

_DATA = Path(__file__).resolve().parent / "data"
DEMO_RECORDING_PATH = str(_DATA / "demo_recording.json")

# Version tags for the recorded demo (frozen with the recording).
_GAME_VERSION = "lightcycles@1"
_PROMPT_VERSION = "edit@1"
_ADAPTER_VERSION = "1.0.0"


def _load_doc() -> dict[str, Any]:
    return json.loads(Path(DEMO_RECORDING_PATH).read_text())


def load_demo_record() -> MatchRecord:
    """The committed real recording as a schema-v2 MatchRecord (verified=false)."""
    doc = _load_doc()
    match = doc["match"]
    players_meta = doc["players"]
    players = [
        PlayerRecord(
            harness=p["harness"],
            model=p["model"],
            model_source="recording",  # honest: a recording, never a live parse
            verified=False,
            tools=[],
            nested_skills=[],
            fingerprint_sha256="0" * 64,  # placeholder: demo carries no live fingerprint
            adapter_version=_ADAPTER_VERSION,
        )
        for p in players_meta
    ]
    outcome = {
        "winner": _winner_label(match),
        "raw": match.get("outcome"),
        "turns": (match["frames"][-1]["turn"] if match.get("frames") else 0),
    }
    return MatchRecord(
        game=match.get("game", "lightcycles"),
        game_version=_GAME_VERSION,
        prompt_version=_PROMPT_VERSION,
        codeclash_version=CODECLASH_VERSION,
        rounds=1,
        outcome=outcome,
        replay_path="",  # filled in when the CLI writes the replay
        players=players,
        verified=False,
    )


def _winner_label(match: dict[str, Any]) -> str:
    o = match.get("outcome")
    if o in ("a_wins", "forfeit_b"):
        return match.get("player_a", "A")
    if o in ("b_wins", "forfeit_a"):
        return match.get("player_b", "B")
    return "draw"


def demo_match_result() -> dict[str, Any]:
    """The raw recorded match dict (frames etc.) for the replay renderer."""
    return _load_doc()["match"]


def demo_envelope(replay_path: str = "") -> dict[str, Any]:
    """Machine-readable envelope for `run --demo --json` (DX-1)."""
    rec = load_demo_record()
    d = rec.to_dict()
    # Default to the conventional replay location; the CLI overrides with the real path.
    d["replay_path"] = replay_path or "_demo_replay/index.html"
    d["next"] = (
        "This is a canned-but-REAL recorded match (verified=false, model_source="
        "recording). To run a live harness-vs-harness match: `atv-bench doctor` to "
        "check prerequisites, then `atv-bench run --game lightcycles --a copilot-cli "
        "--b claude-code --model <M>`."
    )
    return ok_envelope(d)
