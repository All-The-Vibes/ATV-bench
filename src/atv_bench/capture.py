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

from atv_bench.fingerprint.scan import _has_secret_pattern, is_secret

# Bounds — a game bot is a handful of small files.
MAX_FILES = 64
MAX_TOTAL_BYTES = 1024 * 1024  # 1 MiB
MAX_FILE_BYTES = 512 * 1024
# Files we scan for secret CONTENT (text). Binary blobs are rejected outright.
_TEXT_SUFFIXES = {".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".md", ".cfg", ".ini", ""}


class CaptureRejected(Exception):
    """The captured bot tree failed the allowlist and must not be used."""


@dataclasses.dataclass(frozen=True)
class CapturedFile:
    relpath: str
    size: int


def _is_secret_content(text: str) -> bool:
    """True if any line of `text` carries a secret shape / high-entropy token."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if _has_secret_pattern(line):
            return True
        # token-per-word entropy check (reuses the fingerprint scanner)
        for tok in line.replace("=", " ").replace(":", " ").split():
            if len(tok) >= 16 and is_secret(tok):
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
        # The git metadata dir is not part of the bot tree — skip it entirely.
        if ".git" in path.relative_to(root).parts:
            continue
        # Reject ANY symlink (dir or file) — escape + leak surface.
        if path.is_symlink():
            raise CaptureRejected(f"symlink not allowed in captured tree: {path.name}")
        if path.is_dir():
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
