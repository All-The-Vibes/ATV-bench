"""TDD for runner.py — the `atv-bench run` orchestration (Lane C).

Focuses on the host-side, no-Docker logic: preflight fail-closed (no-fake guard),
unverified-tag publish-block, envelope shape, and exit-code mapping. The live match
itself (Docker + real CLIs) is gated behind @integration and proven in the E2E step.
"""
from __future__ import annotations

import pytest

from atv_bench.run_envelope import RunError
from atv_bench.runner import (
    RunConfig,
    preflight_or_raise,
    build_match_record,
)


def _cfg(**kw):
    base = dict(game="lightcycles", a="copilot-cli", b="claude-code",
                model="claude-opus-4.8", rounds=3)
    base.update(kw)
    return RunConfig(**base)


def test_preflight_missing_cli_raises_missing_cli(monkeypatch):
    # No-fake guard (CRITICAL): a missing harness CLI → RunError(missing_cli), NOT a bot.
    import atv_bench.preflight as pf

    def fake_which(binary):
        return None if binary in ("copilot", "claude") else "/usr/bin/" + binary
    monkeypatch.setattr(pf.shutil, "which", fake_which)
    with pytest.raises(RunError) as exc:
        preflight_or_raise(_cfg(), require_docker=False, require_codeclash=False)
    assert exc.value.code == "missing_cli"
    assert exc.value.exit_code == 3


def test_preflight_reports_all_missing_at_once(monkeypatch):
    # DX-4: aggregate — both CLIs missing surfaced together in the message.
    import atv_bench.preflight as pf
    monkeypatch.setattr(pf.shutil, "which", lambda b: None)
    with pytest.raises(RunError) as exc:
        preflight_or_raise(_cfg(), require_docker=False, require_codeclash=False)
    assert "copilot" in exc.value.message and "claude" in exc.value.message


def test_build_match_record_blocks_publish_when_model_unverified():
    # Phase-1 host-subprocess result is verified=False → never publishes a number (DX-8).
    rec = build_match_record(
        _cfg(),
        outcome={"winner": "copilot-cli", "raw": "a_wins", "turns": 42},
        player_models={"copilot-cli": ("claude-opus-4.8", "parsed"),
                       "claude-code": ("claude-opus-4.8", "parsed")},
        player_fingerprints={"copilot-cli": "a" * 64, "claude-code": "b" * 64},
        replay_path="_replay/index.html",
    )
    d = rec.to_dict()
    assert d["verified"] is False  # Phase 1: gateway not yet used → unverified
    assert d["schema_version"] == 2
    assert len(d["players"]) == 2


def test_build_match_record_marks_unknown_model():
    # An unparseable copilot model → model 'unknown', still recorded, never faked.
    rec = build_match_record(
        _cfg(),
        outcome={"winner": "draw", "raw": "draw", "turns": 10},
        player_models={"copilot-cli": ("unknown", "parsed"),
                       "claude-code": ("claude-opus-4.8", "parsed")},
        player_fingerprints={"copilot-cli": "a" * 64, "claude-code": "b" * 64},
        replay_path="",
    )
    models = {p["model"] for p in rec.to_dict()["players"]}
    assert "unknown" in models
    assert rec.is_verified() is False


def test_run_config_rejects_bad_rounds():
    with pytest.raises(RunError) as exc:
        _cfg(rounds=0).validate()
    assert exc.value.code == "usage"


def test_run_config_rejects_unknown_game():
    with pytest.raises(RunError) as exc:
        _cfg(game="pong").validate()
    assert exc.value.code == "usage"
    assert "lightcycles" in exc.value.message  # did-you-mean / valid set


def test_run_config_rejects_unknown_harness():
    with pytest.raises(RunError) as exc:
        _cfg(a="not-a-harness").validate()
    assert exc.value.code == "usage"
