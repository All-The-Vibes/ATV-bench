"""Publish-side entrypoint (trusted job): validate artifact, ingest, build board.

Deliberately imports NOTHING that executes a bot or the arena. It:
  - `validate`  : validate a match-result artifact against the result contract.
  - `ingest`    : append a validated result to the committed match history.
  - `build`     : recompute the leaderboard from the store and render the static site.

Invoked by the publish job in .github/workflows/league.yml. The board is built from
the committed `league/` store (submissions + match history), never hardcoded — a
regression to an empty/1970 board is caught by tests/test_publish.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from atv_bench.leaderboard import validate_leaderboard
from atv_bench.store import LeagueStore, build_leaderboard_from_store

# a match-result artifact must carry at least these keys.
_RESULT_REQUIRED_KEYS = {"status", "player_a", "player_b", "outcome", "match_id"}
_EPOCH = "1970-01-01T00:00:00Z"
_DEFAULT_STORE = "league"


def validate_artifact(path: str) -> dict[str, Any]:
    """Validate a match-result artifact. Raises on malformed/missing."""
    try:
        data = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"artifact is not valid JSON: {e}")
    if not isinstance(data, dict) or "status" not in data:
        raise ValueError("artifact must be an object with a 'status' key")
    # a crash/invalid record only needs status; a real result needs the match keys.
    if data.get("status") == "ok":
        missing = _RESULT_REQUIRED_KEYS - set(data)
        if missing:
            raise ValueError(f"ok result missing required keys {sorted(missing)}")
    return data


def ingest_result(path: str, *, store_dir: str = _DEFAULT_STORE) -> bool:
    """Append a validated OK match result to the store's history.

    Returns True if a match was appended, False for crash/invalid records (which are
    intentionally not scored as a match — a crashed run has no opponent outcome).
    """
    data = validate_artifact(path)
    if data.get("status") != "ok":
        return False
    store = LeagueStore(store_dir)
    store.append_match({
        "player_a": data["player_a"],
        "player_b": data["player_b"],
        "outcome": data["outcome"],
        "match_id": data["match_id"],
        "game": data.get("game", "battlesnake"),
        "seed": int(data.get("seed", 0)),
        **({"forfeit_reason": data["forfeit_reason"]} if data.get("forfeit_reason") else {}),
    })
    return True


def build_site(out_dir: str, *, store_dir: str = _DEFAULT_STORE, updated_at: str = _EPOCH) -> Path:
    """Render the static leaderboard site from the committed store.

    The board is computed from the store (submissions + match history). An empty
    store yields a schema-valid empty board (legitimate: no entrants yet).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = build_leaderboard_from_store(store_dir, updated_at=updated_at)
    validate_leaderboard(doc)
    (out / "leaderboard.json").write_text(json.dumps(doc, indent=2))
    view = Path(__file__).parent.parent.parent / "leaderboard" / "view" / "index.html"
    if view.exists():
        (out / "index.html").write_text(view.read_text())
    return out


def _arg(argv: list[str], flag: str, default: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv else default


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m atv_bench.publish {validate <a>|ingest <a>|build --out <d>}",
              file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "validate":
        validate_artifact(argv[1])
        print(f"artifact OK: {argv[1]}")
        return 0
    if cmd == "ingest":
        store = _arg(argv, "--store", _DEFAULT_STORE)
        appended = ingest_result(argv[1], store_dir=store)
        print("match appended" if appended else "crash/invalid record — not scored")
        return 0
    if cmd == "build":
        out = _arg(argv, "--out", "site")
        store = _arg(argv, "--store", _DEFAULT_STORE)
        updated = _arg(argv, "--updated-at", _EPOCH)
        p = build_site(out, store_dir=store, updated_at=updated)
        print(f"built site: {p}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
