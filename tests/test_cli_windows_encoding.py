"""Windows cp1252 console must not crash the CLI (regression: UnicodeEncodeError).

A Windows console defaults to the legacy cp1252 codepage. The CLI prints ✓/✗ status
glyphs (U+2713/U+2717), which cp1252 cannot encode. With the default strict error
handler, `typer.echo` raises UnicodeEncodeError and the command dies mid-output.

These tests drive the real installed entry point in a subprocess with the console
encoding forced to cp1252/strict, which is exactly what a Windows user hits.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

import atv_bench

# Propagate the running interpreter's import path so the child resolves both atv_bench
# and its third-party deps (typer/click) no matter which interpreter pytest runs under.
_SRC = str(pathlib.Path(atv_bench.__file__).resolve().parent.parent)
_PYTHONPATH = os.pathsep.join([_SRC, *(p for p in sys.path if p)])


def _child_env(**overrides: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = _PYTHONPATH
    env.update(overrides)
    return env

# Commands that print ✓/✗ glyphs on a happy path (no network, no Docker, no repo state).
GLYPH_COMMANDS = [
    ["--help"],
    ["submit", "--help"],
    ["doctor"],
    ["harnesses"],
    ["games"],
]


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    env = _child_env(
        # Force the legacy Windows console codepage with the strict handler Python uses there.
        PYTHONIOENCODING="cp1252:strict",
        PYTHONUTF8="0",  # defeat PEP 540 UTF-8 mode, which would mask the bug
        PYTHONLEGACYWINDOWSSTDIO="1",
    )
    return subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", *args],
        capture_output=True,
        text=True,
        encoding="cp1252",
        errors="replace",
        env=env,
        timeout=120,
    )


@pytest.mark.parametrize("args", GLYPH_COMMANDS, ids=lambda a: " ".join(a))
def test_cp1252_console_does_not_raise_unicode_encode_error(args: list[str]) -> None:
    proc = _run(args)
    combined = proc.stdout + proc.stderr
    assert "UnicodeEncodeError" not in combined, (
        f"cp1252 console crashed on `atv-bench {' '.join(args)}`:\n{combined}"
    )
    assert "charmap" not in combined, f"codec error leaked:\n{combined}"


@pytest.mark.parametrize("args", GLYPH_COMMANDS, ids=lambda a: " ".join(a))
def test_cp1252_console_still_produces_output(args: list[str]) -> None:
    """Degrading glyphs must not degrade into printing nothing."""
    proc = _run(args)
    assert proc.stdout.strip(), f"no stdout on cp1252 for {args}: {proc.stderr}"


def test_utf8_console_keeps_real_glyphs() -> None:
    """The fix must not downgrade capable terminals — UTF-8 still gets ✓/✗."""
    env = _child_env(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "doctor"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    assert "✓" in proc.stdout or "✗" in proc.stdout, proc.stdout


# --- Legibility: degrading must not erase pass/fail information -------------------

def test_cp1252_marks_stay_distinguishable() -> None:
    """`errors="replace"` alone turns both ✓ and ✗ into `?` — pass and fail become
    indistinguishable on exactly the surface (preflight) a Windows user needs to read.
    The CLI must emit ASCII marks instead when the console cannot encode the glyphs."""
    proc = _run(["doctor"])
    out = proc.stdout
    assert "?" not in out, f"lossy replacement char leaked into output:\n{out}"
    assert "[OK]" in out or "[X]" in out, f"no ASCII fallback marks found:\n{out}"


def test_utf8_marks_are_not_downgraded() -> None:
    """A UTF-8 console must keep the real glyphs, not the ASCII fallback."""
    env = _child_env(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "doctor"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    assert "[OK]" not in proc.stdout, f"ASCII fallback used on a UTF-8 console:\n{proc.stdout}"
