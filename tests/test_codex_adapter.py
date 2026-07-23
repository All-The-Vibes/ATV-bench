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
    """parse_codex_model reads the REAL model from codex's --json JSONL, never the input flag."""
    jsonl = "\n".join([
        json.dumps({"type": "session.created", "model": "o4-mini"}),
        json.dumps({"type": "item.completed", "item": {"type": "assistant_message", "text": "done"}}),
    ])
    assert parse_codex_model(jsonl) == "o4-mini"
    # unparseable -> 'unknown', never an echoed "auto"
    assert parse_codex_model("not json\n{}") == "unknown"


def test_codex_run_builds_exec_command(monkeypatch):
    """run() drives `codex exec <goal> -m <model> --json` headless (sandbox bypassed because the
    ARENA already provides the isolation boundary), parses the model, and returns an EDITED/NO_EDIT
    result. We inject a fake subprocess so no real CLI is needed."""
    captured = {}

    def fake_run(cmd, req, *, env):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, returncode=0,
            stdout=json.dumps({"type": "session.created", "model": "o4-mini"}) + "\n",
            stderr="",
        )

    monkeypatch.setattr("atv_bench.adapters.contract._run_harness_subprocess", fake_run)
    # no git repo needed: base is None -> status derives from the (empty) diff
    monkeypatch.setattr("atv_bench.adapters.contract._head_sha", lambda p: None)
    monkeypatch.setattr("atv_bench.adapters.contract.git_diff", lambda p: "")

    res = CodexCliAdapter().run(AdapterRequest(repo_path=".", goal="improve the bot", model="o4-mini"))
    assert res.model == "o4-mini"
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
