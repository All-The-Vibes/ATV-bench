"""Contributor validation tools (devex T6).

`validate-harness` and `validate-game` are the ecosystem on-ramp: a contributor runs
them locally before opening a PR, so a broken harness reader or an unsafe bot fails on
their machine, not in review. Both reuse the same leak-safe scanner + shape guards the
production path uses — no second, weaker code path.
"""
from __future__ import annotations

import posixpath
import re
from typing import Any

from atv_bench.fingerprint import reader
from atv_bench.fingerprint.probe import FINGERPRINT_SCHEMA_KEYS
from atv_bench.fingerprint.scan import _has_secret_pattern, is_safe_name, is_secret
from atv_bench.submit import validate_bot_shape
from atv_bench.errors import AtvError

# A GitHub-login-shaped author (same shape store.py anchors identities to).
_AUTHOR_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
# The only two files a community PR is allowed to add/modify, under its own directory.
_ALLOWED_SUBMISSION_FILES = {"main.py", "submission.json"}


def validate_pr_paths(author: str, changed_paths: list[str]) -> dict[str, Any]:
    """Confine a community PR to its OWN submission tree (santa round-4).

    Runtime scoring is workflow-pinned to the PR author, but the durable leaderboard is
    rebuilt from committed files. A merged PR that edits league/matches.jsonl directly, or
    writes into another entrant's directory, could forge history or poison another row.
    This gate rejects ANY changed path outside league/submissions/<author>/{main.py,
    submission.json}, so CI can fail closed and block the PR before merge. Repo-plumbing
    PRs (workflows, src, docs) are expected to come from maintainers and run without this
    community gate; it is applied only to untrusted community submission PRs.
    """
    errors: list[str] = []
    if not isinstance(author, str) or not _AUTHOR_RE.fullmatch(author):
        return {"ok": False, "errors": [f"invalid PR author login: {author!r}"]}
    allowed_dir = f"league/submissions/{author}"
    for raw in changed_paths:
        if not isinstance(raw, str) or not raw:
            errors.append(f"invalid changed path: {raw!r}")
            continue
        # Normalize to catch traversal/./ tricks, then require the exact canonical shape.
        norm = posixpath.normpath(raw)
        if norm != raw or norm.startswith("/") or ".." in norm.split("/"):
            errors.append(f"unsafe changed path: {raw!r}")
            continue
        parent, name = posixpath.split(norm)
        if parent != allowed_dir or name not in _ALLOWED_SUBMISSION_FILES:
            errors.append(
                f"path {raw!r} is outside {allowed_dir}/"
                f"{{{','.join(sorted(_ALLOWED_SUBMISSION_FILES))}}}"
            )
    return {"ok": not errors, "errors": errors}


_SUBMISSIONS_PREFIX = "league/submissions/"
# League state (submissions, matches.jsonl, …) — renames/deletes here are never allowed.
_LEAGUE_PREFIX = "league/"


# `git diff --name-status` statuses that a PR may carry without tripping the R/C/D gate.
# Anything not in this set and not in _BLOCKED_STATUS_CODES is UNRECOGNIZED and fails
# closed (U=unmerged, X="bug in git", B=broken pairing all land here). Letting an unknown
# code fall through to ALLOW would mean any future/odd status silently skips the gate.
_ALLOWED_STATUS_CODES = frozenset({"A", "M", "T"})
_BLOCKED_STATUS_CODES = frozenset({"R", "C", "D"})
# Statuses are matched as EXACT tokens, never by first character. `status[:1]` treats
# `MALFORMED` as a plain modify, so `MALFORMED\tleague/submissions/x/main.py` sailed
# through a fail-closed gate. A/M/T/D/U/X/B stand alone; only rename/copy carry a
# similarity score, which git writes as 0-100 (R100, C75) — never R999. Padding is not
# accepted either: git emits no surrounding whitespace, so `A ` is not a status git wrote.
_STATUS_RE = re.compile(r"(?:[AMTD]|[RC](?:100|[0-9]{1,2})?)\Z")

# Escapes git emits inside a C-quoted path, per quote_c_style() in quote.c.
_C_ESCAPES = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
              "\\": 92, '"': 34}


def _decode_status_path(path: str) -> str:
    """Decode one raw `git diff --name-status` path field to the real path it names.

    With core.quotepath at its DEFAULT (on), git wraps any path containing non-ASCII or
    control bytes in double quotes and octal-escapes those bytes:

        D\t"league/submissions/caf\\303\\251/main.py"

    That literal string is what ci.yml pipes into this guard, and it compares equal to
    NOTHING: the leading quote defeats the league/ prefix test AND `_is_submission_path`,
    so the PR was not even classified as a submission PR and the normalizing validator
    never ran. A GitHub *login* cannot carry an accent (_AUTHOR_RE is ASCII-only), but a
    *path* can — it is created by the filesystem, and any ASCII attacker can name one.

    Decoding (rather than merely stripping the quotes, which would leave the literal
    text `caf\\303\\251`) restores the true path, so every downstream check — the gate,
    the submission classifier, and validate_pr_paths — sees the same bytes git does.
    """
    if not isinstance(path, str):
        return ""
    # Only the C-quoted wrapper is unwrapped here — the path itself is NOT stripped.
    # A leading/trailing space is a legal filename character that git emits verbatim, so
    # trimming it would let `main.py ` (a DIFFERENT file) satisfy the {main.py,
    # submission.json} allowlist. Framing whitespace is handled by the record splitter.
    p = path
    if not (len(p) >= 2 and p.startswith('"') and p.endswith('"')):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out.extend(ch.encode("utf-8", "surrogateescape"))
            i += 1
            continue
        nxt = body[i + 1] if i + 1 < len(body) else ""
        oct3 = body[i + 1:i + 4]
        if nxt in _C_ESCAPES:
            out.append(_C_ESCAPES[nxt])
            i += 2
        elif len(oct3) == 3 and all(c in "01234567" for c in oct3):
            # Octal only — `int(oct3, 8)` would raise on a digit 8/9, and a crashing
            # guard is a worse failure mode than a conservatively-decoded path.
            out.append(int(oct3, 8) & 0xFF)
            i += 4
        else:
            # Not a git-emitted escape; keep the backslash literally rather than
            # silently dropping bytes out of a path a security gate is about to judge.
            out.extend(ch.encode("utf-8", "surrogateescape"))
            i += 1
    return out.decode("utf-8", "surrogateescape")


def _normalize_status_path(path: str) -> str:
    """Decode + fold ONE raw name-status path into the form every check compares against.

    This is the single normalization boundary, and BOTH the league/** gate and the
    submission-PR classifier must go through it. Normalizing only the gate leaves the
    classifier fail-open, which is a pwn-request hole: a PR spelled
    `A\tLeague/submissions/me/main.py` + `M\t.github/workflows/ci.yml` was not classified
    as a submission PR at all, so confinement never ran and it could rewrite the very
    workflow that scores it.

    Collapsing is done with posixpath.normpath, not by peeling a literal `./` prefix:
    peeling handles `./league/…` but NOT `.//league/…`, `././league/…`, or `/league/…`,
    each of which names the same tree. Leading slashes are then dropped so an absolute
    spelling is still recognized as league. Recognizing MORE paths as league/submission
    is the fail-closed direction — it can only pull a PR INTO the gate, never out of it.

    Casefolding is a DELIBERATE trade-off, not an oversight. On a case-insensitive
    checkout (macOS/Windows) `League/matches.jsonl` and `league/matches.jsonl` are one
    file, so a case-sensitive compare is a confirmed bypass of this gate. The cost is
    that a genuinely distinct `LEAGUE/` tree on a case-sensitive runner would also be
    gated. No such tree exists in this repo (`league/` is the only one), and the
    consequence of a false positive here is a maintainer PR needing review, versus a
    false negative silently admitting forged league history. If a distinct `LEAGUE/`
    tree is ever added, revisit this — but the correct fix then is to not create it.
    """
    if not isinstance(path, str):
        return ""
    p = _decode_status_path(path)
    if not p:
        return ""
    p = posixpath.normpath(p).lstrip("/")
    return p.casefold()


def _is_league_path(path: str) -> bool:
    """True if a path names anything under the league tree, spelling-insensitively.

    Beyond decoding, two more spellings resolve to the same file yet defeat a raw
    prefix test: a leading `./`, and a case variant (`League/`) which is literally the
    same file on a case-insensitive checkout. Casefolding here is deliberately
    conservative — it can only ever make a fail-closed gate reject MORE, never less.
    """
    return _normalize_status_path(path).startswith(_LEAGUE_PREFIX)


def _is_submission_path(path: str) -> bool:
    """True only for a per-entrant submission file league/submissions/<identity>/<file>.

    Requires at least two path segments after the prefix (an identity directory AND a file
    within it). Scaffolding placed directly at the submissions root — most notably
    league/submissions/.gitkeep, committed by the foundational PR to materialize the empty
    tree — has only one trailing segment and is deliberately NOT treated as a submission.

    Classification is done on the NORMALIZED path so that `League/submissions/…` and
    `./league/submissions/…` are recognized as submissions too. Recognizing MORE paths as
    submissions is the fail-closed direction: it can only ever pull a PR INTO confinement.
    """
    if not isinstance(path, str):
        return False
    path = _normalize_status_path(path)
    if not path.startswith(_SUBMISSIONS_PREFIX):
        return False
    remainder = path[len(_SUBMISSIONS_PREFIX):]
    return "/" in remainder.strip("/") and remainder.split("/", 1)[0] != ""


def validate_pr_changes(author: str, name_status_lines: list[str]) -> dict[str, Any]:
    """Confine a community submission PR, from `git diff --name-status` output (santa
    round-7). Stronger than validate_pr_paths (which sees only --name-only path strings):

    - Detects a "submission PR" = one that touches league/submissions/** at all. Only such
      PRs are confined here; a pure maintainer/plumbing PR (no submissions/**) is passed
      through (is_submission_pr=False) for normal review.
    - Rejects RENAMES, COPIES and DELETES (R*/C*/D status) against the LEAGUE tree: a
      rename or copy can drag another entrant's bot into your directory, and a delete can
      remove history/other rows. A submission PR may only ADD or MODIFY its own two files.
      Scoped to league/** on purpose — a maintainer PR deleting a stale doc or renaming a
      src/ module is ordinary plumbing and goes through normal review instead. That scope
      test is applied to a NORMALIZED path (see _normalize_status_path), never to git's
      raw output, and the same normalization decides submission-PR classification.
    - Fails CLOSED on any status it does not positively recognize. Only A (add), M
      (modify) and T (typechange) pass; a submission PR's own files are then confined by
      validate_pr_paths to {main.py, submission.json}, so T cannot widen what it may touch.
    - Rejects a submission PR that ALSO edits anything outside its own submission files —
      crucially .github/workflows/** (the pwn-request vector where a PR rewrites the very
      workflow that scores it) or league/matches.jsonl.

    Each line is a tab-separated `git diff --name-status` record: `<STATUS>\t<path>` for
    add/modify/delete, or `<STATUS>\t<old>\t<new>` for rename/copy. Paths may arrive
    C-quoted (git's default for non-ASCII); they are decoded before any check. CI feeds
    the `-z` form, which is not quoted at all.
    """
    errors: list[str] = []
    if not isinstance(author, str) or not _AUTHOR_RE.fullmatch(author):
        return {"ok": False, "errors": [f"invalid PR author login: {author!r}"],
                "is_submission_pr": False}
    changed_paths: list[str] = []
    is_submission_pr = False
    for raw in name_status_lines:
        # A record arrives either as a raw tab-delimited LINE (legacy/text input) or, from
        # the -z path, as an already-split (status, *paths) SEQUENCE. Accepting the split
        # form keeps -z end-to-end structured: a pathname may legally contain a tab, and
        # git's -z output exists precisely to carry it, so re-joining with tabs would
        # either corrupt that path or force rejecting a legitimate maintainer PR.
        if isinstance(raw, (list, tuple)):
            if not raw or not all(isinstance(f, str) for f in raw):
                # Reject rather than filter: dropping a non-string field would silently
                # turn a malformed record into a well-formed-looking one.
                errors.append(f"malformed change record: {raw!r}")
                continue
            status, paths = raw[0], list(raw[1:])
        elif isinstance(raw, str):
            if not raw.strip():
                continue
            parts = raw.rstrip("\r\n").split("\t")
            # Status is NOT stripped: git emits no padding, so `A ` is not a
            # token git wrote and must not be normalized into one.
            status = parts[0]
            # Paths are NOT stripped: a leading/trailing space is a legal filename byte
            # git emits verbatim, and trimming it would let `main.py ` — a different
            # file — satisfy the {main.py, submission.json} allowlist.
            paths = parts[1:]
        else:
            errors.append(f"malformed change record: {raw!r}")
            continue
        # Empty fields are NOT silently dropped. Filtering them first would defeat the
        # arity check below: `D\tdocs/stale.md\t` and ('D','docs/stale.md','') would each
        # collapse to a well-formed one-path record instead of being rejected as
        # malformed. An empty path field is never something a gate should interpret.
        if any(p == "" for p in paths):
            errors.append(f"malformed record for status {status!r}: empty path field")
            continue
        # A path is a *submission* only if it lives in a per-entrant subdirectory:
        # league/submissions/<identity>/<file> (>=2 segments after the prefix). Directory
        # scaffolding at the submissions ROOT itself (e.g. league/submissions/.gitkeep)
        # is NOT a submission — otherwise the foundational maintainer PR that creates the
        # tree would be misclassified and confined to submission-only paths, rejecting its
        # own .github/** and src/** files.
        code = status[:1]
        # Fail CLOSED on anything we do not positively recognize, BEFORE any other test.
        # Scoping the gate to R/C/D had left every other code (U unmerged, X "bug in
        # git", B broken pairing) falling through to ALLOW, where main rejected them.
        # The token must match EXACTLY: a first-character test read `MALFORMED` as a
        # modify and let it through.
        if not _STATUS_RE.match(status) or (
                code not in _ALLOWED_STATUS_CODES and code not in _BLOCKED_STATUS_CODES):
            errors.append(f"unrecognized change status {status!r} for paths {paths}")
            continue
        # Enforce exact ARITY for the status. R/C carry two paths (old, new); every other
        # status carries exactly one. A record with the wrong count is malformed and must
        # fail closed here rather than only in the -z framing — this function is public
        # and also consumes raw `--name-status` text, where `R100\tdocs/old.md` (a rename
        # missing its destination) was previously accepted.
        want = 2 if code in ("R", "C") else 1
        if len(paths) != want:
            errors.append(
                f"malformed record for status {status!r}: expected {want} path "
                f"field(s), got {len(paths)}"
            )
            continue
        # A path is a *submission* only if it lives in a per-entrant subdirectory.
        if any(_is_submission_path(p) for p in paths):
            is_submission_pr = True
        # Rename/copy (R*/C*) and delete (D) are never allowed against the LEAGUE tree: a
        # rename can pull another entrant's bytes into your dir; a delete can drop
        # history/rows. Scoped to league/** on purpose — a maintainer PR that deletes a
        # stale doc or renames a src/ module is ordinary plumbing and is not policed here
        # (it goes through normal review). Before this scoping, any PR deleting any file
        # was rejected even when is_submission_pr was False.
        if code in _BLOCKED_STATUS_CODES and any(_is_league_path(p) for p in paths):
            errors.append(f"disallowed change status {status!r} for paths {paths}")
            continue
        changed_paths.extend(_decode_status_path(p) for p in paths)
    # Only confine a PR that actually touches the submissions tree; plumbing PRs pass.
    if is_submission_pr:
        inner = validate_pr_paths(author, changed_paths)
        errors.extend(inner["errors"])
    return {"ok": not errors, "errors": errors, "is_submission_pr": is_submission_pr}


def validate_harness_fingerprint(manifest: dict[str, Any]) -> dict[str, Any]:
    """Check a harness reader's output is schema-complete and leak-safe.

    A new harness adapter (copilot, codex, …) implements a reader that returns this
    manifest shape; this validates it before it can enter the league.
    """
    errors: list[str] = []
    # 1. schema completeness (allowlist keys exactly)
    missing = set(FINGERPRINT_SCHEMA_KEYS) - set(manifest)
    for k in sorted(missing):
        errors.append(f"missing required schema key: {k}")
    extra = set(manifest) - set(FINGERPRINT_SCHEMA_KEYS)
    for k in sorted(extra):
        errors.append(f"unexpected key not in fixed schema: {k}")
    # 2. leak-safety: every emitted name must pass the scanner
    for field in ("skills", "nested_skills", "mcps", "plugins"):
        for name in manifest.get(field, []) or []:
            if not is_safe_name(name):
                errors.append(f"leak risk: {field} entry failed safety scan")
    # tools is a list of {name, source, enabled}; scan the emitted names.
    for tool in manifest.get("tools", []) or []:
        if not isinstance(tool, dict) or "name" not in tool:
            errors.append("tools entry missing name")
        elif not is_safe_name(tool["name"]):
            errors.append("leak risk: tools entry failed safety scan")
    # cli_version carries version/path strings — must not be secret-shaped.
    # Use pattern-only check (no entropy gate) for version banners like "2.1.195 (Claude Code)"
    # which have high entropy but are not secrets.
    cli = manifest.get("cli_version")
    if isinstance(cli, dict):
        for key in ("version", "path"):
            val = cli.get(key)
            if isinstance(val, str) and val not in ("unknown", "redacted") and _has_secret_pattern(val):
                errors.append(f"leak risk: cli_version.{key} looks secret-like")
    model = manifest.get("model")
    if isinstance(model, str) and model != "unknown" and is_secret(model):
        errors.append("leak risk: model value looks secret-like")
    # 3. unknown[] entries carry a field + a reason from the locked schema enum
    for u in manifest.get("unknown", []) or []:
        if not isinstance(u, dict) or "field" not in u or "reason" not in u:
            errors.append("unknown[] entry missing field/reason")
        elif u["reason"] not in reader.VALID_REASONS:
            errors.append(
                f"unknown[] reason {u['reason']!r} not in schema enum "
                f"{sorted(reader.VALID_REASONS)}"
            )
    return {"ok": not errors, "errors": errors}


def validate_game_bot(bot_path: str) -> dict[str, Any]:
    """Check a submitted bot's shape/size before it is ever executed."""
    errors: list[str] = []
    try:
        validate_bot_shape(bot_path)
    except AtvError as e:
        errors.append(f"{e.problem} ({e.cause})")
    return {"ok": not errors, "errors": errors}
