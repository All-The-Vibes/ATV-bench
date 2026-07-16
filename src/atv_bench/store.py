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
import sys
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


def _reject_symlinked_ancestors(boundary: Path, stop_at: Path) -> None:
    """Fail closed if `boundary` or any path component up to `stop_at` is a symlink.

    Leaf-file confinement (santa round-2) confines record/main.py against a FIXED
    submissions_dir root — but if that root, or league/, or the store root ITSELF is a
    committed symlink (league/submissions -> ../payload), the "fixed" root is attacker-
    chosen and every downstream _within_dir check resolves against the target. Walk the
    boundary and every ancestor down to (and including) stop_at and reject any symlink,
    so the trusted publish path can never be re-pointed by a tracked directory symlink.
    """
    try:
        boundary = boundary.absolute()
        stop_at = stop_at.absolute()
    except OSError as e:
        raise ValueError(f"cannot resolve store boundary {boundary}: {e}") from e
    cur = boundary
    while True:
        if cur.is_symlink():
            raise ValueError(
                f"store boundary component {cur} is a symlink (escape rejected)"
            )
        if cur == stop_at or cur == cur.parent:
            break
        cur = cur.parent


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

    def add_submission(self, submission: dict[str, Any], *, bot_source: str | None = None) -> None:
        missing = _SUBMISSION_KEYS - set(submission)
        if missing:
            raise ValueError(f"submission missing keys: {sorted(missing)}")
        identity = submission["identity"]
        if not isinstance(identity, str) or not identity.isascii() or "/" in identity or "\\" in identity:
            raise ValueError(f"unsafe identity: {identity!r}")
        path = self._submission_path(identity)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(submission, indent=2, sort_keys=True))
        # A publishable row requires committed bot bytes (santa round-6): co-write the
        # sibling main.py so a store-seeded submission has the same publishable shape as a
        # live-submitted / match-job one. Its bytes back the re-derived bot_sha256 on load.
        (path.parent / "main.py").write_text(bot_source or "def move(state):\n    return 'up'\n")

    def load_submissions(self) -> dict[str, dict[str, Any]]:
        """Strict loader (validators): RAISE on the first malformed entrant.

        Anything invalid — symlink escape, malformed/oversize record, bad types, identity
        mismatch — fails closed with a controlled ValueError so a validator surfaces it.
        For the trusted board build, use load_submissions_quarantined() instead, which
        skips bad rows so one merged bad entrant can't abort the whole board.
        """
        subs: dict[str, dict[str, Any]] = {}
        if not self.submissions_dir.exists():
            return subs
        # Boundary confinement (santa round-3): reject a symlinked submissions_dir / store
        # root before trusting it as the containment anchor for leaf files below.
        _reject_symlinked_ancestors(self.submissions_dir, self.root)
        for d in sorted(p for p in self.submissions_dir.iterdir() if p.is_dir()):
            loaded = self._load_one_submission(d)
            if loaded is None:
                continue  # a partial tree (no record yet) is not an error
            identity, data = loaded
            if identity in subs:
                raise ValueError(f"duplicate submission identity: {identity!r}")
            subs[identity] = data
        return subs

    def load_submissions_quarantined(self) -> tuple[dict[str, dict[str, Any]], list[str]]:
        """Resilient loader (trusted board build, santa round-5): SKIP + diagnose bad rows.

        The strict loader raises on the first malformed entrant, which means one merged bad
        submission would abort the whole board build — an availability DoS (bricks Pages
        deploy, blocks future publishes). Here we quarantine each bad entrant directory:
        skip it, record a diagnostic, and still publish every valid row. Returns
        (valid_submissions, quarantine_diagnostics).
        """
        subs: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        if not self.submissions_dir.exists():
            return subs, errors
        try:
            _reject_symlinked_ancestors(self.submissions_dir, self.root)
        except ValueError as e:
            # A poisoned boundary is not row-local — nothing under it can be trusted.
            return subs, [str(e)]
        for d in sorted(p for p in self.submissions_dir.iterdir() if p.is_dir()):
            try:
                loaded = self._load_one_submission(d)
            except (ValueError, OSError) as e:
                errors.append(f"quarantined submission {d.name!r}: {e}")
                continue
            if loaded is None:
                continue
            identity, data = loaded
            if identity in subs:
                errors.append(f"quarantined duplicate submission identity: {identity!r}")
                continue
            subs[identity] = data
        return subs, errors

    def _load_one_submission(self, d: Path) -> tuple[str, dict[str, Any]] | None:
        """Load + fully validate a single entrant directory, or RAISE ValueError.

        Returns (identity, record) on success, or None if the directory has no record yet
        (a partial tree, not an error). Every trust check lives here so both the strict and
        the quarantining loaders share one validation path (no weaker second code path).
        """
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
            return None
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
        # Bind the PUBLISHED bot_sha256 to the actual committed bytes (santa round-7):
        # the row hash must be the hash of the sibling main.py, not the mutable
        # submission.json claim — else a contributor could display a hash that differs
        # from the bytes that earned the ELO. A publishable row therefore REQUIRES a real,
        # regular main.py (santa round-6): a submission.json-only entrant would otherwise
        # publish an attacker-claimed bot_sha256 with no backing bytes. Recompute from the
        # committed main.py and STAMP the trusted value over whatever the record claimed.
        bot_file = d / "main.py"
        if not bot_file.is_file():
            raise ValueError(
                f"submission {d.name!r} has no main.py; a publishable row requires the "
                "committed bot bytes its bot_sha256 is derived from"
            )
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
        return identity, data

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
        # Boundary confinement (santa round-3): reject a symlinked store root before it is
        # trusted as the containment anchor for the matches file.
        _reject_symlinked_ancestors(self.matches_file.parent, self.root)
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
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"malformed matches.jsonl line: {e}") from e
            # Semantic validation (santa round-3): JSON-syntax validity is not enough. A
            # well-formed line that is not a valid match record ([], {}, missing keys, bad
            # outcome enum) would crash the trusted build downstream (_to_match_result does
            # m["player_a"]/Outcome(m["outcome"])). Fail closed HERE with a controlled error.
            _validate_match_record(rec)
            out.append(rec)
        return out

    def load_matches_quarantined(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Resilient matches loader (trusted board build, santa round-5): skip + diagnose.

        Like load_matches but a malformed line (JSON-syntax OR semantic) is quarantined
        (skipped with a diagnostic) rather than aborting the whole board build. A poisoned/
        symlinked/oversized FILE is still fatal (not row-local), so those propagate.
        """
        good: list[dict[str, Any]] = []
        errors: list[str] = []
        if not self.matches_file.exists():
            return good, errors
        # File-level problems are fatal (not row-local): a symlinked/oversize file means the
        # file itself can't be trusted, so let these propagate rather than quarantine.
        _reject_symlinked_ancestors(self.matches_file.parent, self.root)
        if self.matches_file.is_symlink() or not _within_dir(self.matches_file, self.root):
            raise ValueError(
                "matches.jsonl is a symlink or resolves outside the store (escape rejected)"
            )
        text = _read_text_bounded(self.matches_file, _MAX_MATCHES_BYTES, "matches.jsonl")
        for idx, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                _validate_match_record(rec)
            except (ValueError, json.JSONDecodeError) as e:
                errors.append(f"quarantined matches.jsonl line {idx}: {e}")
                continue
            good.append(rec)
        return good, errors


def _validate_match_record(m: Any) -> None:
    """Fail closed if a committed matches.jsonl record is not a valid match.

    matches.jsonl is attacker-influenced (a merged PR commits it). A well-formed JSON line
    that is not a proper match record ([], {}, missing keys, non-string ids, unknown
    outcome/forfeit enum, non-int seed) must raise a controlled ValueError here, not crash
    the trusted board build downstream in _to_match_result / compute_leaderboard.
    """
    if not isinstance(m, dict):
        raise ValueError(f"matches.jsonl record is not a JSON object: {type(m).__name__}")
    missing = _MATCH_KEYS - set(m)
    if missing:
        raise ValueError(f"matches.jsonl record missing keys: {sorted(missing)}")
    for key in ("player_a", "player_b", "match_id"):
        if not isinstance(m[key], str) or not m[key]:
            raise ValueError(f"matches.jsonl record has invalid {key!r}")
    if m["outcome"] not in {o.value for o in Outcome}:
        raise ValueError(f"matches.jsonl record has invalid outcome {m['outcome']!r}")
    reason = m.get("forfeit_reason")
    if reason is not None and reason not in {r.value for r in ForfeitReason}:
        raise ValueError(f"matches.jsonl record has invalid forfeit_reason {reason!r}")
    # Cross-field invariant (santa round-4): mirror MatchResult.__post_init__ — a forfeit
    # outcome REQUIRES a reason, and a non-forfeit outcome must NOT carry one. Without this,
    # a well-formed line with a valid enum but inconsistent reason passes the loader then
    # raises in _to_match_result during the trusted build (board availability DoS).
    is_forfeit = m["outcome"] in (Outcome.FORFEIT_A.value, Outcome.FORFEIT_B.value)
    if is_forfeit and reason is None:
        raise ValueError(
            f"matches.jsonl forfeit record {m.get('match_id')!r} missing forfeit_reason"
        )
    if not is_forfeit and reason is not None:
        raise ValueError(
            f"matches.jsonl non-forfeit record {m.get('match_id')!r} carries a forfeit_reason"
        )
    seed = m.get("seed", 0)
    if not isinstance(seed, int) or isinstance(seed, bool):
        raise ValueError(f"matches.jsonl record has non-int seed {seed!r}")
    game = m.get("game", "battlesnake")
    if not isinstance(game, str):
        raise ValueError(f"matches.jsonl record has non-string game {game!r}")


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

    Resilience (santa round-5): a single malformed merged entrant/match must NOT abort the
    whole board build (that would let one bad community submission brick Pages deploy and
    block future publishes — an availability DoS). Bad rows are QUARANTINED (skipped and
    reported to stderr) so every valid row still publishes. Only a file-level poisoning
    (symlinked/oversized store file, symlinked boundary) — which is not row-local — stays
    fatal.
    """
    store = LeagueStore(store_dir)
    submissions, sub_errors = store.load_submissions_quarantined()
    match_records, match_errors = store.load_matches_quarantined()
    for diag in (*sub_errors, *match_errors):
        print(f"[leaderboard] {diag}", file=sys.stderr)
    seen: set[str] = set()
    matches = []
    for m in match_records:
        mid = m.get("match_id")
        # only dedup records that carry a match_id; a blank id can't collide meaningfully
        if isinstance(mid, str) and mid:
            if mid in seen:
                continue
            seen.add(mid)
        # A quarantined-clean record already passed _validate_match_record, but a match
        # referencing an entrant whose submission was quarantined is fine: compute_leaderboard
        # seeds unknown players. Defensive: skip any record that still fails MatchResult.
        try:
            matches.append(_to_match_result(m))
        except (ValueError, KeyError) as e:
            print(f"[leaderboard] quarantined match record {m.get('match_id')!r}: {e}",
                  file=sys.stderr)
    return build_leaderboard_doc(matches, submissions, updated_at=updated_at)
