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

import ast
import io
import os
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


def test_packaged_assets_are_read_utf8_not_locale_codepage() -> None:
    """The READ side of the same bug class (santa-loop round 3).

    The round-1 sweep guarded `write_text`, but `read_text()` with no encoding is just
    as locale-dependent. `src/atv_bench/view/shell.js` contains bytes that are not
    cp1252-DECODABLE (0x9d at offset 4062), so on a Windows cp1252 locale
    `play._shell_js()` raises UnicodeDecodeError and `atv-bench play` dies before it
    can write anything — a crash stdout hardening cannot reach.
    """
    import atv_bench

    root = Path(atv_bench.__file__).parent

    # The asset really is undecodable under cp1252 — this test is non-vacuous.
    shell = (root / "view" / "shell.js").read_bytes()
    with pytest.raises(UnicodeDecodeError):
        shell.decode("cp1252")

    offenders = []
    for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ".read_text()" not in line:
                continue
            offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "text reads without an explicit encoding (crash on Windows cp1252 for any "
        "asset with non-cp1252-decodable bytes):\n" + "\n".join(offenders)
    )


def test_shell_js_loads_under_a_cp1252_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    """Behavioural companion: simulate a cp1252 default locale and load the shell."""
    from atv_bench import play

    real_read_text = Path.read_text

    def locale_read_text(self: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if "encoding" not in kwargs and not args:
            return self.read_bytes().decode("cp1252")  # what Windows open() would do
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", locale_read_text)
    assert "canvas" in play._shell_js().lower()


# --- 1b. Subprocess text decoding must not use the locale codepage ------------------

def test_subprocess_text_capture_pins_utf8() -> None:
    """The THIRD vector of the same bug class (santa-loop round 3).

    `subprocess.run(..., text=True)` with no `encoding=` decodes the child's bytes with
    the locale codec under a STRICT handler. On Windows that is cp1252, which has five
    unmapped bytes (0x81 0x8D 0x8F 0x90 0x9D). Git and gh routinely emit UTF-8 curly
    quotes — `error: pathspec 'feature”x' did not match` encodes to ...e2 80 9d... — so
    reading tool output raises UnicodeDecodeError on exactly the `submit` path that was
    reported crashing. Neither stdout hardening nor the file-I/O sweep reaches this.
    """
    import atv_bench

    root = Path(atv_bench.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        # Parse with `ast`, not a regex. A regex that anchors on the closing paren silently
        # skips single-line calls — which is how the most important site (submit.py's live
        # command runner, the exact `submit` path this PR fixes) evaded an earlier draft of
        # this guard while it still reported green.
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "subprocess"
                    and fn.attr in ("run", "Popen", "check_output")):
                continue
            kw = {k.arg: k.value for k in node.keywords if k.arg}
            decodes_text = any(
                isinstance(kw.get(name), ast.Constant) and kw[name].value is True
                for name in ("text", "universal_newlines")
            )
            if decodes_text and "encoding" not in kw:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert not offenders, (
        "subprocess text captures without an explicit encoding — these decode child "
        "output with the locale codepage (cp1252 on Windows, strict) and raise "
        "UnicodeDecodeError on a curly quote in git/gh output:\n" + "\n".join(offenders)
    )


def test_curly_quote_in_tool_output_survives_cp1252_decode() -> None:
    """Behavioural proof the vector is real and that utf-8 pinning closes it."""
    payload = "error: pathspec 'feature”x' did not match".encode("utf-8")

    # cp1252 (what Windows text=True would use) genuinely cannot decode this.
    with pytest.raises(UnicodeDecodeError):
        payload.decode("cp1252")

    # The encoding we pin does, losslessly.
    assert "”" in payload.decode("utf-8")


# --- 1c. stdin must not be decoded with the locale codepage -------------------------

def test_stdin_read_pins_utf8() -> None:
    """The FOURTH vector (santa-loop round 3).

    `sys.stdin.read()` decodes with the locale codec too. `atv-bench validate-pr` is fed
    by `git diff --name-only | atv-bench validate-pr`, and git emits path bytes as UTF-8.
    A submission directory containing a curly quote (…e2 80 9d…) or any char whose UTF-8
    encoding includes a cp1252-unmapped byte makes the read raise UnicodeDecodeError
    before validation runs — so the PR gate crashes instead of rendering a verdict.
    """
    import atv_bench

    root = Path(atv_bench.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            if "sys.stdin.read()" not in stripped or stripped.startswith("#"):
                continue
            # A read guarded by an explicit sys.stdin.buffer decode is the FIX, not the
            # bug: that branch only runs for an already-decoded stream (pytest capture,
            # custom stdin) where there are no raw bytes left to re-decode.
            if "buffer.read().decode" in src:
                continue
            offenders.append(f"{path.relative_to(root)}:{lineno}: {stripped}")
    assert not offenders, (
        "stdin read without an explicit encoding — decodes piped git output with the "
        "locale codepage (cp1252 on Windows, strict):\n" + "\n".join(offenders)
    )


def test_validate_pr_accepts_utf8_paths_on_a_cp1252_stdin() -> None:
    """Behavioural: pipe a UTF-8 path that cp1252 cannot decode into `validate-pr`."""
    payload = "league/submissions/alice/main.py\nleague/submissions/”x/main.py\n"
    raw = payload.encode("utf-8")

    # Non-vacuous: this really is undecodable under the Windows default codepage.
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1252")

    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "validate-pr", "--author", "alice"],
        input=raw, capture_output=True, timeout=60,
        # Inherit the full environment (Windows needs SYSTEMROOT) and force the child's
        # stdio onto cp1252 so this exercises the real Windows decode path everywhere.
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    )
    combined = proc.stdout + proc.stderr
    assert b"UnicodeDecodeError" not in combined, (
        "validate-pr died decoding a UTF-8 path from stdin under a cp1252 locale:\n"
        + combined.decode("utf-8", errors="replace")
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
