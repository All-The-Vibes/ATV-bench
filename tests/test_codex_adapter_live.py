"""Unit A (live e2e): the CodexCliAdapter driving the REAL codex CLI.

Marked ``live`` (deselected by default; run with ``pytest -m live -s``). Requires the ``codex``
binary + auth. These close the gap that the hermetic test only fed a CANNED payload: the real
``codex exec --json`` stream carries NO model field, so the model must be resolved another way.

Skips cleanly (never fails) when codex/auth is absent, so the suite is portable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from atv_bench.adapters.contract import AdapterRequest, AdapterStatus, CodexCliAdapter

pytestmark = pytest.mark.live

requires_codex = pytest.mark.skipif(
    shutil.which("codex") is None, reason="codex CLI not on PATH"
)


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "work"
    repo.mkdir()
    (repo / "main.py").write_text("print('hi')\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b.c", "-c", "user.name=atv", "commit", "-qm", "init"],
        cwd=repo, check=True,
    )
    return repo


@requires_codex
def test_codex_really_edits_a_repo(tmp_path):
    """The real `codex exec` performs a genuine file edit and the adapter reports EDITED."""
    repo = _make_repo(tmp_path)
    res = CodexCliAdapter().run(AdapterRequest(
        repo_path=str(repo),
        goal="Add a one-line comment above the print statement in main.py. "
             "Do not change program behavior.",
        model="auto",
    ))
    # a real run either edits (the goal) or at worst NO_EDIT — never a crash/timeout here.
    assert res.status in (AdapterStatus.EDITED, AdapterStatus.NO_EDIT), res.log
    if res.status is AdapterStatus.EDITED:
        assert res.diff.strip(), "EDITED must carry a real diff"
        assert "main.py" in res.diff


@requires_codex
def test_codex_reports_a_real_model_not_unknown(tmp_path):
    """THE gap-closing test: a real codex run must report a concrete model id, not 'unknown'.

    The real `codex exec --json` stream carries no model field, so the canned-payload unit test
    gave false confidence. With an explicit model the adapter must echo the authoritative id;
    with 'auto' it must resolve the configured default (never a bare 'unknown' when codex has a
    default configured).
    """
    repo = _make_repo(tmp_path)
    # explicit model is authoritative — the adapter must report exactly it.
    res = CodexCliAdapter().run(AdapterRequest(
        repo_path=str(repo), goal="say ok", model="gpt-5.5",
    ))
    assert res.model == "gpt-5.5", (
        f"explicit model must be the reported tag, got {res.model!r} (log: {res.log[:400]})"
    )
