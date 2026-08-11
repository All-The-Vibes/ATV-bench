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
import textwrap
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


def test_store_add_submission_writes_bot_and_record_byte_exactly(tmp_path):
    """`LeagueStore.add_submission` writes submission.json and the sibling main.py whose
    bytes back the re-derived `bot_sha256` (store.py). Both must be byte-exact.

    `write_text` opens with newline=None, so "\\n" becomes os.linesep ON WRITE, and with
    no encoding= the locale codepage applies. Pinning encoding= alone fixes only the
    codec half -- the same half-fix this PR faults #29 for. Assert the bytes on disk,
    not the source text, so the test goes red on a mangled write regardless of how the
    call is formatted. The companion test below covers the newline half, which this one
    cannot observe on a Linux host where os.linesep is already "\\n".
    """
    from atv_bench.store import LeagueStore

    crlf_bot = "def move(s):\r\n    return 'up'\r\n"
    rec = {
        "identity": "octocat", "game": "snake",
        "bot_sha256": hashlib.sha256(crlf_bot.encode("utf-8")).hexdigest(),
        "fingerprint": "fp", "pr_url": "https://example.com/pr/1",
        "logs_url": "https://example.com/logs/1",
    }
    store = LeagueStore(str(tmp_path))
    store.add_submission(rec, bot_source=crlf_bot)

    bot_path = tmp_path / "submissions" / "octocat" / "main.py"
    assert bot_path.read_bytes() == crlf_bot.encode("utf-8"), (
        "add_submission mangled the bot bytes that back bot_sha256"
    )
    assert (
        hashlib.sha256(bot_path.read_bytes()).hexdigest() == rec["bot_sha256"]
    ), "committed bot bytes no longer hash to the published bot_sha256"

    record_path = tmp_path / "submissions" / "octocat" / "submission.json"
    assert record_path.read_bytes() == json.dumps(
        rec, indent=2, sort_keys=True
    ).encode("utf-8"), "add_submission did not write the record byte-exactly"


def test_store_add_submission_writes_an_explicit_empty_bot_verbatim(tmp_path):
    """Only an ABSENT bot_source falls back to the canned default bot.

    A falsy guard (`if not bot_source`) would substitute the default for an explicit
    b"" / "" while the caller's bot_sha256 hashed the empty file -- silently
    desynchronizing the published hash from the committed bytes, which is the exact
    class of defect this PR exists to close.
    """
    from atv_bench.store import LeagueStore

    for i, empty in enumerate((b"", "")):
        ident = f"octocat{i}"
        rec = {
            "identity": ident, "game": "snake",
            "bot_sha256": hashlib.sha256(b"").hexdigest(),
            "fingerprint": "fp", "pr_url": "https://example.com/pr/1",
            "logs_url": "https://example.com/logs/1",
        }
        LeagueStore(str(tmp_path)).add_submission(rec, bot_source=empty)
        written = (tmp_path / "submissions" / ident / "main.py").read_bytes()
        assert written == b"", (
            f"explicit empty bot_source={empty!r} was replaced by the default bot; "
            "committed bytes no longer match the published bot_sha256"
        )
        assert hashlib.sha256(written).hexdigest() == rec["bot_sha256"]


def test_store_add_submission_defaults_only_when_bot_source_is_absent(tmp_path):
    """The default-bot fallback still applies when bot_source is omitted entirely."""
    from atv_bench.store import LeagueStore

    rec = {
        "identity": "octocat", "game": "snake",
        "bot_sha256": "0" * 64, "fingerprint": "fp",
        "pr_url": "https://example.com/pr/1", "logs_url": "https://example.com/logs/1",
    }
    LeagueStore(str(tmp_path)).add_submission(rec)
    written = (tmp_path / "submissions" / "octocat" / "main.py").read_bytes()
    assert written == b"def move(state):\n    return 'up'\n"


def test_store_add_submission_is_byte_exact_on_a_crlf_host():
    """Platform-independent proof of the newline half.

    On Linux os.linesep is "\\n", so a text-mode write is accidentally byte-exact and the
    behavioural test above cannot distinguish write_text from write_bytes here. Monkey-
    patching os.linesep does not help either -- the io module captures it below the Python
    level, so a patched write_text still emits "\\n" and such a test would itself be
    vacuous. Assert the call form instead, via AST so that reformatting the call (the
    exact hole that made the previous source-grep tests green-on-arrival) cannot hide it.
    """
    import ast
    import inspect

    from atv_bench.store import LeagueStore

    tree = ast.parse(textwrap.dedent(inspect.getsource(LeagueStore.add_submission)))
    text_writes = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "write_text"
    ]
    assert not text_writes, (
        "add_submission must use write_bytes for the record and the bot bytes that back "
        f"bot_sha256 -- write_text translates newlines on Windows: {text_writes}"
    )


def test_backfill_rewrite_of_record_is_byte_exact():
    """The PR-url backfill rewrites submission.json (submit.py). json.dumps defaults to
    ensure_ascii=True, so the codepage risk this test previously claimed is NOT reachable
    on this path -- but indent=2 emits "\\n", and a text-mode write translates those to
    os.linesep on Windows. Assert the reachable half: the record write must be binary.

    Source-level assertion (not behavioural) because the backfill sits inside
    open_submission_pr's gh/git orchestration; the surrounding path is covered by the
    live-submit tests. The continuation-line exemption that made the previous version
    vacuous is gone: the source is parsed with `ast`, so the call is matched as a node
    rather than a text line and reformatting it cannot hide an unpinned write.
    """
    import ast
    import inspect

    from atv_bench import submit as submit_mod

    src = inspect.getsource(submit_mod.open_submission_pr)
    tree = ast.parse(textwrap.dedent(src))

    record_writes = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"write_text", "write_bytes"}
        and "submission.json" in ast.unparse(node.func.value)
    ]
    assert record_writes, "expected a submission.json write in open_submission_pr"
    text_writes = [
        ast.unparse(n) for n in record_writes if n.func.attr == "write_text"
    ]
    assert not text_writes, (
        "submission.json must be written with write_bytes -- write_text translates "
        f"json.dumps(indent=2) newlines to os.linesep on Windows: {text_writes}"
    )
