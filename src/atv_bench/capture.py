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

# Bounds — a multi-file arena bot (a compiled-from-source engine like chess/Kojiro or a
# robocode robot dir) is larger than a single main.py, but still bounded. These caps stop
# a DoS blob, not legitimate source trees.
MAX_FILES = 400
MAX_TOTAL_BYTES = 8 * 1024 * 1024  # 8 MiB
MAX_FILE_BYTES = 2 * 1024 * 1024   # 2 MiB
# The capture gate is "decodes as UTF-8 text + carries no secret", NOT a narrow extension
# allowlist. CodeClash arenas legitimately ship bot source in many languages (C/C++ `src/`,
# Java `robots/custom/`, Rust/OCaml `submission/`, JS `robot.js`, Redcode `warrior.red`)
# plus text config (`.opt`, `.cfg`, `.toml`). A per-language allowlist fail-closes on all of
# those even though they ARE the authored bot we must capture. So we DENY known-dangerous /
# opaque binary suffixes and accept any remaining file that decodes as UTF-8 and is
# secret-clean. Binary payloads are caught by the UTF-8 decode check regardless of suffix.
_DENIED_SUFFIXES = {
    # compiled / linkable binaries and archives — opaque, can hide payloads
    ".exe", ".dll", ".dylib", ".so", ".o", ".a", ".lib", ".obj", ".class", ".jar",
    ".bin", ".out", ".pyc", ".pyo", ".wasm", ".node",
    # archives / images / media — not bot source, can smuggle bytes
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".pdf", ".mp3", ".mp4", ".woff", ".woff2",
}
# Transient build/cache artifacts a bot run can drop (bytecode caches, venvs, build output).
# These are NOT part of the authored bot — skip them rather than fail the match.
_IGNORED_DIR_PARTS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache",
                      ".ruff_cache", "node_modules", ".venv", "venv", ".tox",
                      "target", "build", "dist", ".gradle"}
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

        # Suffix denylist: reject known-dangerous / opaque binary types up front, by name,
        # before we even read them (defense in depth — the UTF-8 check below is the real
        # binary gate, but a denied suffix is an unambiguous, cheap early reject).
        suffix = path.suffix.lower()
        if suffix in _DENIED_SUFFIXES:
            raise CaptureRejected(f"disallowed file type in captured tree: {rel_str}")
        # Content gate: must decode as UTF-8 text (binary payloads fail here regardless of
        # suffix) and carry no secret shape. Any legitimate multi-language bot SOURCE
        # (.cpp/.java/.js/.rs/.ml/.red/.opt/…) decodes as text and passes.
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            raise CaptureRejected(f"binary/unreadable file not allowed: {rel_str}")
        # A dotfile like .env is a classic secret carrier — scan it hard.
        if _is_secret_content(text):
            raise CaptureRejected(f"secret-shaped content in captured file: {rel_str}")
        accepted.append(CapturedFile(relpath=rel_str, size=size))
    return accepted
