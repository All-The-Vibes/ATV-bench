"""Windows cp1252 must not corrupt file writes or library consumers (santa-loop round 1).

Companion to test_cli_windows_encoding.py, which covers the CONSOLE. Two further
failure modes of the same bug class were found by adversarial review:

1. FILE I/O. `Path.write_text()` with no `encoding=` uses the platform's locale
   codepage, which on Windows is cp1252 with a STRICT handler. Hardening sys.stdout
   does nothing for these, so `atv-bench play` still crashed with the identical
   `UnicodeEncodeError` when writing replay HTML containing → ⏸ ▶ 🏆.

2. IMPORT-TIME GLOBAL MUTATION. Reconfiguring sys.stdout at module import leaks into
   any library consumer that imports atv_bench.cli, silently flipping their stdout
   from strict to replace. Two in-package importers already exist.
"""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

# Characters that are NOT encodable in cp1252 and appear in real output/templates.
NON_CP1252 = "→ ⏸ ■ ▲ ▶ ⟲ 🏁 🏆 🤝 ★ ⚔ ↳"


# --- 1. File writes must pin an explicit encoding -----------------------------------

def test_replay_html_is_written_utf8_not_locale_codepage(tmp_path: Path) -> None:
    """`play` writes replay HTML containing arrows/emoji. With no explicit encoding
    Windows uses cp1252 and raises UnicodeEncodeError — the same crash class as the
    console bug, on a path stdout hardening cannot reach."""
    from atv_bench import play

    src = Path(play.__file__).read_text(encoding="utf-8")
    assert "path.write_text(html_text)" not in src, (
        "replay HTML is written without an explicit encoding; on a Windows cp1252 "
        "locale this raises UnicodeEncodeError on the template's → ⏸ ▶ 🏆 glyphs"
    )


def test_no_unencoded_text_writes_of_non_ascii_payloads() -> None:
    """Guard the whole class, not just the one instance that was reported.

    JSON writes are exempt: json.dumps defaults to ensure_ascii=True, so its output is
    pure ASCII and safe under any codepage. Everything else that writes arbitrary text
    (HTML templates, copied bot source) must pin encoding explicitly.
    """
    import atv_bench

    root = Path(atv_bench.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ".write_text(" not in line or "encoding=" in line:
                continue
            if "json.dumps" in line:
                continue  # ensure_ascii=True → ASCII-only payload, safe on any codepage
            if ".read_text()" in line or "html" in line.lower():
                offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "arbitrary-text writes without an explicit encoding (crash on Windows cp1252):\n"
        + "\n".join(offenders)
    )


# --- 2. Importing the CLI must not mutate a library consumer's streams --------------

def test_importing_cli_does_not_mutate_caller_stdout() -> None:
    """Reconfiguring global stdio belongs to the CLI entry point, not to import.

    Run in a subprocess: the mutation is process-global and irreversible, so it cannot
    be observed cleanly in-process once any earlier test has imported the module.
    """
    probe = (
        "import io, sys\n"
        "sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding='cp1252', errors='strict')\n"
        "before = sys.stdout.errors\n"
        "import atv_bench.cli\n"
        "after = sys.stdout.errors\n"
        "sys.stderr.write(f'{before},{after}')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ,
             "PYTHONPATH": str(Path(__import__("atv_bench").__file__).parent.parent)},
    )
    before, _, after = proc.stderr.strip().rpartition(",")
    assert (before, after) == ("strict", "strict"), (
        f"importing atv_bench.cli changed a consumer's stdout errors "
        f"{before!r} -> {after!r}; stream hardening must happen in the CLI entry "
        f"point, not at module import"
    )


# --- 3. The reported command's own prose must stay legible --------------------------

def test_submission_status_trail_is_cp1252_safe() -> None:
    """The status trail printed by the reported `submit` command contains → (U+2192),
    which is NOT cp1252-encodable and degrades to `?` — destroying the arrow's meaning
    on exactly the surface the bug was reported against."""
    from atv_bench.submit import submission_status_trail

    for step in submission_status_trail(is_first_time=True):
        try:
            step.encode("cp1252")
        except UnicodeEncodeError as e:  # noqa: PERF203
            pytest.fail(f"status trail step is not cp1252-safe ({e}): {step!r}")


# --- 4. Marks must track the LIVE stream, not a stale first answer ------------------

def test_marks_follow_stdout_swaps_within_a_process() -> None:
    """`_GLYPHS_OK` was memoized once per process, so the first console a mark was
    resolved against decided every later answer. That is wrong in any embedding that
    swaps sys.stdout — Click's CliRunner does exactly this per invocation — and it
    breaks in both directions: UTF-8 output stuck on ASCII fallbacks, or real glyphs
    emitted to a cp1252 stream where they degrade to `?`.
    """
    from atv_bench import cli

    utf8 = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    legacy = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace")
    original = sys.stdout
    try:
        sys.stdout = utf8
        assert cli.ok_mark() == "✓", "UTF-8 stream should render the real glyph"
        sys.stdout = legacy
        assert cli.ok_mark() == "[OK]", (
            "after swapping to a cp1252 stream the mark must degrade to ASCII, "
            "not return the stale UTF-8 answer"
        )
        sys.stdout = utf8
        assert cli.ok_mark() == "✓", "swapping back to UTF-8 must restore the real glyph"
    finally:
        sys.stdout = original
