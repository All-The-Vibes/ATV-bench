"""TDD for the run-CLI envelope + exit-code contract (DX-1/DX-2/DX-3)."""
from __future__ import annotations

import json

from atv_bench.run_envelope import (
    EXIT_CODES,
    RunError,
    error_envelope,
    ok_envelope,
)


def test_exit_codes_are_the_documented_stable_map():
    assert EXIT_CODES == {
        "ok": 0,
        "usage": 2,
        "missing_cli": 3,
        "unauthenticated": 4,
        "docker_unavailable": 5,
        "policy_denied": 6,
        "timeout": 7,
        "model_unparseable": 8,
        "codeclash_dep": 9,
    }


def test_ok_envelope_shape():
    env = ok_envelope(
        {"game": "lightcycles", "players": [], "elo": None, "rounds": 3, "replay_path": "x"}
    )
    assert env["success"] is True
    assert env["error"] is None
    assert env["data"]["game"] == "lightcycles"
    # round-trips as JSON (agent-user contract)
    assert json.loads(json.dumps(env))["success"] is True


def test_error_envelope_carries_code_message_fix():
    env = error_envelope(
        RunError("missing_cli", "copilot CLI not found on PATH", fix="install with: npm i -g @github/copilot")
    )
    assert env["success"] is False
    assert env["data"] is None
    assert env["error"]["code"] == "missing_cli"
    assert env["error"]["exit_code"] == 3
    assert "install" in env["error"]["fix"]


def test_run_error_maps_to_exit_code():
    assert RunError("docker_unavailable", "x").exit_code == 5
    assert RunError("codeclash_dep", "x").exit_code == 9


def test_unknown_error_code_is_usage():
    # A programming mistake shouldn't crash; default to usage (2).
    assert RunError("nonsense", "x").exit_code == 2
