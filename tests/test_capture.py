"""TDD for the captured-tree allowlist (Lane B, ENG-7/gap #12).

Before the harness-built bot tree is written into the arena container or reaches any
record/replay/leaderboard, it must pass an allowlist: reject symlinks, reject paths
escaping the bot dir, cap file count + total size, and secret/entropy-scan file
CONTENTS. A planted symlink and a planted .env must be rejected/redacted.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from atv_bench.capture import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    CaptureRejected,
    scan_captured_tree,
)


def _mk(root: Path, rel: str, content: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable on this worker: {exc}")


def test_clean_tree_passes(tmp_path):
    _mk(tmp_path, "main.py", "def get_move(o): return 'N'\n")
    _mk(tmp_path, "strategy.py", "WEIGHT = 3\n")
    files = scan_captured_tree(tmp_path)
    assert set(f.relpath for f in files) == {"main.py", "strategy.py"}


def test_symlink_is_rejected(tmp_path):
    _mk(tmp_path, "main.py")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret")
    _symlink_or_skip(tmp_path / "evil", outside)
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "symlink" in str(exc.value).lower()


def test_dotenv_secret_is_rejected(tmp_path):
    _mk(tmp_path, "main.py")
    _mk(tmp_path, ".env", "GITHUB_TOKEN=ghp_1234567890abcdefghijklmnopqrstuvwxyzAB\n")
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "secret" in str(exc.value).lower() or ".env" in str(exc.value).lower()


def test_file_count_cap(tmp_path):
    for i in range(MAX_FILES + 1):
        _mk(tmp_path, f"f{i}.py")
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "count" in str(exc.value).lower() or "many" in str(exc.value).lower()


def test_total_size_cap(tmp_path):
    _mk(tmp_path, "big.py", "A" * (MAX_TOTAL_BYTES + 1))
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "size" in str(exc.value).lower() or "large" in str(exc.value).lower()


def test_path_escape_via_dotdot_is_rejected(tmp_path):
    # a captured relpath must never escape the bot dir; scan_captured_tree operates on
    # a rooted dir, so we assert the guard rejects a planted absolute/escaping symlink dir
    outside = tmp_path.parent / "outside"
    outside.mkdir(exist_ok=True)
    (outside / "secret.txt").write_text("s")
    botdir = tmp_path / "bot"
    botdir.mkdir()
    _mk(botdir, "main.py")
    _symlink_or_skip(botdir / "escape", outside, directory=True)
    with pytest.raises(CaptureRejected):
        scan_captured_tree(botdir)


def test_high_entropy_blob_is_rejected(tmp_path):
    _mk(tmp_path, "main.py")
    _mk(tmp_path, "data.txt", "sk-ant-api03-SECRETSECRETSECRETSECRETSECRET\n")
    with pytest.raises(CaptureRejected):
        scan_captured_tree(tmp_path)


def test_transient_pycache_is_skipped_not_rejected(tmp_path):
    """A bot run can drop __pycache__/*.pyc — a build artifact, not the authored bot.
    The allowlist must SKIP it, not fail the whole match (real A/B match surfaced this)."""
    _mk(tmp_path, "main.py", "def get_move(o): return 'N'\n")
    pyc = tmp_path / "__pycache__" / "main.cpython-314.pyc"
    pyc.parent.mkdir(parents=True, exist_ok=True)
    pyc.write_bytes(b"\x00\x01\x02\xbe\xef")  # binary bytecode
    files = scan_captured_tree(tmp_path)
    rels = {f.relpath for f in files}
    assert "main.py" in rels
    assert not any("__pycache__" in r for r in rels)
