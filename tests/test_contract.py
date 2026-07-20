"""Unit tests for the adapter contract schema (design doc: Adapter Contract Schema).

Fast, no network. Validates the typed contract shape and serialization that the
harness-runner and dashboard depend on.
"""
from __future__ import annotations

import json

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    Usage,
)


def test_budget_defaults_and_serialization():
    b = Budget()
    assert b.max_turns == 10
    assert b.max_seconds == 300
    assert b.max_tokens == 200_000
    assert b.to_dict() == {
        "max_turns": 10,
        "max_seconds": 300,
        "max_tokens": 200_000,
    }


def test_request_schema_matches_design():
    req = AdapterRequest(repo_path="/tmp/repo", goal="win", model="gpt-5")
    d = req.to_dict()
    assert set(d) == {"repo_path", "goal", "model", "budget", "bot_file"}
    assert d["budget"]["max_seconds"] == 300


def test_result_schema_matches_design():
    res = AdapterResult(
        status=AdapterStatus.OK,
        diff="--- a\n+++ b\n",
        log="ok",
        usage=Usage(tokens=42, seconds=1.5, turns=1),
        model="claude-opus-4-8",
    )
    d = res.to_dict()
    assert set(d) == {"status", "diff", "log", "usage", "model"}
    assert d["status"] == "ok"
    assert d["usage"] == {"tokens": 42, "seconds": 1.5, "turns": 1}
    assert d["model"] == "claude-opus-4-8"
    # round-trips through JSON (dashboard reads this)
    assert json.loads(res.to_json())["status"] == "ok"


def test_all_statuses_present():
    # design requires these outcome states for scoring semantics
    values = {s.value for s in AdapterStatus}
    assert {"ok", "no_edit", "error", "timeout", "budget_exhausted"} <= values
    # plus the fallback-ladder signal
    assert "policy_denied" in values


# --- Copilot model-tag integrity (Eng Decision #5, gap #15 resolved: copilot JSONL
#     DOES expose the real model via assistant.message events) ---

from atv_bench.adapters.contract import parse_copilot_model


def test_parse_copilot_model_from_assistant_message():
    # Real shape from `copilot --output-format json` (JSONL).
    jsonl = "\n".join([
        '{"type":"session.tools_updated","data":{}}',
        '{"type":"assistant.message","data":{"model":"claude-opus-4.8","content":"done"}}',
        '{"type":"result","exitCode":0,"usage":{"premiumRequests":1}}',
    ])
    assert parse_copilot_model(jsonl) == "claude-opus-4.8"


def test_parse_copilot_model_falls_back_to_modelId():
    jsonl = '{"type":"session.usage_checkpoint","data":{"modelCacheState":[{"modelId":"gpt-5.4"}]}}'
    assert parse_copilot_model(jsonl) == "gpt-5.4"


def test_parse_copilot_model_unparseable_returns_unknown():
    # No machine-readable model anywhere -> unknown, NEVER the input string.
    assert parse_copilot_model("not json at all") == "unknown"
    assert parse_copilot_model("") == "unknown"


def test_copilot_model_auto_never_echoes_input():
    """CRITICAL (★★★): `--model auto` must NOT yield model='auto'.

    We can't run the real CLI in a unit test, so we assert the parser — the only
    source of the tag — cannot emit 'auto' from an input echo; it emits the parsed
    model or 'unknown'.
    """
    # Even if the JSONL somehow lacked a model, we get 'unknown', not 'auto'.
    assert parse_copilot_model('{"type":"result"}') == "unknown"


# --- ENG-A: status derivation must use the union, not plain `git diff` -----------

import subprocess
from pathlib import Path


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _seed(repo: Path) -> str:
    from atv_bench.adapters.snapshot import seed_base

    repo.mkdir(parents=True, exist_ok=True)
    (repo / "main.py").write_text("def get_move(o):\n    return 'N'\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "seed")
    return seed_base(repo)


def test_derive_status_committed_edit_is_not_no_edit(tmp_path):
    """A committed edit leaves a CLEAN working tree; plain `git diff` is empty. The
    union-based derivation must still report EDITED/OK, never NO_EDIT (ENG-A)."""
    from atv_bench.adapters.contract import derive_status

    repo = tmp_path / "repo"
    base = _seed(repo)
    (repo / "main.py").write_text("def get_move(o):\n    return 'S'\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=a@b.c", "-c", "user.name=t", "commit", "-qm", "edit")

    status = derive_status(str(repo), base)
    assert status != AdapterStatus.NO_EDIT
    assert status in {AdapterStatus.EDITED, AdapterStatus.OK}


def test_distinct_nonwin_statuses_exist():
    """The outcome taxonomy must distinguish crash / malformed from edited."""
    values = {s.value for s in AdapterStatus}
    assert {"edited", "crash", "malformed"} <= values
