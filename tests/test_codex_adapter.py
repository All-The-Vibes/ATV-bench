"""Unit 0 (quickstart): CodexCliAdapter — codex as a first-class runnable harness.

Codex shipped a fingerprint reader but no EXECUTION adapter, so it could be probed but never
run in a match. These tests pin the adapter contract (name/available/run-shape + model parse)
and its registration so `bare:codex` and the runner pick it up automatically.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from atv_bench.adapters.contract import (
    ADAPTERS,
    AdapterRequest,
    AdapterStatus,
    CodexCliAdapter,
    HarnessAdapter,
    parse_codex_model,
    resolve_adapter,
)


def test_codex_registered_and_named():
    """codex is a registered leaf adapter with the canonical name."""
    assert CodexCliAdapter.name == "codex"
    assert issubclass(CodexCliAdapter, HarnessAdapter)
    assert ADAPTERS.get("codex") is CodexCliAdapter


def test_codex_available_reflects_cli(monkeypatch):
    """available() is True iff the codex binary is on PATH."""
    monkeypatch.setattr("atv_bench.adapters.contract.shutil.which", lambda b: "/usr/bin/codex")
    assert CodexCliAdapter.available() is True
    monkeypatch.setattr("atv_bench.adapters.contract.shutil.which", lambda b: None)
    assert CodexCliAdapter.available() is False


def test_bare_codex_resolves():
    """`bare:codex` composes the bare control around the codex leaf (free via resolve_adapter)."""
    from atv_bench.lift import BareModelAdapter

    a = resolve_adapter("bare:codex")
    assert isinstance(a, BareModelAdapter)
    assert isinstance(a.inner, CodexCliAdapter)
    assert a.name == "bare:codex"


def test_harness_binary_for_codex():
    """The runner resolves the codex CLI binary (leaf and bare)."""
    from atv_bench.runner import harness_binary_for

    assert harness_binary_for("codex") == "codex"
    assert harness_binary_for("bare:codex") == "codex"


def test_parse_codex_model_from_jsonl():
    """parse_codex_model reads a model IF a future codex --json emits one; never an input echo."""
    jsonl = "\n".join([
        json.dumps({"type": "session.created", "model": "o4-mini"}),
        json.dumps({"type": "item.completed", "item": {"type": "assistant_message", "text": "done"}}),
    ])
    assert parse_codex_model(jsonl) == "o4-mini"
    # unparseable -> 'unknown', never an echoed "auto"
    assert parse_codex_model("not json\n{}") == "unknown"


# The REAL codex exec --json event shape (captured from codex-cli 0.130.0) — there is NO model
# field anywhere in the stream. This fixture pins the real shape so tests reason about reality.
REAL_CODEX_JSONL = "\n".join([
    json.dumps({"type": "thread.started", "thread_id": "abc"}),
    json.dumps({"type": "turn.started"}),
    json.dumps({"type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": "ok"}}),
    json.dumps({"type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 5}}),
])


def test_parse_codex_model_real_stream_has_no_model():
    """The real codex --json stream carries NO model -> parse returns 'unknown' (documents why
    _resolve_codex_model's fallbacks are load-bearing, not decorative)."""
    assert parse_codex_model(REAL_CODEX_JSONL) == "unknown"


def test_resolve_codex_model_priority(monkeypatch, tmp_path):
    """_resolve_codex_model: stream model > explicit -m > config.toml default > 'unknown'."""
    from atv_bench.adapters.contract import AdapterRequest, _resolve_codex_model

    # explicit model wins when the stream has none (the real case).
    req = AdapterRequest(repo_path=".", goal="x", model="gpt-5.5")
    assert _resolve_codex_model(REAL_CODEX_JSONL, req) == "gpt-5.5"

    # 'auto' + a configured default -> the config default.
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5-codex"\n')
    req_auto = AdapterRequest(repo_path=".", goal="x", model="auto",
                              env={"HOME": str(tmp_path)})
    assert _resolve_codex_model(REAL_CODEX_JSONL, req_auto) == "gpt-5-codex"

    # 'auto' + no config -> 'unknown' (fail closed, never fabricated).
    req_none = AdapterRequest(repo_path=".", goal="x", model="auto",
                              env={"HOME": str(tmp_path / "empty")})
    assert _resolve_codex_model(REAL_CODEX_JSONL, req_none) == "unknown"

    # a real stream model (future codex) beats everything.
    stream = json.dumps({"type": "session.created", "model": "o9-turbo"})
    assert _resolve_codex_model(stream, req) == "o9-turbo"


def test_codex_run_builds_exec_command(monkeypatch):
    """run() drives `codex exec <goal> -m <model> --json` headless (sandbox bypassed because the
    ARENA already provides the isolation boundary), resolves the model, and returns an
    EDITED/NO_EDIT result. Fake subprocess returns the REAL (model-less) event shape, so the
    reported model comes from the explicit -m via _resolve_codex_model."""
    captured = {}

    def fake_run(cmd, req, *, env):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout=REAL_CODEX_JSONL + "\n", stderr="",
        )

    monkeypatch.setattr("atv_bench.adapters.contract._run_harness_subprocess", fake_run)
    # no git repo needed: base is None -> status derives from the (empty) diff
    monkeypatch.setattr("atv_bench.adapters.contract._head_sha", lambda p: None)
    monkeypatch.setattr("atv_bench.adapters.contract.git_diff", lambda p: "")

    res = CodexCliAdapter().run(AdapterRequest(repo_path=".", goal="improve the bot", model="o4-mini"))
    assert res.model == "o4-mini"  # resolved from the explicit -m (stream has no model)
    assert res.status in (AdapterStatus.EDITED, AdapterStatus.NO_EDIT)
    cmd = captured["cmd"]
    assert cmd[0] == "codex" and cmd[1] == "exec"
    assert "improve the bot" in cmd
    assert "-m" in cmd and "o4-mini" in cmd
    assert "--json" in cmd
    # the security-critical non-interactive flag must be present (pins it against silent regress)
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd


def test_codex_run_auto_model_omits_flag(monkeypatch):
    """model='auto' omits the -m flag (let codex pick its default)."""
    captured = {}

    def fake_run(cmd, req, *, env):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout="{}\n", stderr="")

    monkeypatch.setattr("atv_bench.adapters.contract._run_harness_subprocess", fake_run)
    monkeypatch.setattr("atv_bench.adapters.contract._head_sha", lambda p: None)
    monkeypatch.setattr("atv_bench.adapters.contract.git_diff", lambda p: "")
    CodexCliAdapter().run(AdapterRequest(repo_path=".", goal="x", model="auto"))
    assert "-m" not in captured["cmd"]


def test_parse_codex_model_skips_auto_placeholder():
    """A leading 'auto'/'default' echo must NOT be reported as the model — only the RESOLVED
    id counts (fail-closed on the placeholder, keep scanning)."""
    jsonl = "\n".join([
        json.dumps({"type": "config", "model": "auto"}),          # placeholder echo first
        json.dumps({"type": "session.created", "model": "gpt-5-codex"}),  # real resolved model
    ])
    assert parse_codex_model(jsonl) == "gpt-5-codex"
    # only placeholders present -> unknown, never 'auto'
    assert parse_codex_model(json.dumps({"model": "auto"})) == "unknown"


def test_codex_config_model_resolves_isolated_home_locations(tmp_path):
    """_codex_config_model finds config.toml at $CODEX_HOME, $HOME/.codex, AND $HOME/config.toml
    (the location isolation.isolated_home seeds) — so the default resolves in the live path."""
    from atv_bench.adapters.contract import _codex_config_model

    # $HOME/.codex/config.toml (the real user location)
    a = tmp_path / "a"; (a / ".codex").mkdir(parents=True)
    (a / ".codex" / "config.toml").write_text('model = "gpt-5.5"\n')
    assert _codex_config_model({"HOME": str(a)}) == "gpt-5.5"

    # $HOME/config.toml (the isolated_home seed location)
    b = tmp_path / "b"; b.mkdir()
    (b / "config.toml").write_text('model = "o4-mini"\n')
    assert _codex_config_model({"HOME": str(b)}) == "o4-mini"

    # $CODEX_HOME/config.toml (explicit override)
    c = tmp_path / "c"; c.mkdir()
    (c / "config.toml").write_text('model = "gpt-5-codex"\n')
    assert _codex_config_model({"CODEX_HOME": str(c), "HOME": str(a)}) == "gpt-5-codex"

    # none present -> None
    assert _codex_config_model({"HOME": str(tmp_path / "empty")}) is None
