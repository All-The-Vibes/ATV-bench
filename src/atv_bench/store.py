"""League data store (santa round-1 fix: publish must build a REAL board).

The store is a committed directory in the repo:

    league/
      submissions/<identity>.json   # one per entrant: bot metadata + fingerprint
      matches.jsonl                 # append-only match history (one JSON object/line)

The publish job reads the store and recomputes the leaderboard from full history —
deterministic, order-independent (see elo.py). This replaces the previous hardcoded
empty board. The match job appends a validated result to `matches.jsonl` via
`publish.ingest_result`.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atv_bench.elo import ForfeitReason, MatchResult, Outcome
from atv_bench.leaderboard import build_leaderboard_doc

_SUBMISSION_KEYS = {"identity", "game", "bot_sha256", "fingerprint", "pr_url", "logs_url"}
_MATCH_KEYS = {"player_a", "player_b", "outcome", "match_id"}


class LeagueStore:
    """Filesystem-backed store for submissions + match history."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.submissions_dir = self.root / "submissions"
        self.matches_file = self.root / "matches.jsonl"

    # --- submissions ---
    def add_submission(self, submission: dict[str, Any]) -> None:
        missing = _SUBMISSION_KEYS - set(submission)
        if missing:
            raise ValueError(f"submission missing keys: {sorted(missing)}")
        self.submissions_dir.mkdir(parents=True, exist_ok=True)
        identity = submission["identity"]
        if not isinstance(identity, str) or not identity.isascii() or "/" in identity or "\\" in identity:
            raise ValueError(f"unsafe identity: {identity!r}")
        path = self.submissions_dir / f"{identity}.json"
        path.write_text(json.dumps(submission, indent=2, sort_keys=True))

    def load_submissions(self) -> dict[str, dict[str, Any]]:
        subs: dict[str, dict[str, Any]] = {}
        if not self.submissions_dir.exists():
            return subs
        for f in sorted(self.submissions_dir.glob("*.json")):
            data = json.loads(f.read_text())
            identity = data.get("identity")
            # Anchor identity to the FILENAME stem: a submission's identity is bound to
            # its file path (league/submissions/<identity>.json), which the PR diff
            # attributes to an author. Rejecting a body whose identity != filename
            # stops a hand-edited mallory.json from claiming another entrant's identity
            # and overwriting their row.
            if identity != f.stem:
                raise ValueError(
                    f"submission identity {identity!r} does not match filename {f.name!r}; "
                    "identity must equal the file stem"
                )
            if identity in subs:
                raise ValueError(f"duplicate submission identity: {identity!r}")
            subs[identity] = data
        return subs

    # --- matches ---
    def append_match(self, match: dict[str, Any]) -> None:
        missing = _MATCH_KEYS - set(match)
        if missing:
            raise ValueError(f"match missing keys: {sorted(missing)}")
        self.root.mkdir(parents=True, exist_ok=True)
        with self.matches_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(match, sort_keys=True) + "\n")

    def load_matches(self) -> list[dict[str, Any]]:
        if not self.matches_file.exists():
            return []
        out = []
        for line in self.matches_file.read_text().splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out


def _to_match_result(m: dict[str, Any]) -> MatchResult:
    reason = m.get("forfeit_reason")
    return MatchResult(
        player_a=m["player_a"],
        player_b=m["player_b"],
        outcome=Outcome(m["outcome"]),
        forfeit_reason=ForfeitReason(reason) if reason else None,
        seed=int(m.get("seed", 0)),
        game=m.get("game", "battlesnake"),
        match_id=m["match_id"],
    )


def build_leaderboard_from_store(store_dir: str, *, updated_at: str) -> dict[str, Any]:
    """Recompute the full leaderboard document from the committed store."""
    store = LeagueStore(store_dir)
    submissions = store.load_submissions()
    matches = [_to_match_result(m) for m in store.load_matches()]
    return build_leaderboard_doc(matches, submissions, updated_at=updated_at)
