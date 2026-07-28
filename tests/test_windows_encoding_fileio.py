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
import re
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
    offenders = []
    for root in _scanned_roots():
      for path in sorted(root.rglob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ".write_text(" not in line or "encoding=" in line:
                continue
            if "json.dumps" in line:
                continue  # ensure_ascii=True → ASCII-only payload, safe on any codepage
            if ".read_text()" in line or "html" in line.lower():
                offenders.append(f"{path}:{lineno}: {line.strip()}")
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
    for scan_root in _scanned_roots():
        for path in sorted(scan_root.rglob("*.py")):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if ".read_text()" not in line:
                    continue
                offenders.append(f"{path}:{lineno}: {line.strip()}")
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

def _scanned_roots() -> list[Path]:
    """Every tree whose Python is shipped and executed, not just the importable package.

    `arena/pkg/atv_bench/` is a SECOND copy of the arena package, baked into the Docker
    image that actually runs matches. Rooting a scan at `atv_bench.__file__` silently
    excludes it, so a revert there would sail past the guards while a byte-identity test
    elsewhere is the only thing holding the two copies in sync.
    """
    import atv_bench

    src_root = Path(atv_bench.__file__).parent
    roots = [src_root]
    baked = src_root.parent.parent / "arena" / "pkg" / "atv_bench"
    if baked.is_dir():
        roots.append(baked)
    return roots


def test_subprocess_text_capture_pins_utf8() -> None:
    """The THIRD vector of the same bug class (santa-loop round 3).

    `subprocess.run(..., text=True)` with no `encoding=` decodes the child's bytes with
    the locale codec under a STRICT handler. On Windows that is cp1252, which has five
    unmapped bytes (0x81 0x8D 0x8F 0x90 0x9D). Git and gh routinely emit UTF-8 curly
    quotes — `error: pathspec 'feature”x' did not match` encodes to ...e2 80 9d... — so
    reading tool output raises UnicodeDecodeError on exactly the `submit` path that was
    reported crashing. Neither stdout hardening nor the file-I/O sweep reaches this.
    """
    offenders = []
    for root in _scanned_roots():
        for path in sorted(root.rglob("*.py")):
            src = path.read_text(encoding="utf-8")
            # Parse with `ast`, not a regex. A regex that anchors on the closing paren
            # silently skips single-line calls — which is how the most important site
            # (submit.py's live command runner, the exact `submit` path this PR fixes)
            # evaded an earlier draft of this guard while it still reported green.
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
                    offenders.append(f"{path}:{node.lineno}")
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
    offenders = []
    for root in _scanned_roots():
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
            offenders.append(f"{path}:{lineno}: {stripped}")
    assert not offenders, (
        "stdin read without an explicit encoding — decodes piped git output with the "
        "locale codepage (cp1252 on Windows, strict):\n" + "\n".join(offenders)
    )


def test_validate_pr_accepts_utf8_paths_on_a_cp1252_stdin() -> None:
    """Behavioural: pipe a UTF-8 path that cp1252 cannot decode into the real command.

    The command is `validate-pr-paths`, NOT `validate-pr`. An earlier draft of this test
    used the latter, which does not exist — Typer answered with "No such command" and
    exited 2 without ever reading stdin, so the test passed while proving nothing. Assert
    on the command's real output, not merely on the absence of a traceback, so the same
    mistake cannot recur silently.
    """
    payload = "league/submissions/alice/main.py\nleague/submissions/”x/main.py\n"
    raw = payload.encode("utf-8")

    # Non-vacuous: this really is undecodable under the Windows default codepage.
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp1252")

    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "validate-pr-paths", "--author", "alice"],
        input=raw, capture_output=True, timeout=60,
        # Inherit the full environment (Windows needs SYSTEMROOT) and force the child's
        # stdio onto cp1252 so this exercises the real Windows decode path everywhere.
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    )
    combined = proc.stdout + proc.stderr
    assert b"UnicodeDecodeError" not in combined, (
        "validate-pr-paths died decoding a UTF-8 path from stdin under a cp1252 locale:\n"
        + combined.decode("utf-8", errors="replace")
    )
    # Proof it actually REACHED the validator rather than bailing out in argument parsing:
    # the second path is outside alice's tree, so the confinement check must reject it.
    assert b"No such command" not in combined, "the test invoked a command that does not exist"
    text = combined.decode("utf-8", errors="replace")
    assert "outside its own submission tree" in text or "not confined" in text, (
        "expected a confinement verdict proving stdin was read and parsed; got:\n" + text
    )


# --- 1d. The first-run banner must stay legible on a legacy codepage ----------------

def test_banner_is_legible_on_a_cp1252_console() -> None:
    """The FIFTH vector (santa-loop round 3).

    The first-run banner is the very first thing a new Windows user sees. It is rendered
    through `rich`, whose default box style draws borders from U+2500-family glyphs, and
    it carries a 🥇 medal — none of which cp1252 can encode. The banner is wrapped in a
    bare `except Exception`, so it never *crashes*; instead the console hardening's
    errors="replace" turned it into a wall of ~108 literal `?` characters.

    That is precisely the failure mode this PR's own design rejects for status marks:
    "worse than an honest ASCII stand-in". A greeting made of question marks reads like a
    broken install, on the one platform whose users just hit a crash bug.
    """
    from atv_bench import banner

    art = banner.render_banner(ascii_only=True)
    unencodable = sorted({ch for ch in art if not _cp1252_encodable(ch)})
    assert not unencodable, (
        "the ASCII-safe banner still contains characters cp1252 cannot encode "
        f"({unencodable}); on a Windows console each becomes a literal '?'"
    )
    # Still a banner, not an empty string stripped of everything.
    assert "ATV" in art and "BENCH" in art


def test_banner_keeps_full_glyphs_on_a_utf8_console() -> None:
    """The ASCII fallback must not downgrade consoles that can render the real thing."""
    from atv_bench import banner

    art = banner.render_banner()
    assert banner.MEDAL in art, "a UTF-8 console lost the medal glyph to the fallback"


def _cp1252_encodable(ch: str) -> bool:
    try:
        ch.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


def test_no_bare_unencodable_glyphs_in_interactive_prompt_titles() -> None:
    """Sixth site of the legibility class: the questionary model picker.

    `interactive.py` marked the configured model with `←`, which cp1252 cannot encode, so
    a Windows user saw "gpt-5  ? your configured model" — the pointer degrading into what
    looks like a rendering bug. Interactive prompts are TTY-only, so no test that captures
    output would ever have caught it.
    """
    from atv_bench import interactive

    src = Path(interactive.__file__).read_text(encoding="utf-8")
    offenders = []
    for lineno, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#") or "_glyph(" in line:
            continue
        if "Choice(" not in line and "title=" not in line:
            continue
        bad = sorted({ch for ch in line if not _cp1252_encodable(ch)})
        if bad:
            offenders.append(f"{lineno}: {bad} in {stripped[:70]!r}")
    assert not offenders, (
        "interactive prompt titles carry glyphs cp1252 cannot encode; each renders as a "
        "bare '?' on a Windows console:\n" + "\n".join(offenders)
    )


def test_banner_production_path_selects_the_ascii_variant(tmp_path: Path) -> None:
    """Gate the SELECTOR, not just the renderer.

    An earlier draft asserted only on `render_banner(ascii_only=True)`, which proves the
    ASCII variant exists but not that production ever chooses it. `maybe_show_banner()`
    makes that decision, and it is skipped for a non-TTY — so the cp1252 CI run (which
    redirects stdout) never exercises it either. Reverting the selector would have left
    every guard green while real Windows users got a wall of `?`.

    Drive the real entry point with a cp1252 stream and assert on the bytes it wrote.
    """
    from atv_bench import banner

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="replace")
    shown = banner.maybe_show_banner(
        sentinel=tmp_path / ".banner_shown_v1",
        is_tty=True, json_mode=False, env_suppressed=False, stream=stream,
    )
    assert shown, "the banner did not render; this test would prove nothing"
    stream.flush()
    written = stream.buffer.getvalue().decode("cp1252")
    assert "?" not in written, (
        "the production banner path emitted lossy '?' on a cp1252 console:\n" + written
    )
    assert "ATV" in written and "BENCH" in written, "the banner lost its wordmark"


def test_banner_production_path_keeps_glyphs_on_utf8(tmp_path: Path) -> None:
    """The selector must not downgrade a console that can render the real banner."""
    from atv_bench import banner

    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    assert banner.maybe_show_banner(
        sentinel=tmp_path / ".banner_shown_v1",
        is_tty=True, json_mode=False, env_suppressed=False, stream=stream,
    )
    stream.flush()
    written = stream.buffer.getvalue().decode("utf-8")
    assert banner.MEDAL in written, "a UTF-8 console was downgraded to the ASCII banner"


def test_model_picker_survives_an_unimportable_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fail-soft marker import must actually be exercised, not merely written.

    `interactive.select_model` pulls `_glyph` from `atv_bench.cli` purely to decorate the
    configured model. An earlier commit imported it unguarded, which made a cosmetic
    marker able to raise ImportError out of a picker that would otherwise have worked
    (slim install, partial environment). The static prompt-title guard cannot catch a
    revert of the try/except, and every other interactive test imports `cli` successfully
    — so nothing covered it. Force the import to fail and require selection to proceed.
    """
    import builtins

    from atv_bench import interactive

    real_import = builtins.__import__

    def exploding_import(name, *args, **kwargs):
        if name == "atv_bench.cli":
            raise ImportError("simulated: atv_bench.cli unimportable")
        return real_import(name, *args, **kwargs)

    captured: dict[str, object] = {}

    class _Answer:
        def ask(self):
            return "model-b"

    def fake_select(message, choices, default=None):
        captured["titles"] = [c.title for c in choices]
        return _Answer()

    monkeypatch.setattr(builtins, "__import__", exploding_import)
    monkeypatch.setattr(interactive, "_questionary_select", fake_select)

    choices = [
        interactive.ModelChoice("model-a", "Model A", is_current=True),
        interactive.ModelChoice("model-b", "Model B"),
    ]
    assert interactive.select_model(choices, non_interactive=False) == "model-b"

    titles = captured["titles"]
    assert any("<-" in t for t in titles), (
        "the ASCII fallback marker was not used when atv_bench.cli failed to import; "
        f"got {titles!r}"
    )


def test_command_help_text_is_cp1252_legible() -> None:
    """Command docstrings BECOME `--help` output, so they need the same discipline.

    `submit`'s docstring described the flow with `→`, which cp1252 cannot encode. The
    console hardening kept it from crashing, so it degraded instead:

        (fork ? branch ? stage under league/submissions/<identity>/ ? commit ? push ? PR)

    Five arrows became five question marks in the help text for `submit` — the exact
    command from the original bug report. Non-crashing but illegible is the failure mode
    this branch already rejects for status marks.

    Only Typer COMMAND docstrings are checked: those are rendered to a console. Internal
    helper docstrings are read in source, where `→` is fine and clearer than `->`.
    """
    offenders = []
    for root in _scanned_roots():
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                is_command = any(
                    "command" in ast.dump(dec) or "callback" in ast.dump(dec)
                    for dec in node.decorator_list
                )
                if not is_command:
                    continue
                doc = ast.get_docstring(node) or ""
                bad = sorted({ch for ch in doc if not _cp1252_encodable(ch)})
                if bad:
                    offenders.append(f"{path}:{node.lineno} {node.name}(): {bad}")
    assert not offenders, (
        "Typer command docstrings become --help text; these carry glyphs cp1252 cannot "
        "encode, so each renders as a bare '?' on a Windows console:\n"
        + "\n".join(offenders)
    )


def test_submit_help_renders_without_replacement_chars() -> None:
    """Behavioural companion: run the real `submit --help` on a cp1252 stdout."""
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "submit", "--help"],
        capture_output=True, timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    )
    text = (proc.stdout + proc.stderr).decode("cp1252", errors="replace")
    assert "UnicodeEncodeError" not in text, "submit --help crashed on a cp1252 console"
    # The flow line is the one that degraded; assert on it directly rather than banning
    # every '?', since the help legitimately contains "...and run matches?".
    flow = [ln for ln in text.splitlines() if "fork" in ln and "branch" in ln]
    assert flow, "the submit flow line vanished from --help"
    assert "?" not in flow[0], (
        "submit --help still degrades its flow arrows into '?' on cp1252:\n" + flow[0]
    )


def test_unexpected_error_output_is_legible_on_cp1252() -> None:
    """Typer's rich pretty-tracebacks are a console surface too (fresh-reviewer finding).

    Rich frames its tracebacks with U+2500 box characters and a `❱` gutter marker, none of
    which cp1252 can encode. The console hardening keeps them from crashing, so an
    unexpected error rendered as:

        | ?  572         text = paths_file.read_text(encoding="utf-8")            |

    Crash-free but illegible — and it is the surface a confused user reaches for FIRST.
    Every other guard on this branch watches the happy path; nothing watched the failure
    path, which is exactly where a Windows user in trouble ends up.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "validate-pr-paths",
         "--author", "alice", "--paths-file", "."],
        capture_output=True, timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"},
    )
    assert proc.returncode != 0, "this probe must actually fail; otherwise it proves nothing"
    text = (proc.stdout + proc.stderr).decode("cp1252", errors="replace")
    degraded = [ln for ln in text.splitlines() if _has_degraded_glyph(ln)]
    assert not degraded, (
        "error output degrades glyphs into '?' on a cp1252 console:\n"
        + "\n".join(degraded[:8])
    )


def test_utf8_console_keeps_rich_tracebacks() -> None:
    """The fallback must not downgrade a console that CAN draw rich's frames."""
    proc = subprocess.run(
        [sys.executable, "-m", "atv_bench.cli", "validate-pr-paths",
         "--author", "alice", "--paths-file", "."],
        capture_output=True, timeout=60,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    text = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert "─" in text or "│" in text, (
        "a UTF-8 console lost rich's traceback frames to the ASCII fallback"
    )


def _has_degraded_glyph(line: str) -> bool:
    """Mirror of the CI gate's degraded-glyph shapes (.github/scripts/assert_cp1252_output.py)."""
    stripped = line.strip()
    if not stripped:
        return False
    first = stripped.split(" ", 1)[0]
    if "?" in first:
        return True
    if " ? " in line:
        return True
    if stripped.endswith("?") and len(stripped) > 1 and stripped[-2].isspace():
        return True
    return bool(re.search(r"\?\s*[,;:)\]]", stripped))


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
