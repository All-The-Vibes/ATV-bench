"""Canonical serialization and race-resistant evaluation evidence capture."""
from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

from atv_bench import capture as capture_module


class UnsafePathError(ValueError):
    """A path or tree cannot be read as confined immutable evidence."""


DEFAULT_MAX_FILES = 4_096
DEFAULT_MAX_TOTAL_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_ENTRIES = 8_192
DEFAULT_MAX_DIRECTORIES = 4_096
DEFAULT_MAX_DEPTH = 64
DEFAULT_MAX_PATH_BYTES = 4_096
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass(frozen=True, slots=True)
class TreeLimits:
    max_files: int = DEFAULT_MAX_FILES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_directories: int = DEFAULT_MAX_DIRECTORIES
    max_depth: int = DEFAULT_MAX_DEPTH
    max_path_bytes: int = DEFAULT_MAX_PATH_BYTES

    def __post_init__(self) -> None:
        for field in (
            "max_files",
            "max_total_bytes",
            "max_file_bytes",
            "max_entries",
            "max_directories",
            "max_depth",
            "max_path_bytes",
        ):
            value = getattr(self, field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        if self.max_file_bytes > self.max_total_bytes:
            raise ValueError("max_file_bytes cannot exceed max_total_bytes")


@dataclass(frozen=True, slots=True)
class RegularFileSnapshot:
    path: str
    data: bytes
    size: int
    sha256: str

    def manifest_entry(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


def canonical_json_bytes(value: Any) -> bytes:
    """Return a stable UTF-8 JSON encoding suitable for hashing."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def require_sha256(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
    return value


def safe_relative_path(value: str, *, field: str = "path") -> PurePosixPath:
    """Validate a portable relative path without resolving it on the host."""

    if not isinstance(value, str) or not value:
        raise UnsafePathError(f"{field} must be a non-empty relative path")
    if "\\" in value:
        raise UnsafePathError(f"{field} must use forward slashes")
    path = PurePosixPath(value)
    if path.is_absolute() or value.startswith(("/", "\\")):
        raise UnsafePathError(f"{field} must be relative")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise UnsafePathError(f"{field} contains an unsafe path segment")
    if path.parts and ":" in path.parts[0]:
        raise UnsafePathError(f"{field} must not contain a drive designator")
    if any(
        any(ord(character) < 0x20 or ord(character) == 0x7F for character in part)
        for part in path.parts
    ):
        raise UnsafePathError(f"{field} contains control characters")
    return path


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _absolute(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _identity(st: os.stat_result, *, include_times: bool) -> tuple[int, ...]:
    values = [
        int(st.st_dev),
        int(st.st_ino),
        int(stat.S_IFMT(st.st_mode)),
    ]
    if include_times:
        values.extend(
            [
                int(st.st_size),
                int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))),
                int(getattr(st, "st_ctime_ns", int(st.st_ctime * 1e9))),
                int(getattr(st, "st_nlink", 1)),
            ]
        )
    return tuple(values)


def _reject_link_or_special(
    st: os.stat_result,
    *,
    display: str,
    directory: bool,
) -> None:
    if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
        raise UnsafePathError(f"symlink or junction is forbidden: {display}")
    if directory:
        if not stat.S_ISDIR(st.st_mode):
            raise UnsafePathError(f"path is not a directory: {display}")
        return
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"special file is forbidden: {display}")
    if getattr(st, "st_nlink", 1) > 1:
        raise UnsafePathError(f"hardlink is forbidden: {display}")


def _snapshot_path_chain(root: Path) -> tuple[tuple[Path, tuple[int, ...]], ...]:
    absolute = _absolute(root)
    current = Path(absolute.anchor)
    rows: list[tuple[Path, tuple[int, ...]]] = []
    for part in absolute.parts[1:]:
        current = current / part
        try:
            current_stat = os.lstat(current)
        except OSError as exc:
            raise UnsafePathError(f"path chain is unreadable: {current}") from exc
        _reject_link_or_special(
            current_stat,
            display=str(current),
            directory=True,
        )
        rows.append((current, _identity(current_stat, include_times=False)))
    if not rows:
        raise UnsafePathError("filesystem root cannot be used as an evidence tree")
    return tuple(rows)


def _verify_path_chain(
    snapshot: Sequence[tuple[Path, tuple[int, ...]]],
) -> None:
    for path, expected in snapshot:
        try:
            observed = os.lstat(path)
        except OSError as exc:
            raise UnsafePathError(f"path chain changed during capture: {path}") from exc
        _reject_link_or_special(observed, display=str(path), directory=True)
        if _identity(observed, include_times=False) != expected:
            raise UnsafePathError(f"path chain was replaced during capture: {path}")


def confined_path(
    root: Path,
    relative: str,
    *,
    field: str = "path",
    must_exist: bool = True,
) -> Path:
    """Return a lexical confined path.

    This function does not make later path-based reads safe. Call
    :func:`read_stable_confined_regular_file` for bytes or
    :func:`snapshot_regular_tree` for a complete tree.
    """

    rel = safe_relative_path(relative, field=field)
    absolute_root = _absolute(root)
    chain = _snapshot_path_chain(absolute_root)
    candidate = absolute_root.joinpath(*rel.parts)
    root_key = os.path.normcase(os.path.abspath(os.fspath(absolute_root)))
    candidate_key = os.path.normcase(os.path.abspath(os.fspath(candidate)))
    if os.path.commonpath((root_key, candidate_key)) != root_key:
        raise UnsafePathError(f"{field} escapes its package root")
    if must_exist:
        try:
            os.lstat(candidate)
        except OSError as exc:
            raise UnsafePathError(f"{field} is unreadable") from exc
    _verify_path_chain(chain)
    return candidate


def read_stable_confined_regular_file(
    root: Path,
    relative: str,
    *,
    max_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> bytes:
    """Read twice through the descriptor-safe capture primitive.

    The surrounding path chain, inode, size, link count, and timestamps must be
    stable before, between, and after both descriptor reads. This closes the
    path-based stat/read gaps that are unacceptable for benchmark evidence.
    """

    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    rel = safe_relative_path(relative)
    rel_text = rel.as_posix()
    absolute_root = _absolute(root)
    chain = _snapshot_path_chain(absolute_root)
    candidate = absolute_root.joinpath(*rel.parts)
    try:
        before = os.lstat(candidate)
        _reject_link_or_special(before, display=rel_text, directory=False)
        if before.st_size > max_bytes:
            raise UnsafePathError(
                f"file exceeds limit: {rel_text} ({before.st_size} > {max_bytes})"
            )
        first = capture_module.read_confined_regular_file(
            absolute_root,
            rel_text,
            max_bytes=max_bytes,
        )
        middle = os.lstat(candidate)
        _reject_link_or_special(middle, display=rel_text, directory=False)
        _verify_path_chain(chain)
        if _identity(middle, include_times=True) != _identity(
            before, include_times=True
        ):
            raise UnsafePathError(f"file changed during capture: {rel_text}")
        second = capture_module.read_confined_regular_file(
            absolute_root,
            rel_text,
            max_bytes=max_bytes,
        )
        after = os.lstat(candidate)
        _reject_link_or_special(after, display=rel_text, directory=False)
        _verify_path_chain(chain)
    except capture_module.CaptureRejected as exc:
        raise UnsafePathError(str(exc)) from exc
    except OSError as exc:
        raise UnsafePathError(f"file changed or became unreadable: {rel_text}") from exc
    if _identity(after, include_times=True) != _identity(before, include_times=True):
        raise UnsafePathError(f"file changed during capture: {rel_text}")
    if first != second or len(first) != after.st_size:
        raise UnsafePathError(f"file content changed during capture: {rel_text}")
    return first


def snapshot_regular_tree(
    root: Path,
    *,
    limits: TreeLimits | None = None,
) -> tuple[RegularFileSnapshot, ...]:
    """Capture a bounded regular-file tree once as immutable bytes."""

    active_limits = limits or TreeLimits()
    absolute_root = _absolute(root)
    chain = _snapshot_path_chain(absolute_root)
    total_bytes = 0
    files: list[RegularFileSnapshot] = []
    entries_seen = 0
    directories_seen = 0

    def visit(directory: Path, relative_parts: tuple[str, ...]) -> None:
        nonlocal total_bytes, entries_seen, directories_seen
        if len(relative_parts) > active_limits.max_depth:
            raise UnsafePathError(
                f"tree exceeds depth limit ({active_limits.max_depth})"
            )
        directories_seen += 1
        if directories_seen > active_limits.max_directories:
            raise UnsafePathError(
                f"tree exceeds directory limit ({active_limits.max_directories})"
            )
        try:
            before = os.lstat(directory)
            _reject_link_or_special(
                before,
                display="/".join(relative_parts) or ".",
                directory=True,
            )
            with os.scandir(directory) as scanner:
                entries = sorted(scanner, key=lambda entry: entry.name)
        except OSError as exc:
            raise UnsafePathError(
                f"directory changed or became unreadable: "
                f"{'/'.join(relative_parts) or '.'}"
            ) from exc

        for entry in entries:
            entries_seen += 1
            if entries_seen > active_limits.max_entries:
                raise UnsafePathError(
                    f"tree exceeds entry limit ({active_limits.max_entries})"
                )
            parts = relative_parts + (entry.name,)
            relative = "/".join(parts)
            if (
                len(relative.encode("utf-8", errors="surrogatepass"))
                > active_limits.max_path_bytes
            ):
                raise UnsafePathError(
                    f"path exceeds {active_limits.max_path_bytes} UTF-8 bytes"
                )
            safe_relative_path(relative)
            try:
                entry_stat = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise UnsafePathError(f"path became unreadable: {relative}") from exc
            if stat.S_ISDIR(entry_stat.st_mode):
                _reject_link_or_special(
                    entry_stat,
                    display=relative,
                    directory=True,
                )
                visit(Path(entry.path), parts)
                continue
            _reject_link_or_special(
                entry_stat,
                display=relative,
                directory=False,
            )
            if len(files) >= active_limits.max_files:
                raise UnsafePathError(
                    f"tree exceeds file limit ({active_limits.max_files})"
                )
            if entry_stat.st_size > active_limits.max_file_bytes:
                raise UnsafePathError(
                    f"file exceeds limit: {relative} "
                    f"({entry_stat.st_size} > {active_limits.max_file_bytes})"
                )
            data = read_stable_confined_regular_file(
                absolute_root,
                relative,
                max_bytes=active_limits.max_file_bytes,
            )
            total_bytes += len(data)
            if total_bytes > active_limits.max_total_bytes:
                raise UnsafePathError(
                    f"tree exceeds total byte limit "
                    f"({active_limits.max_total_bytes})"
                )
            files.append(
                RegularFileSnapshot(
                    path=relative,
                    data=data,
                    size=len(data),
                    sha256=sha256_bytes(data),
                )
            )

        try:
            after = os.lstat(directory)
        except OSError as exc:
            raise UnsafePathError(
                f"directory changed during capture: "
                f"{'/'.join(relative_parts) or '.'}"
            ) from exc
        _reject_link_or_special(
            after,
            display="/".join(relative_parts) or ".",
            directory=True,
        )
        if (
            _identity(after, include_times=False)
            != _identity(before, include_times=False)
            or getattr(after, "st_mtime_ns", None)
            != getattr(before, "st_mtime_ns", None)
            or getattr(after, "st_ctime_ns", None)
            != getattr(before, "st_ctime_ns", None)
        ):
            raise UnsafePathError(
                f"directory changed during capture: "
                f"{'/'.join(relative_parts) or '.'}"
            )

    visit(absolute_root, ())
    _verify_path_chain(chain)
    return tuple(sorted(files, key=lambda item: item.path))


def assert_safe_tree(
    root: Path,
    *,
    limits: TreeLimits | None = None,
) -> None:
    snapshot_regular_tree(root, limits=limits)


def iter_regular_files(
    root: Path,
    *,
    limits: TreeLimits | None = None,
) -> Iterator[RegularFileSnapshot]:
    yield from snapshot_regular_tree(root, limits=limits)


def tree_manifest_from_snapshots(
    snapshots: Sequence[RegularFileSnapshot],
) -> tuple[dict[str, Any], ...]:
    return tuple(item.manifest_entry() for item in snapshots)


def tree_manifest(
    root: Path,
    *,
    limits: TreeLimits | None = None,
) -> tuple[dict[str, Any], ...]:
    return tree_manifest_from_snapshots(snapshot_regular_tree(root, limits=limits))


def tree_digest_from_snapshots(
    snapshots: Sequence[RegularFileSnapshot],
) -> str:
    return sha256_json({"files": tree_manifest_from_snapshots(snapshots)})


def tree_digest(
    root: Path,
    *,
    limits: TreeLimits | None = None,
) -> str:
    return tree_digest_from_snapshots(snapshot_regular_tree(root, limits=limits))
