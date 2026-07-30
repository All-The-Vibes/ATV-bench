"""Issue #32 — CRLF bots must not break `bot_sha256` / provenance binding.

`open_submission_pr` copies the bot into the PR tree. If that copy is text-mode, a bot
authored with CRLF line endings is committed as LF, so the bytes the store hashes are not
the bytes `build_submission` hashed. `store.py` then silently restamps `bot_sha256`, the
provenance token stops binding, and `load_submissions()` raises ValueError — rejecting the
WHOLE league, not just the offending row.

Fails closed: an availability/integrity defect, not forgery or an authz bypass.

The copy must therefore be byte-preserving. These tests pin the byte-level invariant, the
end-to-end store load, and the locale-codepage half of the same defect (the bare
`read_text()` at the copy site decodes with the platform codepage).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from atv_bench.store import LeagueStore
from atv_bench.submit import build_submission, open_submission_pr


_CRLF_BOT = b"def move(state):\r\n    return 'up'\r\n"


def _fingerprint():
    return {
        "harness": "claude-code", "model": "claude-opus-4-8", "gstack": True,
        "skills": ["gstack"], "mcps": [], "plugins": [], "custom_agents_count": 0,
        "unknown": [], "probe_version": "1.0.0",
    }


class _Runner:
    """Scripted gh/git runner: records calls, never touches a real remote."""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for needle, res in self.results.items():
            if needle in joined:
                return res
        return (0, "", "")


def _submit(tmp_path, bot_bytes, identity="octocat"):
    """Run the real submit path over `bot_bytes`; return (record, committed main.py)."""
    bot = tmp_path / "bot.py"
    bot.write_bytes(bot_bytes)
    record = build_submission(bot_path=str(bot), fingerprint=_fingerprint(),
                              identity=identity, game="battlesnake")
    wt = tmp_path / "wt"
    open_submission_pr(
        record=record, bot_path=str(bot), identity=identity,
        runner=_Runner({"pr create": (0, "https://github.com/x/y/pull/1\n", "")}),
        workdir=str(wt),
    )
    return record, wt / "league" / "submissions" / identity / "main.py"


def test_crlf_bot_is_committed_byte_identical(tmp_path):
    """The copy into the PR tree must preserve bytes exactly. Text-mode IO translates
    CRLF to LF here, which is the origin of the whole failure chain."""
    _record, committed = _submit(tmp_path, _CRLF_BOT)
    assert committed.read_bytes() == _CRLF_BOT


def test_crlf_bot_sha256_survives_the_commit(tmp_path):
    """The capture-time hash (submit.py, over the original bytes) must equal the hash the
    store recomputes over the committed bytes -- otherwise store.py restamps the field."""
    record, committed = _submit(tmp_path, _CRLF_BOT)
    committed_sha = hashlib.sha256(committed.read_bytes()).hexdigest()
    assert record["bot_sha256"] == committed_sha


def test_crlf_submission_loads_without_provenance_error(tmp_path):
    """End-to-end: the merged tree must load. This is the league-wide failure -- a single
    CRLF entrant raises ValueError out of load_submissions() and takes the board down."""
    record, committed = _submit(tmp_path, _CRLF_BOT)
    league = tmp_path / "league_root"
    dest = league / "submissions" / "octocat"
    dest.mkdir(parents=True)
    (dest / "main.py").write_bytes(committed.read_bytes())
    (dest / "submission.json").write_text(json.dumps(record, indent=2, sort_keys=True))

    subs = LeagueStore(str(league)).load_submissions()

    assert set(subs) == {"octocat"}
    assert subs["octocat"]["bot_sha256"] == record["bot_sha256"]


_LOCALE_PROBE = '''
import hashlib, sys
from pathlib import Path
from atv_bench.submit import build_submission, open_submission_pr

class _R:
    def __call__(self, cmd, **kw):
        if "pr create" in " ".join(cmd):
            return (0, "https://github.com/x/y/pull/1\\n", "")
        return (0, "", "")

tmp = Path(sys.argv[1])
raw = "# cafe\\u0301 bot \\u2014 non-ascii\\ndef move(state):\\n    return 'up'\\n".encode("utf-8")
bot = tmp / "bot.py"
bot.write_bytes(raw)
rec = build_submission(
    bot_path=str(bot),
    fingerprint={"harness": "claude-code", "model": "m", "gstack": True, "skills": [],
                 "mcps": [], "plugins": [], "custom_agents_count": 0, "unknown": [],
                 "probe_version": "1.0.0"},
    identity="octocat", game="battlesnake")
open_submission_pr(record=rec, bot_path=str(bot), identity="octocat",
                   runner=_R(), workdir=str(tmp / "wt"))
got = (tmp / "wt" / "league" / "submissions" / "octocat" / "main.py").read_bytes()
assert got == raw, "committed bytes differ from source bytes"
assert hashlib.sha256(got).hexdigest() == rec["bot_sha256"], "hash diverged"
print("OK")
'''


def test_non_ascii_bot_survives_commit_under_non_utf8_locale(tmp_path):
    """The copy site's `read_text()`/`write_text()` carry no `encoding=`, so they decode
    with the locale codepage -- the same defect class PR #29 fixed elsewhere. A non-ASCII
    UTF-8 bot passes `validate_bot_shape` (it IS valid UTF-8) but is mangled or rejected on
    a non-UTF-8 host (cp1252 on Windows; C/ASCII here). Byte-preserving IO closes this and
    the CRLF half at once.

    Run in a subprocess because the interpreter's locale encoding is fixed at startup.
    """
    script = tmp_path / "probe.py"
    script.write_text(_LOCALE_PROBE, encoding="utf-8")
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([sys.executable, str(script), str(tmp_path)],
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_gitattributes_pins_submitted_bots_as_binary():
    """Byte-preserving IO in submit.py is NOT sufficient on its own.

    A Windows contributor typically has `core.autocrlf=true`, which translates CRLF to LF
    at COMMIT time -- after `write_bytes` has already put correct bytes on disk. The blob
    git actually stores would still be LF, so the hash still diverges and the league still
    fails to load. The repo must therefore mark the submitted bot path as non-text so git
    never rewrites it in either direction.
    """
    repo_root = Path(__file__).resolve().parents[1]
    attrs = repo_root / ".gitattributes"
    assert attrs.is_file(), (
        ".gitattributes is required: without it core.autocrlf=true rewrites submitted "
        "bots at commit time and defeats the byte-preserving copy (issue #32)"
    )
    body = attrs.read_text(encoding="utf-8")
    rule = [ln for ln in body.splitlines()
            if "league/submissions" in ln and "main.py" in ln]
    assert rule, "no .gitattributes rule covering league/submissions/**/main.py"
    assert any("-text" in ln for ln in rule), (
        f"submitted bots must be marked -text (no newline translation); got: {rule}"
    )


def test_git_preserves_crlf_bot_through_commit_under_autocrlf(tmp_path):
    """End-to-end against a REAL git repo with the Windows-default autocrlf=true: the
    committed blob must be byte-identical to the bot on disk. This is what store.py
    actually hashes after checkout on the Linux CI runner."""
    repo_root = Path(__file__).resolve().parents[1]
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)

    git("init", "-q", ".")
    git("config", "core.autocrlf", "true")   # the Windows default -- the whole point
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / ".gitattributes").write_bytes(
        (repo_root / ".gitattributes").read_bytes())
    git("add", ".gitattributes")
    git("commit", "-qm", "attrs")

    dest = repo / "league" / "submissions" / "octocat"
    dest.mkdir(parents=True)
    (dest / "main.py").write_bytes(_CRLF_BOT)
    git("add", "league/submissions")
    git("commit", "-qm", "bot")

    blob = subprocess.run(["git", "cat-file", "-p", "HEAD:league/submissions/octocat/main.py"],
                          cwd=repo, capture_output=True)
    assert blob.stdout == _CRLF_BOT, (
        "git rewrote the bot at commit time; bot_sha256 will diverge league-wide"
    )
    assert hashlib.sha256(blob.stdout).hexdigest() == hashlib.sha256(_CRLF_BOT).hexdigest()


def test_store_add_submission_pins_utf8_when_writing_bot_and_record(tmp_path):
    """`LeagueStore.add_submission` writes submission.json and the sibling main.py whose
    bytes back the re-derived `bot_sha256` (store.py:139,143). Both are bare `write_text()`
    with no `encoding=`, so on a non-UTF-8 host a non-ASCII bot or record is mangled or
    raises -- the same locale-codepage defect class as submit.py:367.

    Newline translation is NOT the risk on this path (write_text preserves an explicit
    \\r\\n); the encoding is. Assert the source pins it rather than asserting behaviour
    that is already correct on a UTF-8 host and would pass green-on-arrival.
    """
    import inspect

    from atv_bench.store import LeagueStore

    src = inspect.getsource(LeagueStore.add_submission)
    writes = [ln.strip() for ln in src.splitlines() if ".write_text(" in ln]
    assert writes, "expected write_text calls in add_submission"
    unpinned = [ln for ln in writes if "encoding=" not in ln and not ln.endswith("(")]
    assert not unpinned, (
        "add_submission must pin encoding=utf-8 when writing the record and the bot "
        f"bytes that back bot_sha256; unpinned: {unpinned}"
    )


def test_backfill_rewrite_of_record_is_utf8(tmp_path):
    """The PR-url backfill rewrites submission.json (submit.py:400) with a bare
    `write_text()`. On a non-UTF-8 host a record carrying non-ASCII (e.g. a model or skill
    name) raises UnicodeEncodeError there. That path is inside a try/except AtvError, so
    the error escapes uncaught AFTER the PR is already open."""
    import inspect

    from atv_bench import submit as submit_mod

    src = inspect.getsource(submit_mod.open_submission_pr)
    backfill_writes = [
        ln.strip() for ln in src.splitlines()
        if "write_text" in ln and "submission.json" in ln
    ]
    assert backfill_writes, "expected a submission.json write in the backfill path"
    for ln in backfill_writes:
        assert "encoding=" in ln or ln.endswith("("), (
            f"submission.json write must pin encoding=utf-8: {ln}"
        )
