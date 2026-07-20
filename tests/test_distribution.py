"""Distribution / fresh-install polish tests (Section 7, ENG-C, DX-1/3/4/5/6).

These are deterministic doc/scan + render tests. They do NOT run a live `uvx`
install (that is a nightly job). They assert the shipped artifact tells a fresh,
tool-installed user (no vendor/ checkout) the truth about install + remediation.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"

# The broken remediation assumes a source checkout with vendor/CodeClash; a
# tool-installed user (uv tool install --from git+...) has no such directory.
_BROKEN = "uv pip install -e vendor/CodeClash"


def _codeclash_fix_texts() -> list[str]:
    """Every place the codeclash-dep (exit 9) remediation is surfaced to a user."""
    from atv_bench import preflight as pf
    from atv_bench.codeclash_env import CodeClashUnavailable, import_codeclash

    texts: list[str] = []
    # preflight check fix
    texts.append(pf.check_codeclash().fix)
    # the import-time error message
    try:
        import_codeclash()
    except CodeClashUnavailable as exc:
        texts.append(str(exc))
    return [t for t in texts if t]


def test_exit9_tool_install_remediation():
    """The exit-9 (codeclash-dep) remediation must WORK for a tool-installed user.

    codeclash is now a git dependency in pyproject, so the recovery is to
    reinstall the tool (which pulls the git dep) or run doctor — never the
    checkout-only `uv pip install -e vendor/CodeClash`.
    """
    texts = _codeclash_fix_texts()
    assert texts, "expected the codeclash-dep remediation to be surfaced somewhere"
    for t in texts:
        assert _BROKEN not in t, (
            f"codeclash-dep fix still tells a tool-installed user to run the "
            f"checkout-only command, which fails with no vendor/ dir:\n{t}"
        )
        # A working recovery: reinstall the tool from git, or run doctor.
        assert ("uv tool install" in t or "uv tool upgrade" in t
                or "--reinstall" in t or "atv-bench doctor" in t), (
            f"codeclash-dep fix gives no working remediation for a tool user:\n{t}"
        )


def test_uvx_invocation_string():
    """README must use a working install form, not bare `uvx atv-bench`.

    atv-bench is not on PyPI, so `uvx atv-bench` / `uv tool install atv-bench`
    (no source) cannot resolve. Every advertised install must name the git source.
    """
    text = README.read_text()
    # No bare uvx/uv-tool-install of atv-bench without a --from git+ source.
    bad = re.findall(r"^.*\buvx\s+atv-bench\b.*$", text, flags=re.MULTILINE)
    assert not bad, f"README advertises bare `uvx atv-bench` (not on PyPI): {bad}"
    bad_tool = re.findall(
        r"^.*uv tool install\s+(?!--from)atv-bench\b.*$", text, flags=re.MULTILINE
    )
    assert not bad_tool, f"README advertises source-less install: {bad_tool}"
    # And at least one correct git-source install is present.
    assert "git+https://github.com/All-The-Vibes/ATV-bench" in text, (
        "README must show the working git-source install form"
    )


def test_readme_tldr_first():
    """README leads with a short TL;DR whose demo command appears before ~line 30."""
    lines = README.read_text().splitlines()
    head = lines[:30]
    joined = "\n".join(head)
    assert "run --demo" in joined, (
        "README must surface `atv-bench run --demo` in a TL;DR before line ~30 "
        "(currently Quick start is buried ~line 102)"
    )
