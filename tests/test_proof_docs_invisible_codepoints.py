"""No invisible codepoints in agent-facing text.

AGENT-01 (the prompt-injection lens) treats invisible codepoints as an injection carrier:
a reader — human or model — cannot see them, so they can carry content that is not in the
visible string. The pr-review-2930 report both *reported* that class of finding and
*contained* one (a U+2060 WORD JOINER, on the very line describing the finding).

It survived because the remediation the report prescribed was a grep for U+200B/C/D/FEFF —
a hand-curated class that did not include U+2060. The lesson is not "add U+2060 to the
list"; it is **stop hand-curating the list**. This module classifies by Unicode category
(Cf = format characters) plus a small explicit set of non-Cf characters that render blank,
so a carrier nobody enumerated is still caught.

Scope note: the original finding was a U+200B inside `scripts/wf_pr_review_2324.js` — a
`.js` file. A scanner covering only `docs/**/*.md` would not see its own motivating case,
so this walks every tracked text file.
"""
from __future__ import annotations

import pathlib
import subprocess
import unicodedata

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Non-Cf characters that still render as blank/zero-width, so category alone misses them.
_EXTRA_INVISIBLE = {
    0x00AD,  # SOFT HYPHEN (Cf on most builds, listed for explicitness)
    0x3164,  # HANGUL FILLER (Lo, renders blank — classic filler carrier)
    0x115F,  # HANGUL CHOSEONG FILLER (Lo)
    0x1160,  # HANGUL JUNGSEONG FILLER (Lo)
    0x180E,  # MONGOLIAN VOWEL SEPARATOR
    0x2800,  # BRAILLE PATTERN BLANK (So, renders blank)
}

# Legitimately-needed format characters. Every entry here is a hole, so each is justified:
#
#   U+200D ZWJ / U+200C ZWNJ — required for correct rendering of Indic and Arabic scripts
#     and for emoji sequences (e.g. family glyphs). Banning them repo-wide would reject
#     correct human text, and a guard that rejects correct text gets deleted.
#   U+FE0F VARIATION SELECTOR-16 — emoji presentation selector; `⚠️` is `⚠` + U+FE0F.
#     Already present in committed plan docs as ordinary formatting.
#
# These remain the weakest carriers (they cannot reorder or hide a rendered instruction
# the way U+202E RLO or U+2060 WORD JOINER can). The high-severity carriers stay banned.
_ALLOWLIST: frozenset[int] = frozenset({0x200C, 0x200D, 0xFE0F})

# Extensions that are text and can carry agent-facing content.
_TEXT_SUFFIXES = {
    ".md", ".py", ".js", ".ts", ".yml", ".yaml", ".json", ".toml", ".txt",
    ".sh", ".cfg", ".ini", ".html",
}

# This file necessarily contains literal invisible characters (the probe fixtures below),
# so it excludes itself. Nothing else may.
_SELF = pathlib.Path(__file__).resolve()


def _is_invisible(ch: str) -> bool:
    """True if `ch` is a format character or an otherwise blank-rendering codepoint.

    Category-driven, NOT a hand-curated list — that is the failure mode this exists to
    prevent. `Cf` covers U+200B-200F, U+2060-2064, U+202A-202E (bidi overrides, the
    highest-severity carrier), U+2066-2069 (isolates), U+061C, and U+FEFF without anyone
    having to remember them.
    """
    cp = ord(ch)
    if cp in _ALLOWLIST:
        return False
    return unicodedata.category(ch) == "Cf" or cp in _EXTRA_INVISIBLE


def _tracked_text_files() -> list[pathlib.Path]:
    """Every tracked text file, via git — so a new directory is covered automatically."""
    try:
        out = subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "ls-files", "-z"],
            capture_output=True, check=True, text=True, timeout=60,
        ).stdout
    except (subprocess.SubprocessError, OSError):  # pragma: no cover - CI always has git
        return []
    files = []
    for rel in out.split("\0"):
        if not rel:
            continue
        p = _REPO_ROOT / rel
        if p.suffix.lower() in _TEXT_SUFFIXES and p.is_file() and p.resolve() != _SELF:
            files.append(p)
    return sorted(files)


_FILES = _tracked_text_files()


def test_scan_scope_is_not_empty() -> None:
    """Fail loudly if the scan discovered nothing.

    Without this, `parametrize([])` collects zero cases, pytest reports SKIP, and the
    suite exits 0 — the guard evaporates into a green check while enforcing nothing.
    That is the exact 'looks enforced, enforces nothing' failure the prescribed grep had.
    """
    assert len(_FILES) > 50, (
        f"invisible-codepoint scan discovered only {len(_FILES)} files — scope is broken"
    )


@pytest.mark.parametrize("doc", _FILES, ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_file_has_no_invisible_codepoints(doc: pathlib.Path) -> None:
    try:
        text = doc.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        # Fail closed and NAME the file. Never errors="ignore" — that would silently
        # discard the very bytes under investigation.
        pytest.fail(f"{doc.relative_to(_REPO_ROOT)}: not valid UTF-8, cannot scan ({exc})")
    # Scan the raw text, not splitlines(): str.splitlines() itself consumes U+2028/U+2029/
    # U+0085/U+000B/U+000C, so a line-based scan can never report them.
    hits = []
    line = 1
    for ch in text:
        if ch == "\n":
            line += 1
            continue
        if _is_invisible(ch):
            name = unicodedata.name(ch, "UNNAMED")
            hits.append(f"{doc.relative_to(_REPO_ROOT)}:{line}: U+{ord(ch):04X} {name}")
    assert not hits, "invisible codepoints found:\n" + "\n".join(hits)


@pytest.mark.parametrize("cp,label", [
    (0x2060, "WORD JOINER"),          # the character that caused this
    (0x200B, "ZERO WIDTH SPACE"),     # the original AGENT-01 finding
    (0x202E, "RIGHT-TO-LEFT OVERRIDE"),  # highest-severity: visually reorders text
    (0x2066, "LEFT-TO-RIGHT ISOLATE"),
    (0x2061, "FUNCTION APPLICATION"),
    (0x3164, "HANGUL FILLER"),        # non-Cf, needs the extra set
    (0x00AD, "SOFT HYPHEN"),
    (0xFEFF, "ZERO WIDTH NO-BREAK SPACE"),
])
def test_detector_catches_known_carriers(cp: int, label: str) -> None:
    """Guard the guard: most of these were missed by the originally-prescribed class.

    If someone narrows the detector back to a hand-curated set, these fail rather than
    silently passing — which is what let the original defect through.
    """
    assert _is_invisible(chr(cp)), f"detector missed U+{cp:04X} {label}"


def test_detector_does_not_flag_ordinary_text() -> None:
    """False positives would make the guard unusable, so it gets deleted. Guard that too."""
    for ch in "abcXYZ0189 \t\n.,;:!?-_/\\'\"()[]{}#@$%^&*+=<>|~`áéîöüßçñ日本語한글→—…":
        assert not _is_invisible(ch), f"false positive on {ch!r} (U+{ord(ch):04X})"
