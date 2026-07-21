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


def test_clean_tree_passes(tmp_path):
    _mk(tmp_path, "main.py", "def get_move(o): return 'N'\n")
    _mk(tmp_path, "strategy.py", "WEIGHT = 3\n")
    files = scan_captured_tree(tmp_path)
    assert set(f.relpath for f in files) == {"main.py", "strategy.py"}


def test_symlink_is_rejected(tmp_path):
    _mk(tmp_path, "main.py")
    (tmp_path / "evil").symlink_to("/etc/passwd")
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
    (botdir / "escape").symlink_to(outside)
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


# --- Multi-language source trees (Wave C): the scan must ACCEPT legitimate bot source in
# any language, not just .py. A narrow suffix allowlist fail-closed on real CodeClash
# arenas (corewar's config/88.opt, chess's src/*.cpp, robocode's *.java, robot.js). The
# gate is "decodes as UTF-8 text + secret-clean", with a denylist for opaque binaries. ---

def test_multilang_source_suffixes_are_accepted(tmp_path):
    """Bot source in C++/Java/JS/Rust/OCaml/Redcode + text config must all pass — these
    are the authored bot for compiled/multi-language arenas (chess, robocode, halite,
    corewar, robotrumble). Real matrix run surfaced corewar's `config/88.opt` rejection."""
    _mk(tmp_path, "src/engine.cpp", "int main(){return 0;}\n")
    _mk(tmp_path, "src/engine.hpp", "#pragma once\n")
    _mk(tmp_path, "robots/custom/Bot.java", "class Bot {}\n")
    _mk(tmp_path, "robot.js", "function robot(state, unit) { return {}; }\n")
    _mk(tmp_path, "submission/bot.rs", "fn main() {}\n")
    _mk(tmp_path, "submission/bot.ml", "let () = ()\n")
    _mk(tmp_path, "warrior.red", "MOV 0, 1\n")
    _mk(tmp_path, "config/88.opt", "-r 100\n")  # the exact file that failed the matrix
    _mk(tmp_path, "Makefile", "all:\n\tgcc -o bot bot.c\n")
    files = scan_captured_tree(tmp_path)
    rels = {f.relpath for f in files}
    assert {"src/engine.cpp", "robots/custom/Bot.java", "robot.js",
            "warrior.red", "config/88.opt"} <= rels


def test_binary_payload_still_rejected_by_denylist(tmp_path):
    """A denied binary suffix is rejected even if a caller sneaks it in — defense in depth
    alongside the UTF-8 decode gate."""
    _mk(tmp_path, "main.py", "def get_move(o): return 'N'\n")
    (tmp_path / "payload.exe").write_bytes(b"MZ\x90\x00\x03")
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "disallowed" in str(exc.value).lower()


def test_binary_content_rejected_regardless_of_suffix(tmp_path):
    """A file with an innocuous suffix but binary (non-UTF-8) content is still rejected by
    the content gate — the suffix denylist is not the only guard."""
    _mk(tmp_path, "main.py", "def get_move(o): return 'N'\n")
    (tmp_path / "bot.c").write_bytes(b"\x00\x01\x02\xff\xfe\xbe\xef")  # not valid UTF-8
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "binary" in str(exc.value).lower()


def test_secret_in_source_file_still_rejected(tmp_path):
    """Secret-shaped content is rejected even in a legitimate source suffix."""
    _mk(tmp_path, "bot.cpp", "// key: sk-ant-api03-SECRETSECRETSECRETSECRETSECRET\n")
    with pytest.raises(CaptureRejected) as exc:
        scan_captured_tree(tmp_path)
    assert "secret" in str(exc.value).lower()


def test_engine_replay_artifact_is_skipped_not_rejected(tmp_path):
    """A game engine run in the workdir drops replays/logs (Halite `.hlt` can be multi-MB).
    These are engine output, not authored bot — SKIP them, don't fail the match on the size
    cap. Real matrix run surfaced a 6MB `.hlt` erroring the whole halite match."""
    _mk(tmp_path, "MyBot.py", "def main(): pass\n")
    big_replay = tmp_path / "1784597837-271413169.hlt"
    big_replay.write_text("x" * (3 * 1024 * 1024))  # 3MB replay, over MAX_FILE_BYTES
    (tmp_path / "sim_0.log").write_text("turn log\n")
    files = scan_captured_tree(tmp_path)
    rels = {f.relpath for f in files}
    assert "MyBot.py" in rels
    assert not any(r.endswith(".hlt") for r in rels)
    assert not any(r.endswith(".log") for r in rels)
