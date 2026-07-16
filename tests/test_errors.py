"""Tests for user-facing errors (devex T4).

Every user-facing error carries: problem + cause + fix + docs link. The test
asserts the rendered string contains an actionable fix and a docs URL — enums are
not actionable messages.
"""
from __future__ import annotations

import pytest

from atv_bench.errors import AtvError, ErrorCode


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
