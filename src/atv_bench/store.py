"""League data store (santa round-1 fix: publish must build a REAL board).

The store is a committed directory in the repo:

    league/
      submissions/<identity>/submission.json   # one dir per entrant: metadata + fingerprint
      submissions/<identity>/main.py            # the harness-built bot (match job reads)
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
    #
    # Canonical on-disk layout (F1, santa round-1): ONE directory per entrant, shared
    # by the match job, the live-submit writer, and this reader:
    #
    #     league/submissions/<identity>/main.py          # bot (match job reads)
    #     league/submissions/<identity>/submission.json  # record (this reader)
    #
    # The previous flat `submissions/<identity>.json` layout was invisible to the nested
    # tree that `submit --live` and the match job both use, so a live entrant was scored
    # but never appeared on the board. Identity is anchored to the PARENT DIRECTORY name,
    # preserving the spoof protection the flat stem-check provided.
    _RECORD_FILENAME = "submission.json"

    def _submission_path(self, identity: str) -> Path:
        return self.submissions_dir / identity / self._RECORD_FILENAME

    def add_submission(self, submission: dict[str, Any]) -> None:
        missing = _SUBMISSION_KEYS - set(submission)
        if missing:
            raise ValueError(f"submission missing keys: {sorted(missing)}")
        identity = submission["identity"]
        if not isinstance(identity, str) or not identity.isascii() or "/" in identity or "\\" in identity:
            raise ValueError(f"unsafe identity: {identity!r}")
        path = self._submission_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(submission, indent=2, sort_keys=True))

    def load_submissions(self) -> dict[str, dict[str, Any]]:
        subs: dict[str, dict[str, Any]] = {}
        if not self.submissions_dir.exists():
            return subs
        for d in sorted(p for p in self.submissions_dir.iterdir() if p.is_dir()):
            record = d / self._RECORD_FILENAME
            if not record.is_file():
                # A bot directory without a record is not yet a scored submission
                # (e.g. a partial tree). Skip it rather than crash the whole board.
                continue
            # H3 (santa round-3): a committed submission.json is hand-editable. Fail CLOSED
            # on malformed content — invalid JSON or a record missing required keys must
            # raise a controlled ValueError here, NOT surface later as an uncaught
            # JSONDecodeError/KeyError deep inside trusted board generation (a DoS on the
            # whole board). We do not silently skip either — a maintainer must see + fix it.
            try:
                data = json.loads(record.read_text())
            except json.JSONDecodeError as e:
                raise ValueError(f"malformed submission.json for {d.name!r}: {e}") from e
            if not isinstance(data, dict):
                raise ValueError(f"submission record for {d.name!r} is not a JSON object")
            missing = _SUBMISSION_KEYS - set(data)
            if missing:
                raise ValueError(
                    f"submission record for {d.name!r} missing required keys: {sorted(missing)}"
                )
            # Nested-type validation (santa round-4): top-level key presence is not enough.
            # A wrong-TYPED nested field (fingerprint as a string, unknown/skills/mcps/plugins
            # as a scalar) crashed trusted board generation with an uncaught AttributeError/
            # TypeError. Fail closed HERE so a malformed merged record can never DoS the board.
            fp = data.get("fingerprint")
            if not isinstance(fp, dict):
                raise ValueError(
                    f"submission record for {d.name!r} has a non-object fingerprint"
                )
            for list_field in ("skills", "mcps", "plugins", "unknown"):
                if list_field in fp and not isinstance(fp[list_field], list):
                    raise ValueError(
                        f"submission record for {d.name!r} fingerprint.{list_field} "
                        f"must be a list, got {type(fp[list_field]).__name__}"
                    )
            identity = data.get("identity")
            # Anchor identity to the DIRECTORY name: a submission's identity is bound to
            # its path (league/submissions/<identity>/), which the PR diff attributes to
            # an author. Rejecting a body whose identity != directory stops a hand-edited
            # mallory/submission.json from claiming another entrant's identity.
            if identity != d.name:
                raise ValueError(
                    f"submission identity {identity!r} does not match directory {d.name!r}; "
                    "identity must equal the submission directory name"
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
        # Write-time dedup: a re-ingest of an existing match_id is a no-op, so history
        # stays a set of distinct matches rather than accumulating duplicate lines
        # forever. Recompute also dedups (defense in depth) but this keeps the store lean.
        mid = match.get("match_id")
        if isinstance(mid, str) and mid:
            for existing in self.load_matches():
                if existing.get("match_id") == mid:
                    return
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
    """Recompute the full leaderboard document from the committed store.

    Dedup by match_id so recompute is idempotent: if the same match_id ever appears
    twice in history (e.g. a re-ingest of the same artifact), it is counted once. First
    occurrence wins; a stable append order keeps this deterministic. Note match_id is
    the workflow's stable github.run_id, so a publish re-run reuses the same id and does
    not double-count; a genuinely new run gets a new id and is a distinct match.
    """
    store = LeagueStore(store_dir)
    submissions = store.load_submissions()
    seen: set[str] = set()
    matches = []
    for m in store.load_matches():
        mid = m.get("match_id")
        # only dedup records that carry a match_id; a blank id can't collide meaningfully
        if isinstance(mid, str) and mid:
            if mid in seen:
                continue
            seen.add(mid)
        matches.append(_to_match_result(m))
    return build_leaderboard_doc(matches, submissions, updated_at=updated_at)
