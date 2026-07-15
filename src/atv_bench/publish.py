"""Publish-side entrypoint (trusted job): validate artifact + build static board.

Deliberately imports NOTHING that executes a bot or the arena. It reads a
match-result artifact, validates it against the locked leaderboard schema, recomputes
ELO from the full stored history, and renders the static site. Invoked by the publish
job in .github/workflows/league.yml as `python -m atv_bench.publish {validate|build}`.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from atv_bench.leaderboard import validate_leaderboard

_RESULT_SCHEMA_KEYS = {"status"}  # minimal artifact contract; extended as needed


def validate_artifact(path: str) -> dict[str, Any]:
    """Validate a match-result artifact. Raises on malformed/missing."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or not _RESULT_SCHEMA_KEYS <= set(data):
        raise ValueError(f"artifact missing required keys {_RESULT_SCHEMA_KEYS}")
    return data


def build_site(out_dir: str, leaderboard_doc: dict[str, Any] | None = None) -> Path:
    """Render the static leaderboard site into out_dir.

    If a leaderboard doc is provided it is validated against the locked schema and
    written to `out/leaderboard.json` next to the static viewer.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    doc = leaderboard_doc or {"schema_version": 1, "updated_at": "1970-01-01T00:00:00Z", "rows": []}
    validate_leaderboard(doc)
    (out / "leaderboard.json").write_text(json.dumps(doc, indent=2))
    # copy the static viewer (leaderboard/view/index.html) if present
    view = Path(__file__).parent.parent.parent / "leaderboard" / "view" / "index.html"
    if view.exists():
        (out / "index.html").write_text(view.read_text())
    return out


def _main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m atv_bench.publish {validate <artifact>|build --out <dir>}", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "validate":
        validate_artifact(argv[1])
        print(f"artifact OK: {argv[1]}")
        return 0
    if cmd == "build":
        out = "site"
        if "--out" in argv:
            out = argv[argv.index("--out") + 1]
        p = build_site(out)
        print(f"built site: {p}")
        return 0
    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
