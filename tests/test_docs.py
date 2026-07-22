"""Docs-anchor resolution + exit-code documentation tests (DX-2, DX-5).

Errors and CLI help point users at `<doc>.md#anchor` links. A dangling anchor is
a dead end for a stuck user, so every anchor referenced from the code must exist
in its target doc. Also: every stable exit code must be documented.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _slugify(heading: str) -> str:
    """GitHub-style anchor slug from a markdown heading text."""
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)  # drop punctuation
    text = re.sub(r"\s+", "-", text)
    return text


def _anchors_for(doc: Path) -> set[str]:
    anchors: set[str] = set()
    for line in doc.read_text().splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line)
        if m:
            anchors.add(_slugify(m.group(1)))
    return anchors


def test_all_docs_anchors_resolve():
    """Every error code's docs_url must resolve to a REAL local file + REAL heading.

    We map the rendered URL back to a repo-relative path (this is what a user's
    browser hits), assert the file exists, then assert the #anchor matches a
    GitHub-slugified heading in that file. A path-prefix bug (e.g. a root doc
    served under /docs/) makes the local-file lookup fail here — which is exactly
    the failure a startswith("https://") check let slip through.
    """
    from atv_bench.errors import AtvError, ErrorCode

    # Anchor back-mapping at the fixed GitHub tree root — NOT the module's
    # _DOCS_BASE. If we stripped _DOCS_BASE and the base itself carried a bad
    # "/docs" prefix, we'd strip the bug away and pass wrongly. The tree root is
    # invariant, so a wrong /docs prefix survives into `rel` and fails the
    # file-exists check — which is the whole point.
    tree_root = "https://github.com/All-The-Vibes/ATV-bench/blob/main/"
    broken: list[str] = []
    checked = 0
    for code in ErrorCode:
        url = AtvError(code).docs_url
        assert url.startswith(tree_root), f"{code}: {url} not under {tree_root}"
        rel, _, anchor = url[len(tree_root):].partition("#")
        local = REPO / rel
        if not local.is_file():
            broken.append(f"{code.value}: file '{rel}' does not exist ({url})")
            continue
        if not anchor:
            broken.append(f"{code.value}: no #anchor in {url}")
            continue
        if anchor not in _anchors_for(local):
            broken.append(f"{code.value}: #{anchor} not a heading in {rel}")
            continue
        checked += 1
    assert checked == len(list(ErrorCode)), "not every code was resolved"
    assert not broken, "unresolvable docs links:\n" + "\n".join(broken)


def test_exit_codes_documented():
    """Every RunError exit code must appear in a documented exit-code table."""
    from atv_bench.run_envelope import EXIT_CODES

    docs_text = ""
    for name in ("README.md",):
        p = REPO / name
        if p.is_file():
            docs_text += p.read_text()
    for p in (REPO / "docs").rglob("*.md"):
        docs_text += p.read_text()

    missing = []
    for code, num in EXIT_CODES.items():
        # A documented row must name both the numeric code and its symbolic name.
        name_token = code.replace("_", "-")
        if not (re.search(rf"\b{num}\b", docs_text)
                and (code in docs_text or name_token in docs_text)):
            missing.append(f"{num} ({code})")
    assert not missing, f"exit codes not documented: {missing}"
