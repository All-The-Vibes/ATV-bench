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
from atv_bench.fingerprint.scan import is_safe_name, is_secret
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


def _is_submission_path(path: str) -> bool:
    """True only for a per-entrant submission file league/submissions/<identity>/<file>.

    Requires at least two path segments after the prefix (an identity directory AND a file
    within it). Scaffolding placed directly at the submissions root — most notably
    league/submissions/.gitkeep, committed by the foundational PR to materialize the empty
    tree — has only one trailing segment and is deliberately NOT treated as a submission.
    """
    if not isinstance(path, str) or not path.startswith(_SUBMISSIONS_PREFIX):
        return False
    remainder = path[len(_SUBMISSIONS_PREFIX):]
    return "/" in remainder.strip("/") and remainder.split("/", 1)[0] != ""


def validate_pr_changes(author: str, name_status_lines: list[str]) -> dict[str, Any]:
    """Confine a community submission PR, from `git diff --name-status` output (santa
    round-7). Stronger than validate_pr_paths (which sees only --name-only path strings):

    - Detects a "submission PR" = one that touches league/submissions/** at all. Only such
      PRs are confined here; a pure maintainer/plumbing PR (no submissions/**) is passed
      through (is_submission_pr=False) for normal review.
    - Rejects RENAMES and DELETES (R*/C*/D status) outright: a rename can drag another
      entrant's bot into your directory, and a delete can remove history/other rows. A
      submission PR may only ADD or MODIFY its own two files.
    - Rejects a submission PR that ALSO edits anything outside its own submission files —
      crucially .github/workflows/** (the pwn-request vector where a PR rewrites the very
      workflow that scores it) or league/matches.jsonl.

    Each line is a tab-separated `git diff --name-status` record: `<STATUS>\t<path>` for
    add/modify/delete, or `<STATUS>\t<old>\t<new>` for rename/copy.
    """
    errors: list[str] = []
    if not isinstance(author, str) or not _AUTHOR_RE.fullmatch(author):
        return {"ok": False, "errors": [f"invalid PR author login: {author!r}"],
                "is_submission_pr": False}
    changed_paths: list[str] = []
    is_submission_pr = False
    for raw in name_status_lines:
        if not isinstance(raw, str) or not raw.strip():
            continue
        parts = raw.split("\t")
        status = parts[0].strip()
        paths = [p.strip() for p in parts[1:] if p.strip()]
        # A path is a *submission* only if it lives in a per-entrant subdirectory:
        # league/submissions/<identity>/<file> (>=2 segments after the prefix). Directory
        # scaffolding at the submissions ROOT itself (e.g. league/submissions/.gitkeep)
        # is NOT a submission — otherwise the foundational maintainer PR that creates the
        # tree would be misclassified and confined to submission-only paths, rejecting its
        # own .github/** and src/** files.
        if any(_is_submission_path(p) for p in paths):
            is_submission_pr = True
        # Rename/copy (R*/C*) and delete (D) are never allowed on a submission PR: a rename
        # can pull another entrant's bytes into your dir; a delete can drop history/rows.
        code = status[:1]
        if code in ("R", "C", "D"):
            errors.append(f"disallowed change status {status!r} for paths {paths}")
            continue
        changed_paths.extend(paths)
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
    for field in ("skills", "mcps", "plugins"):
        for name in manifest.get(field, []) or []:
            if not is_safe_name(name):
                errors.append(f"leak risk: {field} entry failed safety scan")
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
