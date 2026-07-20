"""Safe capture of untrusted harness output trees.

Harnesses can create more than ordinary files: links, Windows junctions, device
nodes, sockets, FIFOs, hardlinks, and files that change between validation and
read.  Capture is therefore fail-closed and descriptor-oriented.  A rejected
tree must never be copied into an arena or published as benchmark evidence.
"""
from __future__ import annotations

import dataclasses
import os
import re
import stat
from collections.abc import Collection
from pathlib import Path, PurePosixPath

from atv_bench.fingerprint.scan import _has_secret_pattern

# Bounds -- a game bot is a handful of small files.
MAX_FILES = 64
MAX_TOTAL_BYTES = 1024 * 1024  # 1 MiB
MAX_FILE_BYTES = 512 * 1024
MAX_ENTRIES = 512
MAX_DIRECTORIES = 256
MAX_DEPTH = 32
MAX_PATH_BYTES = 4_096

# Files we scan for secret CONTENT (text). Binary blobs are rejected outright.
_TEXT_SUFFIXES = {
    ".py", ".txt", ".json", ".yaml", ".yml", ".toml", ".md", ".cfg", ".ini", ""
}

# Transient build/cache artifacts a bot run can drop. They are not authored
# output, but the path object itself must still be a real directory/file before
# it is ignored.
_IGNORED_DIR_PARTS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules", ".venv", "venv", ".tox",
}
_IGNORED_SUFFIXES = {".pyc", ".pyo", ".so", ".o", ".class"}

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class CaptureRejected(Exception):
    """The captured tree failed validation and must not be used."""


@dataclasses.dataclass(frozen=True)
class CapturedFile:
    relpath: str
    size: int


def _is_reparse_point(st: os.stat_result) -> bool:
    """Return whether *st* describes a Windows reparse point.

    ``Path.is_symlink`` does not cover every junction/mount-point shape.  The
    file-attribute check is harmless on POSIX because ``st_file_attributes`` is
    absent there.
    """

    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _validate_root(root: Path) -> Path:
    root = Path(os.path.abspath(os.fspath(root)))
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current = current / part
        try:
            ancestor_st = os.lstat(current)
        except OSError as exc:
            raise CaptureRejected(f"capture root is unreadable: {exc}") from exc
        if stat.S_ISLNK(ancestor_st.st_mode) or _is_reparse_point(ancestor_st):
            raise CaptureRejected("capture root traverses a symlink or junction")
    try:
        st = os.lstat(root)
    except OSError as exc:
        raise CaptureRejected(f"capture root is unreadable: {exc}") from exc
    if stat.S_ISLNK(st.st_mode) or _is_reparse_point(st):
        raise CaptureRejected("capture root may not be a symlink or junction")
    if not stat.S_ISDIR(st.st_mode):
        raise CaptureRejected("capture root is not a directory")
    return root


def _normalise_relative_path(relpath: str | os.PathLike[str]) -> tuple[str, ...]:
    raw = os.fspath(relpath)
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    if "\x00" in raw:
        raise CaptureRejected("captured path contains NUL")
    portable = raw.replace("\\", "/")
    if (
        portable.startswith("/")
        or portable.startswith("//")
        or _WINDOWS_DRIVE.match(portable)
    ):
        raise CaptureRejected(f"absolute captured path is not allowed: {raw!r}")
    parts = PurePosixPath(portable).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise CaptureRejected(f"unsafe captured path: {raw!r}")
    if any(any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in part) for part in parts):
        raise CaptureRejected(f"captured path contains control characters: {raw!r}")
    return tuple(parts)


def _confined_candidate(root: Path, parts: tuple[str, ...]) -> Path:
    candidate = root.joinpath(*parts)
    root_key = os.path.normcase(os.path.abspath(os.fspath(root)))
    candidate_key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
    try:
        common = os.path.commonpath([root_key, candidate_key])
    except ValueError as exc:
        raise CaptureRejected(f"captured path escapes root: {'/'.join(parts)}") from exc
    if common != root_key:
        raise CaptureRejected(f"captured path escapes root: {'/'.join(parts)}")
    return candidate


def _reject_unsafe_stat(st: os.stat_result, relpath: str, *, directory_ok: bool) -> None:
    mode = st.st_mode
    if stat.S_ISLNK(mode) or _is_reparse_point(st):
        raise CaptureRejected(f"symlink/junction not allowed in captured tree: {relpath}")
    if stat.S_ISDIR(mode):
        if directory_ok:
            return
        raise CaptureRejected(f"captured path is a directory, not a file: {relpath}")
    if not stat.S_ISREG(mode):
        raise CaptureRejected(f"special file not allowed in captured tree: {relpath}")
    # A hardlink can expose an inode outside the declared tree.  There is no
    # portable way to prove every linked name is within the capture root, so
    # untrusted multi-link files are rejected.
    if getattr(st, "st_nlink", 1) > 1:
        raise CaptureRejected(f"hardlink not allowed in captured tree: {relpath}")


def read_confined_regular_file(
    root: Path,
    relpath: str | os.PathLike[str],
    *,
    max_bytes: int = MAX_FILE_BYTES,
) -> bytes:
    """Read one regular file without following links or leaving ``root``.

    The path is validated both before and after opening.  POSIX uses
    ``O_NOFOLLOW`` when available; all platforms compare the opened descriptor
    to a fresh ``lstat`` to detect replacement races.
    """

    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    root = _validate_root(root)
    parts = _normalise_relative_path(relpath)
    candidate = _confined_candidate(root, parts)
    rel = "/".join(parts)

    # Every ancestor must be a real directory rather than a link/junction.
    current = root
    for part in parts[:-1]:
        current = current / part
        try:
            ancestor_st = os.lstat(current)
        except OSError as exc:
            raise CaptureRejected(f"captured path is unreadable: {rel}") from exc
        _reject_unsafe_stat(ancestor_st, rel, directory_ok=True)
        if not stat.S_ISDIR(ancestor_st.st_mode):
            raise CaptureRejected(f"captured path ancestor is not a directory: {rel}")

    try:
        before = os.lstat(candidate)
    except OSError as exc:
        raise CaptureRejected(f"captured file is unreadable: {rel}") from exc
    _reject_unsafe_stat(before, rel, directory_ok=False)
    if before.st_size > max_bytes:
        raise CaptureRejected(f"file too large: {rel} ({before.st_size} bytes)")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(candidate, flags)
    except OSError as exc:
        raise CaptureRejected(f"captured file could not be opened safely: {rel}") from exc

    try:
        opened = os.fstat(fd)
        _reject_unsafe_stat(opened, rel, directory_ok=False)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise CaptureRejected(f"captured file changed while opening: {rel}")

        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise CaptureRejected(f"file too large: {rel} (> {max_bytes} bytes)")

        try:
            after = os.lstat(candidate)
        except OSError as exc:
            raise CaptureRejected(f"captured file changed while reading: {rel}") from exc
        _reject_unsafe_stat(after, rel, directory_ok=False)
        if (
            (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino)
            or after.st_size != len(data)
        ):
            raise CaptureRejected(f"captured file changed while reading: {rel}")
        return data
    finally:
        os.close(fd)


def _is_secret_content(text: str) -> bool:
    """True if any line carries a hard secret pattern."""

    for line in text.splitlines():
        line = line.strip()
        if line and _has_secret_pattern(line):
            return True
    return False


def scan_captured_tree(
    root: Path,
    *,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_entries: int = MAX_ENTRIES,
    max_directories: int = MAX_DIRECTORIES,
    max_depth: int = MAX_DEPTH,
    max_path_bytes: int = MAX_PATH_BYTES,
    allowed_text_suffixes: Collection[str] | None = _TEXT_SUFFIXES,
) -> list[CapturedFile]:
    """Validate and enumerate the captured regular text files below ``root``."""

    if min(
        max_files,
        max_total_bytes,
        max_file_bytes,
        max_entries,
        max_directories,
        max_depth,
        max_path_bytes,
    ) < 0:
        raise ValueError("capture limits must be non-negative")
    suffix_allowlist = (
        None
        if allowed_text_suffixes is None
        else {str(value).lower() for value in allowed_text_suffixes}
    )
    root = _validate_root(root)
    accepted: list[CapturedFile] = []
    total = 0
    entries_seen = 0
    directories_seen = 0

    def visit(directory: Path, rel_parts: tuple[str, ...]) -> None:
        nonlocal total, entries_seen, directories_seen
        if len(rel_parts) > max_depth:
            raise CaptureRejected(f"captured tree exceeds depth limit ({max_depth})")
        directories_seen += 1
        if directories_seen > max_directories:
            raise CaptureRejected(
                f"captured tree has too many directories (> {max_directories})"
            )
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            rel = "/".join(rel_parts) or "."
            raise CaptureRejected(f"captured directory is unreadable: {rel}") from exc

        for entry in entries:
            entries_seen += 1
            if entries_seen > max_entries:
                raise CaptureRejected(
                    f"captured tree has too many entries (> {max_entries})"
                )
            parts = rel_parts + (entry.name,)
            rel = "/".join(parts)
            if len(rel.encode("utf-8", errors="surrogatepass")) > max_path_bytes:
                raise CaptureRejected(
                    f"captured path exceeds {max_path_bytes} UTF-8 bytes"
                )
            try:
                entry_st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise CaptureRejected(f"captured path is unreadable: {rel}") from exc

            # Reject link-like objects before applying ignore rules.  An ignored
            # directory must not become a hiding place for a junction escape.
            if entry.is_symlink() or _is_reparse_point(entry_st):
                raise CaptureRejected(
                    f"symlink/junction not allowed in captured tree: {rel}"
                )

            if stat.S_ISDIR(entry_st.st_mode):
                if entry.name in _IGNORED_DIR_PARTS:
                    continue
                visit(Path(entry.path), parts)
                continue
            if not stat.S_ISREG(entry_st.st_mode):
                raise CaptureRejected(f"special file not allowed in captured tree: {rel}")
            if getattr(entry_st, "st_nlink", 1) > 1:
                raise CaptureRejected(f"hardlink not allowed in captured tree: {rel}")

            suffix = Path(entry.name).suffix.lower()
            if suffix in _IGNORED_SUFFIXES:
                continue
            if len(accepted) >= max_files:
                raise CaptureRejected(f"too many files in captured tree (> {max_files})")
            if entry_st.st_size > max_file_bytes:
                raise CaptureRejected(f"file too large: {rel} ({entry_st.st_size} bytes)")

            data = read_confined_regular_file(root, rel, max_bytes=max_file_bytes)
            total += len(data)
            if total > max_total_bytes:
                raise CaptureRejected(
                    f"captured tree total size too large (> {max_total_bytes})"
                )
            if suffix_allowlist is not None and suffix not in suffix_allowlist:
                raise CaptureRejected(f"disallowed file type in captured tree: {rel}")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CaptureRejected(f"binary/unreadable file not allowed: {rel}") from exc
            if "\x00" in text:
                raise CaptureRejected(f"binary/unreadable file not allowed: {rel}")
            if _is_secret_content(text):
                raise CaptureRejected(f"secret-shaped content in captured file: {rel}")
            accepted.append(CapturedFile(relpath=rel, size=len(data)))

    visit(root, ())
    return accepted
