"""Focused filesystem-confinement tests for captured harness artifacts."""
from __future__ import annotations

import os
import subprocess

import pytest

from atv_bench.capture import (
    CaptureRejected,
    read_confined_regular_file,
    scan_captured_tree,
)


def test_confined_reader_rejects_absolute_and_parent_paths(tmp_path):
    (tmp_path / "ok.py").write_text("VALUE = 1\n", encoding="utf-8")
    with pytest.raises(CaptureRejected, match="absolute|unsafe"):
        read_confined_regular_file(tmp_path, str((tmp_path / "ok.py").resolve()))
    with pytest.raises(CaptureRejected, match="unsafe|escapes"):
        read_confined_regular_file(tmp_path, "../ok.py")


def test_confined_reader_enforces_byte_limit(tmp_path):
    (tmp_path / "large.py").write_text("x" * 100, encoding="utf-8")
    with pytest.raises(CaptureRejected, match="large"):
        read_confined_regular_file(tmp_path, "large.py", max_bytes=16)


def test_control_characters_in_artifact_paths_are_rejected(tmp_path):
    strange = tmp_path / "line\nbreak.py"
    try:
        strange.write_text("VALUE = 1\n", encoding="utf-8")
    except OSError as exc:
        pytest.skip(f"control-character filenames unavailable on this worker: {exc}")
    with pytest.raises(CaptureRejected, match="control"):
        scan_captured_tree(tmp_path)


def test_hardlink_is_rejected(tmp_path):
    outside = tmp_path.parent / "hardlink-source.py"
    outside.write_text("VALUE = 1\n", encoding="utf-8")
    try:
        os.link(outside, tmp_path / "linked.py")
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on this worker: {exc}")
    with pytest.raises(CaptureRejected, match="hardlink"):
        scan_captured_tree(tmp_path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO fixture")
def test_fifo_is_rejected_as_special_file(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(CaptureRejected, match="special"):
        scan_captured_tree(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fixture")
def test_windows_junction_is_rejected(tmp_path):
    outside = tmp_path.parent / "junction-target"
    outside.mkdir(exist_ok=True)
    (outside / "secret.py").write_text("VALUE = 1\n", encoding="utf-8")
    junction = tmp_path / "junction"
    proc = subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"junction creation unavailable: {proc.stderr or proc.stdout}")
    with pytest.raises(CaptureRejected, match="junction|symlink"):
        scan_captured_tree(tmp_path)


def test_capture_root_itself_may_not_be_a_link(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this worker: {exc}")
    with pytest.raises(CaptureRejected, match="root|junction|symlink"):
        scan_captured_tree(linked)


def test_empty_directory_flood_and_depth_are_bounded(tmp_path):
    flood = tmp_path / "flood"
    flood.mkdir()
    for index in range(12):
        (flood / f"d{index}").mkdir()
    with pytest.raises(CaptureRejected, match="entries|directories"):
        scan_captured_tree(
            flood,
            max_entries=10,
            max_directories=10,
        )

    deep = tmp_path / "deep"
    deep.mkdir()
    current = deep
    for index in range(6):
        current = current / f"d{index}"
        current.mkdir()
    with pytest.raises(CaptureRejected, match="depth"):
        scan_captured_tree(deep, max_depth=3)


def test_explicit_source_tree_profile_allows_arbitrary_utf8_suffix(tmp_path):
    source = tmp_path / "main.go"
    source.write_bytes(b"package main\n")

    with pytest.raises(CaptureRejected, match="disallowed file type"):
        scan_captured_tree(tmp_path)

    captured = scan_captured_tree(
        tmp_path,
        allowed_text_suffixes=None,
    )
    assert [(item.relpath, item.size) for item in captured] == [
        ("main.go", len(b"package main\n"))
    ]
