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
import hashlib
import re
from pathlib import Path
from typing import Any

from atv_bench.elo import ForfeitReason, MatchResult, Outcome
from atv_bench.leaderboard import build_leaderboard_doc

_SUBMISSION_KEYS = {"identity", "game", "bot_sha256", "fingerprint", "pr_url", "logs_url"}
# Matches the leaderboard schema's bot_sha256 pattern (64 lowercase hex).
_SHA256_RE = re.compile(r"[a-f0-9]{64}")
# A GitHub-login-shaped identity: 1-39 chars, alphanumeric with single internal hyphens.
# The trusted publish path derives identity from the submission directory name, so this
# guards a crafted directory (unicode/space/punctuation) from publishing an odd identity.
_IDENTITY_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
_MATCH_KEYS = {"player_a", "player_b", "outcome", "match_id"}

# Bounded reads on trusted-path inputs (santa round-2): submission.json / main.py /
# matches.jsonl are all attacker-influenced (a merged PR commits them). Reading an
# unbounded file into memory before validation lets an oversized input OOM the trusted
# build. Cap each read and treat oversize as a controlled ValueError, not a MemoryError.
_MAX_RECORD_BYTES = 256 * 1024        # submission.json: metadata, kilobytes at most
_MAX_BOT_BYTES = 1024 * 1024          # main.py: a bot script, ~1 MiB ceiling
_MAX_MATCHES_BYTES = 32 * 1024 * 1024  # matches.jsonl: full history, generous but bounded


def _read_text_bounded(path: Path, limit: int, label: str) -> str:
    """Read at most `limit` bytes of text; fail closed if the file exceeds it.

    Guards the trusted build against an oversized attacker-controlled file exhausting
    memory before validation runs. Reads limit+1 and rejects if the extra byte exists.
    """
    return _read_bytes_bounded(path, limit, label).decode("utf-8", errors="strict")


def _read_bytes_bounded(path: Path, limit: int, label: str) -> bytes:
    """Read at most `limit` bytes; fail closed (ValueError) if the file exceeds it."""
    with path.open("rb") as fh:
        blob = fh.read(limit + 1)
    if len(blob) > limit:
        raise ValueError(f"{label} exceeds the {limit}-byte limit (oversize rejected)")
    return blob


def _within_dir(path: Path, root: Path) -> bool:
    """True iff the fully-resolved `path` stays inside the resolved `root`.

    A committed submission is UNTRUSTED input. If submission.json or main.py is a symlink
    pointing outside the submission tree, following it would let a crafted PR make the
    trusted board build read arbitrary host files (info leak / deploy-time DoS). We resolve
    both and confine, mirroring fingerprint/reader.py's _within_root posture.
    """
    try:
        rp = path.resolve()
        rr = root.resolve()
    except OSError:
        return False
    return rp == rr or rr in rp.parents


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
            # Directory-symlink confinement (santa round-2, both reviewers CRITICAL):
            # iterdir()+is_dir() FOLLOWS a directory symlink, so a git-tracked
            # `submissions/alice -> ../payload` would resolve its files inside the target
            # and pass a per-`d` containment check. Reject any symlinked entrant directory,
            # and confine every file against the FIXED submissions_dir root (not the
            # attacker-influenced `d`), so no escape survives.
            if d.is_symlink() or not _within_dir(d, self.submissions_dir):
                raise ValueError(
                    f"submission directory {d.name!r} is a symlink or resolves outside "
                    "the submissions tree (escape rejected)"
                )
            record = d / self._RECORD_FILENAME
            if not record.is_file():
                # A bot directory without a record is not yet a scored submission
                # (e.g. a partial tree). Skip it rather than crash the whole board.
                continue
            # Symlink confinement (santa dual-review): a committed submission.json is
            # untrusted. Reject a record that is a symlink or resolves outside the
            # submissions tree so a crafted symlink can't read arbitrary host files.
            if record.is_symlink() or not _within_dir(record, self.submissions_dir):
                raise ValueError(
                    f"submission record for {d.name!r} resolves outside the submissions "
                    "tree (symlink escape rejected)"
                )
            # H3 (santa round-3): a committed submission.json is hand-editable. Fail CLOSED
            # on malformed content — invalid JSON or a record missing required keys must
            # raise a controlled ValueError here, NOT surface later as an uncaught
            # JSONDecodeError/KeyError deep inside trusted board generation (a DoS on the
            # whole board). We do not silently skip either — a maintainer must see + fix it.
            try:
                data = json.loads(
                    _read_text_bounded(record, _MAX_RECORD_BYTES, f"submission.json for {d.name!r}")
                )
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
            # Top-level published scalars (santa round-6): a wrong-typed/malformed
            # bot_sha256/pr_url/logs_url passed load then crashed the trusted build deep in
            # schema validation (availability DoS). Validate them HERE so a bad merged record
            # fails closed with a clear per-record message, matching the leaderboard schema
            # patterns (bot_sha256 = 64 lowercase hex; urls = http(s)).
            sha = data.get("bot_sha256")
            if not isinstance(sha, str) or not _SHA256_RE.fullmatch(sha):
                raise ValueError(
                    f"submission record for {d.name!r} has an invalid bot_sha256 "
                    "(must be 64 lowercase hex chars)"
                )
            for url_field in ("pr_url", "logs_url"):
                url = data.get(url_field)
                if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                    raise ValueError(
                        f"submission record for {d.name!r} has an invalid {url_field} "
                        "(must be an http(s) URL)"
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
            # Validate identity is a GitHub-login-shaped slug (santa round-8): the trusted
            # path derives it from the directory name, so a crafted dir (spaces, unicode,
            # punctuation) must not publish an odd identity into a public row.
            if not _IDENTITY_RE.fullmatch(identity):
                raise ValueError(
                    f"submission identity {identity!r} is not a valid GitHub-login slug "
                    "(1-39 alphanumerics with single internal hyphens)"
                )
            if identity in subs:
                raise ValueError(f"duplicate submission identity: {identity!r}")
            # Bind the PUBLISHED bot_sha256 to the actual committed bytes (santa round-7):
            # the row hash must be the hash of the sibling main.py, not the mutable
            # submission.json claim — else a contributor could display a hash that differs
            # from the bytes that earned the ELO. Recompute from main.py when present and
            # STAMP the trusted value over whatever the record claimed.
            bot_file = d / "main.py"
            if bot_file.is_file():
                # Symlink confinement (santa dual-review): refuse to read/hash a main.py
                # that is a symlink or resolves outside the submissions tree.
                if bot_file.is_symlink() or not _within_dir(bot_file, self.submissions_dir):
                    raise ValueError(
                        f"bot file for {d.name!r} resolves outside the submissions tree "
                        "(symlink escape rejected)"
                    )
                # Bounded read (santa round-2): an oversized main.py must fail closed, not
                # OOM the trusted build, before it is hashed into the published row.
                bot_bytes = _read_bytes_bounded(bot_file, _MAX_BOT_BYTES, f"main.py for {d.name!r}")
                trusted_sha = hashlib.sha256(bot_bytes).hexdigest()
                if data.get("bot_sha256") != trusted_sha:
                    data = {**data, "bot_sha256": trusted_sha}
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
        # Symlink confinement (santa round-2): never append through a symlinked matches
        # file into arbitrary host content, even when dedup was skipped (empty match_id).
        if self.matches_file.exists() and (
            self.matches_file.is_symlink() or not _within_dir(self.matches_file, self.root)
        ):
            raise ValueError(
                "matches.jsonl is a symlink or resolves outside the store (escape rejected)"
            )
        with self.matches_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(match, sort_keys=True) + "\n")

    def load_matches(self) -> list[dict[str, Any]]:
        if not self.matches_file.exists():
            return []
        # Symlink confinement (santa round-2): matches.jsonl is committed into the store by
        # a merged PR. Refuse to follow a symlinked matches file into arbitrary host content
        # (the trusted read/append path must stay inside the store root).
        if self.matches_file.is_symlink() or not _within_dir(self.matches_file, self.root):
            raise ValueError(
                "matches.jsonl is a symlink or resolves outside the store (escape rejected)"
            )
        # Bounded read: an oversized history file must fail closed, not OOM the build.
        text = _read_text_bounded(self.matches_file, _MAX_MATCHES_BYTES, "matches.jsonl")
        out = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # Per-line fail-closed: a malformed line must raise a controlled ValueError,
            # not surface as an uncaught JSONDecodeError deep in trusted board generation.
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"malformed matches.jsonl line: {e}") from e
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
