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
