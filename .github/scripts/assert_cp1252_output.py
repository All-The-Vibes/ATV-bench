"""Assert a cp1252 console run stayed crash-free AND legible.

Used by the `windows-console-encoding` CI job. This is a Python script rather than a
chain of `findstr` calls because the two checks that matter most are exactly the ones
`cmd` makes treacherous:

  * `findstr /C:"?"` — `?` is a single-character wildcard in findstr, so the literal
    search a reviewer expects is not the search that runs.
  * `findstr /R` combined with `/C:` — the flags interact in ways that are easy to get
    subtly wrong and impossible to exercise on a Linux dev box.

Every assertion here is deliberately NON-VACUOUS: it must fail if the encoding fix is
reverted. In particular the status marks are asserted on a PREFLIGHT line (the loop that
actually crashed), not on doctor's `[OK] Python 3.x`, which setup-python guarantees and
which would pass even if every other mark were broken.
"""
from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: assert_cp1252_output.py <captured-output-file>", file=sys.stderr)
        return 2

    path = Path(argv[1])
    if not path.exists():
        print(f"FAIL: {path} does not exist — the CLI produced no captured output", file=sys.stderr)
        return 1

    # Read as cp1252: that is what the console actually emitted. `replace` here is for the
    # READER only, so a mojibake byte cannot crash the assertion script itself.
    text = path.read_text(encoding="cp1252", errors="replace")
    failures: list[str] = []

    if len(text.strip()) < 200:
        failures.append(
            f"output is only {len(text.strip())} chars — the commands produced almost "
            "nothing, so the other assertions would pass vacuously"
        )

    for marker in ("UnicodeEncodeError", "Traceback (most recent call last)", "charmap"):
        if marker in text:
            failures.append(f"crash signature {marker!r} present — the CLI died on a cp1252 console")

    # The preflight loop is the exact code path that crashed. Requiring a mark HERE (not on
    # doctor's guaranteed Python line) is what makes this gate non-vacuous.
    preflight = [ln for ln in text.splitlines() if "gh_installed" in ln]
    if not preflight:
        failures.append("no preflight line found — `submit` did not reach its preflight loop")
    elif not any(m in preflight[0] for m in ("[OK]", "[X]", "[-]")):
        failures.append(f"preflight line carries no ASCII status mark: {preflight[0]!r}")

    # A literal '?' where a GLYPH belongs means errors="replace" swallowed it: no crash,
    # but the reader cannot tell pass from fail. Scope this to positions where the CLI
    # emits a glyph — a leading status mark, or the known decorative-glyph prefixes —
    # rather than scanning free prose. `atv-bench --help` legitimately contains
    # "...submit, and run matches?", and a URL query string or a genuine question in a fix
    # hint would each trip a whole-line scan, making this gate flaky for reasons that have
    # nothing to do with encoding.
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        first = stripped.split(" ", 1)[0]
        # A '?' as the first token is a degraded status mark or decorative glyph.
        if "?" in first:
            failures.append(
                f"line {lineno} begins with a lossy replacement char where a glyph "
                f"belongs: {stripped!r}"
            )
        # "  ? detected on this machine" style: a '?' delimited by spaces mid-line is a
        # swallowed separator glyph (the arrow/marker forms the CLI prints).
        elif " ? " in line:
            failures.append(
                f"line {lineno} contains a lossy replacement char in a glyph position: "
                f"{stripped!r}"
            )

    if failures:
        print("cp1252 console assertions FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print("cp1252 console output is crash-free and legible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
