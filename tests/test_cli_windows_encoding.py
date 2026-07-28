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
    # Scope the lossy-char check to the MARK COLUMN, not the whole stream: prose or a
    # runner-specific path could legitimately contain a literal "?", which would make a
    # whole-output assertion flaky for reasons unrelated to encoding.
    mark_column = [ln.strip().split(" ", 1)[0] for ln in out.splitlines() if ln.startswith("  ")]
    assert "?" not in mark_column, f"lossy replacement char used as a status mark:\n{out}"
    assert any(m in ("[OK]", "[X]", "[-]") for m in mark_column), (
        f"no ASCII fallback marks found:\n{out}"
    )


def test_utf8_marks_are_not_downgraded() -> None:
    """A UTF-8 console must keep the real glyphs, not the ASCII fallback."""
    env = _child_env(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "doctor"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=120,
    )
    assert "[OK]" not in proc.stdout, f"ASCII fallback used on a UTF-8 console:\n{proc.stdout}"


# --- The actual reported invocation, not just `--help` -----------------------------
# santa-loop round 1: adversarial review showed GLYPH_COMMANDS only covered
# `submit --help`, which prints argparse help and NEVER enters the preflight loop —
# the exact code path that crashed. Mutation testing confirmed the suite was near
# vacuous: deleting the whole hardening layer still left 12/13 tests passing.

def test_reported_submit_invocation_survives_cp1252(tmp_path) -> None:
    """`atv-bench submit ./main.py --game battlesnake` — verbatim the command the user
    reported — must run its preflight loop on a cp1252 console without crashing, and
    must print legible ASCII marks rather than lossy `?`."""
    bot = tmp_path / "main.py"
    bot.write_text("def move(state):\n    return 'up'\n", encoding="utf-8")
    env = _child_env(
        PYTHONIOENCODING="cp1252:strict", PYTHONUTF8="0", PYTHONLEGACYWINDOWSSTDIO="1",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "submit", str(bot),
         "--game", "battlesnake", "--harness", "claude-code"],
        capture_output=True, text=True, encoding="cp1252", errors="replace",
        env=env, cwd=str(tmp_path), timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert "UnicodeEncodeError" not in out, f"reported command still crashes:\n{out}"
    # It must actually REACH preflight — otherwise the assertion above is vacuous.
    preflight = [ln for ln in out.splitlines() if "gh_installed" in ln]
    assert preflight, f"never reached the preflight loop (the crash site):\n{out}"
    assert any(m in preflight[0] for m in ("[OK]", "[X]", "[-]")), (
        f"preflight line has no legible ASCII mark: {preflight[0]!r}"
    )
    assert "?" not in out, f"lossy replacement char in reported command output:\n{out}"


def test_decorative_glyph_commands_stay_legible() -> None:
    """Commands carrying non-status glyphs (▶ ★ ⚔ ↳) must degrade to ASCII too.

    `errors="replace"` alone renders `▶ atv-bench run --demo` as `? atv-bench run --demo`,
    which reads like a broken program. Layer 2's rationale applies beyond ✓/✗.
    """
    proc = _run(["run", "--demo"])
    out = proc.stdout
    assert out.strip(), f"no output from run --demo: {proc.stderr}"
    assert "?" not in out, f"lossy replacement char leaked:\n{out}"
