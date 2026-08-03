"""No invisible codepoints in committed proof/evidence docs.

AGENT-01 (the prompt-injection lens) treats invisible codepoints in agent-facing text as
an injection carrier: a reader — human or model — cannot see them, so they can carry
content that is not in the visible string. The pr-review-2930 report both *reported* that
class of finding and *contained* one (a U+2060 WORD JOINER, on the very line describing
the finding), which is how it got missed: the remediation the report prescribed was a grep
for U+200B/C/D/FEFF, a character class that does not include U+2060.

This test is that grep, widened to the codepoints that actually matter and wired to CI so
the guarantee is enforced rather than prescribed.
"""
from __future__ import annotations

import pathlib

import pytest

# Zero-width and invisible formatting characters. U+2060 is included deliberately: its
# omission from the originally-prescribed class is the exact reason the defect survived.
INVISIBLE = {
    0x200B: "ZERO WIDTH SPACE",
    0x200C: "ZERO WIDTH NON-JOINER",
    0x200D: "ZERO WIDTH JOINER",
    0x2060: "WORD JOINER",
    0x00AD: "SOFT HYPHEN",
    0xFEFF: "ZERO WIDTH NO-BREAK SPACE / BOM",
    0x200E: "LEFT-TO-RIGHT MARK",
    0x200F: "RIGHT-TO-LEFT MARK",
}

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_DOC_ROOTS = ("docs/proof", "docs/plans")


def _docs() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for root in _DOC_ROOTS:
        base = _REPO_ROOT / root
        if base.is_dir():
            out.extend(sorted(base.rglob("*.md")))
    return out


@pytest.mark.parametrize("doc", _docs(), ids=lambda p: str(p.relative_to(_REPO_ROOT)))
def test_doc_has_no_invisible_codepoints(doc: pathlib.Path) -> None:
    text = doc.read_text(encoding="utf-8")
    hits = [
        f"{doc.relative_to(_REPO_ROOT)}:{lineno}: U+{ord(ch):04X} {INVISIBLE[ord(ch)]}"
        for lineno, line in enumerate(text.splitlines(), 1)
        for ch in line
        if ord(ch) in INVISIBLE
    ]
    assert not hits, "invisible codepoints found:\n" + "\n".join(hits)


def test_scanner_actually_detects_word_joiner(tmp_path: pathlib.Path) -> None:
    """Guard the guard: a scanner that misses U+2060 is the bug that caused this.

    Without this, narrowing INVISIBLE back to the original U+200B/C/D/FEFF class would
    leave every test above passing while the defect class returned.
    """
    assert 0x2060 in INVISIBLE
    probe = tmp_path / "probe.md"
    probe.write_text("cleanly/⁠safely resolve\n", encoding="utf-8")
    found = [ch for ch in probe.read_text(encoding="utf-8") if ord(ch) in INVISIBLE]
    assert found, "scanner failed to flag an embedded U+2060"
