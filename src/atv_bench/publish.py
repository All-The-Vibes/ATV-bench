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

from atv_bench.elo import ForfeitReason, Outcome
from atv_bench.leaderboard import validate_leaderboard
from atv_bench.store import LeagueStore, build_leaderboard_from_store

_VALID_STATUSES = {"ok", "crash", "invalid_output"}
_FORFEIT_OUTCOMES = {Outcome.FORFEIT_A.value, Outcome.FORFEIT_B.value}
_OK_REQUIRED_KEYS = {"player_a", "player_b", "outcome", "match_id"}
_CRASH_REQUIRED_KEYS = {"loser", "opponent", "match_id"}
_EPOCH = "1970-01-01T00:00:00Z"
_DEFAULT_STORE = "league"


def _require_nonempty_str(data: dict[str, Any], key: str) -> None:
    v = data.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{key} must be a non-empty string, got {v!r}")


def validate_artifact(path: str) -> dict[str, Any]:
    """Validate a match-result artifact — FAIL-CLOSED at the trust boundary.

    An untrusted bot's run produced this. We reject anything that isn't one of the
    known result shapes with correctly-TYPED fields BEFORE it can reach the store or
    the ELO engine. A non-string match_id/player would otherwise be committed and then
    crash the trusted recompute with a TypeError (poison-the-trusted-job).
    """
    try:
        data = json.loads(Path(path).read_text())
    except (json.JSONDecodeError, ValueError) as e:
        raise ValueError(f"artifact is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("artifact must be a JSON object")
    status = data.get("status")
    if status not in _VALID_STATUSES:
        raise ValueError(f"status must be one of {sorted(_VALID_STATUSES)}, got {status!r}")
    if status == "ok":
        missing = _OK_REQUIRED_KEYS - set(data)
        if missing:
            raise ValueError(f"ok result missing keys {sorted(missing)}")
        _require_nonempty_str(data, "player_a")
        _require_nonempty_str(data, "player_b")
        _require_nonempty_str(data, "match_id")
        outcome = data["outcome"]
        if outcome not in {o.value for o in Outcome}:
            raise ValueError(f"invalid outcome {outcome!r}")
        if outcome in _FORFEIT_OUTCOMES:
            reason = data.get("forfeit_reason")
            if reason not in {r.value for r in ForfeitReason}:
                raise ValueError(f"forfeit outcome requires a valid forfeit_reason, got {reason!r}")
        if "seed" in data and not isinstance(data["seed"], int):
            raise ValueError(f"seed must be an integer, got {data['seed']!r}")
    elif status in ("crash", "invalid_output"):
        missing = _CRASH_REQUIRED_KEYS - set(data)
        if missing:
            raise ValueError(f"{status} record missing keys {sorted(missing)} "
                             "(loser+opponent needed to score the forfeit)")
        _require_nonempty_str(data, "loser")
        _require_nonempty_str(data, "opponent")
        _require_nonempty_str(data, "match_id")
    return data


def ingest_result(path: str, *, store_dir: str = _DEFAULT_STORE) -> bool:
    """Append a validated result to the store's history.

    An `ok` result is appended as-is. A `crash`/`invalid_output` record is scored as a
    FORFEIT LOSS for the crashing player (reason CRASH) — never dropped, because a
    dropped forfeit skews everyone's ELO (the forfeit=loss+reason claim). Returns True
    when a match was appended.
    """
    data = validate_artifact(path)
    store = LeagueStore(store_dir)
    if data["status"] == "ok":
        match = {
            "player_a": data["player_a"],
            "player_b": data["player_b"],
            "outcome": data["outcome"],
            "match_id": data["match_id"],
            "game": data.get("game", "battlesnake"),
            "seed": int(data.get("seed", 0)),
        }
        if data.get("forfeit_reason"):
            match["forfeit_reason"] = data["forfeit_reason"]
        store.append_match(match)
        return True
    # crash / invalid_output -> forfeit loss for the crasher
    loser, opponent = data["loser"], data["opponent"]
    store.append_match({
        "player_a": opponent,
        "player_b": loser,
        "outcome": Outcome.FORFEIT_B.value,   # player_b (loser) forfeited
        "forfeit_reason": ForfeitReason.CRASH.value,
        "match_id": data["match_id"],
        "game": data.get("game", "battlesnake"),
        "seed": int(data.get("seed", 0)),
    })
    return True


def _normalize_utc_z(ts: str) -> str:
    """Normalize an ISO-8601 timestamp to the schema's required `...Z` UTC form.

    git %cI emits a numeric offset (e.g. 2026-07-15T15:36:06-05:00, or +00:00 on a
    UTC runner). The leaderboard schema requires a `Z` suffix, so build_site must
    convert; otherwise validate_leaderboard raises on every real publish run.
    """
    from datetime import datetime, timezone
    s = ts.strip()
    if s.endswith("Z") and "+" not in s:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        # unparseable -> fall back to epoch (schema-valid, obviously-stale)
        return _EPOCH


def build_site(out_dir: str, *, store_dir: str = _DEFAULT_STORE, updated_at: str = _EPOCH) -> Path:
    """Render the static leaderboard site from the committed store.

    The board is computed from the store (submissions + match history). An empty
    store yields a schema-valid empty board (legitimate: no entrants yet). The
    timestamp is normalized to UTC `Z` so a git-commit-time input validates.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = build_leaderboard_from_store(store_dir, updated_at=_normalize_utc_z(updated_at))
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
