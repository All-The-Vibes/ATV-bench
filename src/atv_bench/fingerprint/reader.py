"""Safe filesystem reads for the fingerprint probe (eng T5).

Every read is confined to the harness config root, never follows a symlink that
escapes that root, and converts every failure into a structured `(value, reason)`
outcome instead of a raise or a silent drop. No bare `except: pass`.
"""
from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Reason enum surfaced into manifest["unknown"][*]["reason"].
REASON_NOT_READABLE = "not_readable"
REASON_ABSENT = "absent"
REASON_MALFORMED = "malformed"
REASON_EMPTY = "empty"
REASON_PERMISSION = "permission_denied"
REASON_SYMLINK_ESCAPE = "symlink_escape"
REASON_NAME_UNSAFE = "name_failed_safety_scan"

VALID_REASONS = frozenset({
    REASON_NOT_READABLE, REASON_ABSENT, REASON_MALFORMED, REASON_EMPTY,
    REASON_PERMISSION, REASON_SYMLINK_ESCAPE, REASON_NAME_UNSAFE,
})


@dataclass(frozen=True)
class ReadOutcome:
    """Either `value` is set (success) or `reason` is set (failure). Never both."""
    value: Any = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.reason is None


def _within_root(path: Path, root: Path) -> bool:
    """True iff the fully-resolved `path` stays inside the resolved `root`.

    Guards against a symlink under the config dir pointing at, e.g., /etc or a
    sibling secrets dir. We resolve both and compare.
    """
    try:
        rp = path.resolve()
        rr = root.resolve()
    except OSError:
        return False
    return rp == rr or rr in rp.parents


def read_json(path: Path, root: Path) -> ReadOutcome:
    """Read + parse a JSON config file, confined to `root`."""
    if not _within_root(path, root):
        return ReadOutcome(reason=REASON_SYMLINK_ESCAPE)
    try:
        if not path.exists():
            return ReadOutcome(reason=REASON_ABSENT)
        raw = path.read_text(encoding="utf-8")
    except PermissionError:
        return ReadOutcome(reason=REASON_PERMISSION)
    except UnicodeDecodeError:
        # non-UTF8 bytes are a malformed config, not a crash (M2 regression).
        return ReadOutcome(reason=REASON_MALFORMED)
    except OSError:
        return ReadOutcome(reason=REASON_NOT_READABLE)
    if not raw.strip():
        return ReadOutcome(reason=REASON_EMPTY)
    try:
        return ReadOutcome(value=json.loads(raw))
    except (json.JSONDecodeError, ValueError):
        return ReadOutcome(reason=REASON_MALFORMED)


def read_toml(path: Path, root: Path) -> ReadOutcome:
    """Read + parse a TOML config file, confined to `root`.

    Mirrors `read_json`: same confinement, same reason enums, same text→parse flow.
    We read text (like read_json) then `tomllib.loads`, NOT `tomllib.load` on a binary
    handle — so a non-UTF8 file becomes REASON_MALFORMED here too, never a crash.
    """
    if not _within_root(path, root):
        return ReadOutcome(reason=REASON_SYMLINK_ESCAPE)
    try:
        if not path.exists():
            return ReadOutcome(reason=REASON_ABSENT)
        raw = path.read_text(encoding="utf-8")
    except PermissionError:
        return ReadOutcome(reason=REASON_PERMISSION)
    except UnicodeDecodeError:
        return ReadOutcome(reason=REASON_MALFORMED)
    except OSError:
        return ReadOutcome(reason=REASON_NOT_READABLE)
    if not raw.strip():
        return ReadOutcome(reason=REASON_EMPTY)
    try:
        return ReadOutcome(value=tomllib.loads(raw))
    except tomllib.TOMLDecodeError:
        return ReadOutcome(reason=REASON_MALFORMED)


def list_child_dir_names(path: Path, root: Path) -> tuple[list[str], list[tuple[str, str]]]:
    """List immediate subdirectory BASENAMES under `path` (never file contents).

    Returns (names, errors) where errors is a list of (field, reason). A child
    whose real path escapes `root` (symlink) is reported as an error, not listed.
    """
    names: list[str] = []
    errors: list[tuple[str, str]] = []
    if not _within_root(path, root):
        return names, [(str(path.name), REASON_SYMLINK_ESCAPE)]
    try:
        if not path.exists():
            return names, errors
        entries = sorted(path.iterdir())
    except PermissionError:
        return names, [(str(path.name), REASON_PERMISSION)]
    except OSError:
        return names, [(str(path.name), REASON_NOT_READABLE)]
    for child in entries:
        try:
            if not child.is_dir():
                continue
        except OSError:
            errors.append((child.name, REASON_NOT_READABLE))
            continue
        # A symlinked child that escapes the root is refused (don't even emit name).
        if child.is_symlink() and not _within_root(child, root):
            errors.append((child.name, REASON_SYMLINK_ESCAPE))
            continue
        names.append(child.name)
    return names, errors


def count_child_files(
    path: Path, root: Path, suffix: str | None = None
) -> tuple[int, list[tuple[str, str]]]:
    """Count immediate files under `path` (basenames/counts only, never contents).

    Returns (count, errors) where errors is a list of (name, reason). A child whose
    real path escapes `root` (symlink) is NOT counted and IS reported; a per-child
    read failure is reported rather than silently skipped.
    """
    errors: list[tuple[str, str]] = []
    if not _within_root(path, root):
        return 0, [(path.name, REASON_SYMLINK_ESCAPE)]
    try:
        if not path.exists():
            return 0, errors
        entries = list(path.iterdir())
    except PermissionError:
        return 0, [(path.name, REASON_PERMISSION)]
    except OSError:
        return 0, [(path.name, REASON_NOT_READABLE)]
    n = 0
    for child in entries:
        # a symlinked child that escapes the root is refused (never counted)
        try:
            is_link = child.is_symlink()
        except OSError:
            errors.append((child.name, REASON_NOT_READABLE))
            continue
        if is_link and not _within_root(child, root):
            errors.append((child.name, REASON_SYMLINK_ESCAPE))
            continue
        try:
            if child.is_file() and (suffix is None or child.name.endswith(suffix)):
                n += 1
        except OSError:
            errors.append((child.name, REASON_NOT_READABLE))
            continue
    return n, errors
