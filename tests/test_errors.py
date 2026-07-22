"""Tests for user-facing errors (devex T4).

Every user-facing error carries: problem + cause + fix + docs link. The test
asserts the rendered string contains an actionable fix and a docs URL — enums are
not actionable messages.
"""
from __future__ import annotations

import re

import pytest

from atv_bench.errors import AtvError, ErrorCode


def _fields(rendered: str) -> dict[str, str]:
    """Parse a rendered error into its labeled sections."""
    fields: dict[str, str] = {}
    for line in rendered.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip().lower()] = val.strip()
    return fields


def test_unified_error_render():
    """AtvError and RunError share ONE formatter shape: problem + cause + fix +
    exit code, so a user sees the same structure regardless of which subsystem
    raised the error (DX-3)."""
    from atv_bench.errors import render_error
    from atv_bench.run_envelope import RunError

    atv = AtvError(ErrorCode.GH_NOT_AUTHED, cause="not logged in")
    run = RunError("timeout", "the match timed out", fix="retry with more time")

    a = render_error(atv)
    r = render_error(run)

    for rendered in (a, r):
        low = rendered.lower()
        assert "problem:" in low, rendered
        assert "fix:" in low, rendered
        assert "exit" in low, rendered  # exit code surfaced

    # cause is included when present
    assert "not logged in" in a
    # both carry a numeric exit code
    assert re.search(r"exit\s*\d", a.lower())
    assert re.search(r"exit\s*\d", r.lower())


def test_error_renders_problem_cause_fix_link():
    err = AtvError(
        ErrorCode.GH_NOT_AUTHED,
        cause="gh CLI is installed but not logged in",
    )
    msg = str(err)
    assert "gh CLI is installed but not logged in" in msg  # cause
    assert "gh auth login" in msg                            # fix (actionable)
    assert "https://" in msg                                 # docs link
    # structured fields also available for machine rendering
    assert err.code == ErrorCode.GH_NOT_AUTHED
    assert err.fix
    assert err.docs_url.startswith("https://")


@pytest.mark.parametrize("code", list(ErrorCode))
def test_every_error_code_has_fix_and_link(code):
    err = AtvError(code, cause="x")
    assert err.fix, f"{code} missing fix"
    assert err.docs_url.startswith("https://"), f"{code} missing docs link"
    rendered = str(err)
    assert "Fix:" in rendered
    assert "Docs:" in rendered
