"""Adversarial tests for bounded repository snapshot capture."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import atv_bench.adapters.snapshot as snapshot_module
from atv_bench.adapters.snapshot import (
    DiffLimitExceeded,
    SnapshotRejected,
    capture_diff,
    seed_base,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c", "user.email=a@b.c", "-c", "user.name=atv",
        "commit", "-qm", "seed",
    )
    return repo


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this worker: {exc}")


def test_git_capture_command_has_a_hard_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(
        snapshot_module,
        "_git_command",
        lambda executable, repo, hooks_dir, args: [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        ],
    )
    monkeypatch.setattr(snapshot_module.shutil, "which", lambda name: sys.executable)

    with pytest.raises(SnapshotRejected, match="exceeded"):
        snapshot_module._git(
            tmp_path,
            "status",
            timeout_seconds=0.1,
        )


def _marker_script(path: Path, marker: Path) -> None:
    marker_text = marker.as_posix().replace("'", "'\"'\"'")
    path.write_text(
        "#!/bin/sh\n"
        f"printf ran > '{marker_text}'\n"
        "exit 0\n",
        encoding="utf-8",
    )
    try:
        path.chmod(0o755)
    except OSError:
        pass


def test_untracked_symlink_is_rejected_without_reading_target(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    outside = tmp_path / "outside-secret.py"
    outside.write_text("SECRET = 'do-not-read'\n", encoding="utf-8")
    _symlink_or_skip(repo / "linked.py", outside)
    with pytest.raises(SnapshotRejected, match="symlink|junction|unsafe"):
        capture_diff(repo, base)


def test_tracked_symlink_is_rejected_without_reading_target(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    outside = tmp_path / "outside-tracked-secret.py"
    outside.write_text("SECRET = 'do-not-read'\n", encoding="utf-8")
    _symlink_or_skip(repo / "linked.py", outside)
    _git(repo, "add", "linked.py")
    with pytest.raises(SnapshotRejected, match="symlink|junction|unsafe"):
        capture_diff(repo, base)


def test_untracked_hardlink_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 99\n", encoding="utf-8")
    try:
        os.link(outside, repo / "linked.py")
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable on this worker: {exc}")
    with pytest.raises(SnapshotRejected, match="hardlink|unsafe"):
        capture_diff(repo, base)


def test_untracked_binary_is_rejected(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    (repo / "binary.py").write_bytes(b"\x00\xff\x01")
    with pytest.raises(SnapshotRejected, match="binary"):
        capture_diff(repo, base)


def test_tracked_diff_limit_is_enforced_without_returning_partial_patch(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    (repo / "main.py").write_text("VALUE = '" + ("x" * 5000) + "'\n", encoding="utf-8")
    with pytest.raises(DiffLimitExceeded):
        capture_diff(repo, base, max_bytes=256)


def test_untracked_diff_limit_is_enforced(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    (repo / "large.py").write_text("VALUE = '" + ("x" * 5000) + "'\n", encoding="utf-8")
    with pytest.raises(SnapshotRejected, match="large|exceeded|unsafe"):
        capture_diff(repo, base, max_bytes=256)


def test_repository_external_diff_configuration_is_not_executed(tmp_path):
    repo = _repo(tmp_path)
    base = seed_base(repo)
    marker = tmp_path / "external-diff-ran.txt"
    helper = tmp_path / "external_diff.py"
    helper.write_text(
        "import pathlib,sys\n"
        f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
        "sys.exit(0)\n",
        encoding="utf-8",
    )
    # Git accepts a shell command for diff.external. Capture must disable it.
    _git(repo, "config", "diff.external", f'"{os.fspath(Path(os.sys.executable))}" "{helper}"')
    (repo / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    diff = capture_diff(repo, base)
    assert "VALUE = 2" in diff
    assert not marker.exists()


def test_git_capture_ignores_fsmonitor_hooks_signing_and_ambient_config(
    tmp_path, monkeypatch
):
    repo = _repo(tmp_path)
    ambient_parent = tmp_path / "ambient-case"
    ambient_parent.mkdir()
    ambient_repo = _repo(ambient_parent)

    repo_fsmonitor_marker = tmp_path / "repo-fsmonitor-ran.txt"
    repo_hook_marker = tmp_path / "repo-hook-ran.txt"
    repo_gpg_marker = tmp_path / "repo-gpg-ran.txt"
    repo_fsmonitor = tmp_path / "repo-fsmonitor.sh"
    repo_hooks = tmp_path / "repo-hooks"
    repo_hooks.mkdir()
    repo_reference_hook = repo_hooks / "reference-transaction"
    repo_gpg = tmp_path / "repo-gpg.sh"
    _marker_script(repo_fsmonitor, repo_fsmonitor_marker)
    _marker_script(repo_reference_hook, repo_hook_marker)
    _marker_script(repo_gpg, repo_gpg_marker)
    _git(repo, "config", "core.fsmonitor", repo_fsmonitor.as_posix())
    _git(repo, "config", "core.hooksPath", repo_hooks.as_posix())
    _git(repo, "config", "tag.gpgSign", "true")
    _git(repo, "config", "gpg.program", repo_gpg.as_posix())

    ambient_fsmonitor_marker = tmp_path / "ambient-fsmonitor-ran.txt"
    ambient_hook_marker = tmp_path / "ambient-hook-ran.txt"
    ambient_fsmonitor = tmp_path / "ambient-fsmonitor.sh"
    ambient_hooks = tmp_path / "ambient-hooks"
    ambient_hooks.mkdir()
    ambient_reference_hook = ambient_hooks / "reference-transaction"
    _marker_script(ambient_fsmonitor, ambient_fsmonitor_marker)
    _marker_script(ambient_reference_hook, ambient_hook_marker)
    ambient_config = tmp_path / "ambient.gitconfig"
    ambient_config.write_text(
        "[core]\n"
        f"\tfsmonitor = {ambient_fsmonitor.as_posix()}\n"
        f"\thooksPath = {ambient_hooks.as_posix()}\n"
        "[tag]\n"
        "\tgpgSign = true\n",
        encoding="utf-8",
    )
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    (fake_home / ".gitconfig").write_text(
        ambient_config.read_text(encoding="utf-8"), encoding="utf-8"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(ambient_config))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(ambient_config))
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home))

    base = seed_base(repo)
    (repo / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    diff = capture_diff(repo, base)
    assert "VALUE = 2" in diff

    ambient_base = seed_base(ambient_repo)
    (ambient_repo / "main.py").write_text("VALUE = 3\n", encoding="utf-8")
    ambient_diff = capture_diff(ambient_repo, ambient_base)
    assert "VALUE = 3" in ambient_diff
    assert not repo_fsmonitor_marker.exists()
    assert not repo_hook_marker.exists()
    assert not repo_gpg_marker.exists()
    assert not ambient_fsmonitor_marker.exists()
    assert not ambient_hook_marker.exists()
