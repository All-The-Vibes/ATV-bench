"""Captured-tree allowlist (ENG-7 / gap #12).

The harness-built bot tree is untrusted: it can contain symlinks (escape), a planted
`.env`/secret (leak), or an oversized blob (DoS). Before that tree is written into the
arena container OR reaches any match record / replay / leaderboard, it must pass this
allowlist. A rejection is fail-closed: the match errors, it does not silently ship a
redacted-but-partial tree.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

from atv_bench.fingerprint.scan import _has_secret_pattern

# Bounds — a game bot is a handful of small files.
MAX_FILES = 64
MAX_TOTAL_BYTES = 1024 * 1024  # 1 MiB
MAX_FILE_BYTES = 512 * 1024
# Files we scan for secret CONTENT (text). Binary blobs are rejected outright.
_TEXT_SUFFIXES = {".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".md", ".cfg", ".ini", ""}
# Transient build/cache artifacts a bot run can drop (bytecode caches, venvs, coverage).
# These are NOT part of the authored bot — skip them rather than fail the match.
_IGNORED_DIR_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                      ".ruff_cache", "node_modules", ".venv", "venv", ".tox"}
_IGNORED_SUFFIXES = {".pyc", ".pyo", ".so", ".o", ".class"}


class CaptureRejected(Exception):
    """The captured bot tree failed the allowlist and must not be used."""


@dataclasses.dataclass(frozen=True)
class CapturedFile:
    relpath: str
    size: int


def _is_secret_content(text: str) -> bool:
    """True if any line of `text` carries a hard secret PATTERN.

    Content scanning uses PATTERN matching only (token shapes, keys, creds-in-URL, PEM)
    — NOT the name-entropy heuristic. A bot's own source, a seed README, or minified
    code legitimately contains high-entropy tokens (markdown links, hashes, base64) that
    are not secrets; entropy-scanning file BODIES false-positives on all of them. Real
    leaked credentials still match `_has_secret_pattern`.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _has_secret_pattern(line):
            return True
    return False


def scan_captured_tree(root: Path) -> list[CapturedFile]:
    """Validate the captured bot tree under `root`; return the accepted files or raise.

    Fail-closed on: any symlink, any path escaping `root`, > MAX_FILES files,
    > MAX_TOTAL_BYTES total, a single file > MAX_FILE_BYTES, a binary blob, or any
    file whose content carries a secret shape.
    """
    root = Path(root).resolve()
    accepted: list[CapturedFile] = []
    total = 0
    count = 0
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        # Skip transient build/cache dirs (bytecode caches, venvs) — not the authored bot.
        if any(part in _IGNORED_DIR_PARTS for part in rel_parts):
            continue
        # Reject ANY symlink (dir or file) — escape + leak surface.
        if path.is_symlink():
            raise CaptureRejected(f"symlink not allowed in captured tree: {path.name}")
        if path.is_dir():
            continue
        # Skip transient artifact files (compiled bytecode, object files).
        if path.suffix.lower() in _IGNORED_SUFFIXES:
            continue
        # Path-escape guard (defense in depth even though rglob stays under root).
        try:
            rel = path.resolve().relative_to(root)
        except ValueError:
            raise CaptureRejected(f"path escapes bot dir: {path}")
        rel_str = rel.as_posix()

        count += 1
        if count > MAX_FILES:
            raise CaptureRejected(f"too many files in captured tree (> {MAX_FILES})")

        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise CaptureRejected(f"file too large: {rel_str} ({size} bytes)")
        total += size
        if total > MAX_TOTAL_BYTES:
            raise CaptureRejected(f"captured tree total size too large (> {MAX_TOTAL_BYTES})")

        # Content scan (text only; binary rejected).
        suffix = path.suffix.lower()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            raise CaptureRejected(f"binary/unreadable file not allowed: {rel_str}")
        if suffix not in _TEXT_SUFFIXES:
            raise CaptureRejected(f"disallowed file type in captured tree: {rel_str}")
        # A dotfile like .env is a classic secret carrier — scan it hard.
        if _is_secret_content(text):
            raise CaptureRejected(f"secret-shaped content in captured file: {rel_str}")
        accepted.append(CapturedFile(relpath=rel_str, size=size))
    return accepted
