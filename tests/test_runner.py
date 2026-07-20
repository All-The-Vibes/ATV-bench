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


def test_match_row_populates_tools_nested_skills():
    # ENG-F: the fingerprint manifest already captures tools + nested_skills; the record
    # must PERSIST them, not drop them to []. `tools` in the manifest is a list of
    # {name, source, enabled} dicts; the row records the tool names.
    manifests = {
        "copilot-cli": {
            "tools": [
                {"name": "read", "source": "permission", "enabled": True},
                {"name": "write", "source": "permission", "enabled": True},
            ],
            "nested_skills": ["gstack/plan", "office-hours"],
        },
        "claude-code": {
            "tools": [{"name": "bash", "source": "builtin", "enabled": True}],
            "nested_skills": ["compound-engineering/ce-work"],
        },
    }
    rec = build_match_record(
        _cfg(),
        outcome={"winner": "copilot-cli"},
        player_models={"copilot-cli": ("claude-opus-4.8", "parsed"),
                       "claude-code": ("claude-opus-4.8", "parsed")},
        player_fingerprints={"copilot-cli": "a" * 64, "claude-code": "b" * 64},
        player_manifests=manifests,
        replay_path="_replay/index.html",
    )
    by_h = {p.harness: p for p in rec.players}
    assert by_h["copilot-cli"].tools == ["read", "write"]
    assert by_h["copilot-cli"].nested_skills == ["gstack/plan", "office-hours"]
    assert by_h["claude-code"].tools == ["bash"]
    assert by_h["claude-code"].nested_skills == ["compound-engineering/ce-work"]
    # explicit non-regression: NOT the hardcoded empty lists
    assert by_h["copilot-cli"].tools != []


def test_budget_recorded_per_match():
    # G10: each player row carries a budget vector (tokens, tool_calls, wall_time_s)
    # sourced from the adapter result / run timing, so outspending is disclosed.
    budgets = {
        "copilot-cli": {"tokens": 50000, "tool_calls": 12, "wall_time_s": 120.0},
        "claude-code": {"tokens": None, "tool_calls": None, "wall_time_s": 88.5},
    }
    rec = build_match_record(
        _cfg(),
        outcome={"winner": "copilot-cli"},
        player_models={"copilot-cli": ("claude-opus-4.8", "parsed"),
                       "claude-code": ("claude-opus-4.8", "parsed")},
        player_fingerprints={"copilot-cli": "a" * 64, "claude-code": "b" * 64},
        player_budgets=budgets,
        replay_path="_replay/index.html",
    )
    by_h = {p.harness: p for p in rec.players}
    assert by_h["copilot-cli"].budget.tokens == 50000
    assert by_h["copilot-cli"].budget.tool_calls == 12
    assert by_h["copilot-cli"].budget.wall_time_s == 120.0
    # unreported tokens/tool-calls recorded as None (no fabrication), wall-time present
    assert by_h["claude-code"].budget.tokens is None
    assert by_h["claude-code"].budget.wall_time_s == 88.5
    # serialized into the row
    d = rec.to_dict()
    row = next(p for p in d["players"] if p["harness"] == "copilot-cli")
    assert row["budget"] == {"tokens": 50000, "tool_calls": 12, "wall_time_s": 120.0}


def test_budget_sourced_from_adapter_usage():
    # G10 REAL PATH: the per-player budget must be sourced from the adapter's measured
    # Usage (tokens + wall clock) that flows through HarnessPlayerCore.edit_turn into the
    # build-once artifact cache — NOT from metadata.budgets, a key no producer ever writes.
    # This fails when summarize_budgets reads the empty metadata: budget would be all-None.
    from atv_bench.adapters.contract import (
        AdapterRequest, AdapterResult, AdapterStatus, HarnessAdapter, Usage,
    )
    from atv_bench.config import _distinct_names
    from atv_bench.players import HarnessPlayerCore, clear_artifact_cache
    from atv_bench.runner import summarize_budgets

    class _FakeAdapter(HarnessAdapter):
        name = "fake"

        def run(self, req: AdapterRequest) -> AdapterResult:
            # A real adapter measures Usage(tokens, seconds) around its CLI subprocess.
            return AdapterResult(
                status=AdapterStatus.NO_EDIT, diff="", log="",
                usage=Usage(tokens=1234, seconds=5.0, turns=1),
            )

    class _MemContainer:
        def __init__(self):
            self.tree = {"main.py": "print('hi')\n"}

        def read_tree(self):
            return dict(self.tree)

        def write_tree(self, files):
            self.tree = dict(files)

    cfg = _cfg()
    a_name, _b_name = _distinct_names(cfg.a, cfg.b)
    clear_artifact_cache()
    try:
        core = HarnessPlayerCore(
            adapter=_FakeAdapter(), container=_MemContainer(),
            goal="improve", player_id=a_name, game=cfg.game,
        )
        result = core.edit_turn()
        assert result.usage.tokens == 1234

        budgets = summarize_budgets({}, cfg)
        a_budget = budgets[cfg.a]
        assert a_budget.tokens == 1234
        assert a_budget.wall_time_s == pytest.approx(5.0)
        assert a_budget.tokens is not None  # not sourced from the empty metadata.budgets
    finally:
        clear_artifact_cache()
