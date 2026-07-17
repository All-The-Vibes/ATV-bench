"""Build the committed demo recording (a REAL engine match, not synthetic scores).

Run once to (re)generate src/atv_bench/data/demo_recording.json:

    python scripts/build_demo_recording.py

The recording is a real deterministic lightcycles match between two DISTINCT bots,
labeled as the two showcase harnesses. Its model tags carry model_source=recording
and verified=false — honest provenance (ENG-13), never a publishable number.
"""
from __future__ import annotations

import json
from pathlib import Path

from atv_bench.play import Contestant, run_local_match

OUT = Path(__file__).resolve().parent.parent / "src" / "atv_bench" / "data" / "demo_recording.json"

# The two showcase harnesses. Distinct bots so the match is decided by PLAY, not RNG.
PLAYER_A = {"label": "atv-phoenix", "harness": "copilot-cli", "bot": "wall_hugger",
            "model": "claude-opus-4.8"}
PLAYER_B = {"label": "hve-core", "harness": "claude-code", "bot": "greedy",
            "model": "claude-opus-4.8"}


def main() -> None:
    result = run_local_match(
        game="lightcycles",
        player=Contestant(key=PLAYER_A["bot"], label=PLAYER_A["label"]),
        opponent=Contestant(key=PLAYER_B["bot"], label=PLAYER_B["label"]),
        seed=1,
    )
    doc = {
        "match": result,
        "players": [PLAYER_A, PLAYER_B],
        "provenance": {
            "kind": "recording",
            "note": "Real deterministic lightcycles engine match between two distinct "
                    "bots, labeled as the showcase harnesses. Model tags are recorded "
                    "provenance (model_source=recording), NOT a verified benchmark number.",
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=2))
    print(f"wrote {OUT} — outcome={result['outcome']} frames={len(result.get('frames') or [])}")


if __name__ == "__main__":
    main()
