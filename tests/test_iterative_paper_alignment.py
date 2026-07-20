"""Deterministic paper-alignment fixtures for edit/compete/feedback tournaments."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    EvidenceSource,
    HarnessAdapter,
    Usage,
)
from atv_bench.cli import app
from atv_bench.config import build_pvp_config
from atv_bench.match_record import MatchRecord, PlayerRecord
from atv_bench.players import (
    ADAPTATION_FROZEN,
    ADAPTATION_ITERATIVE,
    FrozenArtifactIdentity,
    HarnessPlayerCore,
)
from atv_bench.runner import (
    RunConfig,
    build_bradley_terry_summary,
    summarize_tournament,
)


class PersistentContainer:
    def __init__(self, tree=None, feedback=None):
        self.tree = dict(tree or {"main.py": "ROUND = 0\n"})
        self.feedback = dict(feedback or {})
        self.writes: list[dict[str, str]] = []

    def read_tree(self):
        return dict(self.tree)

    def write_tree(self, files):
        self.tree = dict(files)
        self.writes.append(dict(files))

    def read_feedback(self, previous_round):
        return dict(self.feedback.get(previous_round, {}))


class IncrementingAdapter(HarnessAdapter):
    name = "incrementing"

    def __init__(self):
        self.calls = 0
        self.goals: list[str] = []
        self.starting_rounds: list[int] = []

    def run(self, request: AdapterRequest) -> AdapterResult:
        self.calls += 1
        self.goals.append(request.goal)
        path = Path(request.repo_path) / request.bot_file
        current = int(path.read_bytes().decode("utf-8").split("=")[1].strip())
        self.starting_rounds.append(current)
        path.write_bytes(f"ROUND = {current + 1}\n".encode("utf-8"))
        return AdapterResult(
            status=AdapterStatus.OK,
            diff="",
            log=f"edit-{self.calls}",
            usage=Usage(tokens=10 * self.calls, seconds=1.0, turns=1),
            model=request.model,
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=False,
        )


class SequenceAdapter(HarnessAdapter):
    name = "sequence"

    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def run(self, request: AdapterRequest) -> AdapterResult:
        status = self.statuses[self.calls]
        self.calls += 1
        if status is AdapterStatus.OK:
            (Path(request.repo_path) / request.bot_file).write_bytes(b"ROUND = 99\n")
        return AdapterResult(
            status=status,
            diff="",
            log=status.value,
            usage=Usage(),
            model=request.model,
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=False,
        )


def _core(container, adapter, *, adaptation=ADAPTATION_ITERATIVE, **overrides):
    values = {
        "adapter": adapter,
        "container": container,
        "bot_file": "main.py",
        "goal": "Improve the bot.",
        "model": "requested-model",
        "budget": Budget(max_turns=4, max_seconds=20, max_tokens=1000),
        "player_id": "player-a",
        "game": "LightCycles",
        "prompt_version": "edit@1",
        "adaptation": adaptation,
        "adapter_version": "adapter@1",
        "harness_manifest_digest": "1" * 64,
        "harness_config_digest": "2" * 64,
        "model_policy_digest": "3" * 64,
        "task_digest": "4" * 64,
        "prompt_digest": "5" * 64,
        "protocol_version": "atv.harness/v1",
        "manifest_capabilities": {"resumable": False},
    }
    values.update(overrides)
    return HarnessPlayerCore(**values)


def test_iterative_invokes_every_round_and_persists_prior_edits():
    container = PersistentContainer()
    adapter = IncrementingAdapter()
    core = _core(container, adapter)

    for round_number in range(1, 4):
        core.edit_turn(round_number=round_number)

    assert adapter.calls == 3
    assert adapter.starting_rounds == [0, 1, 2]
    assert container.tree["main.py"] == "ROUND = 3\n"
    assert [row.round for row in core.round_evidence] == [1, 2, 3]
    assert all(row.fresh_harness_process for row in core.round_evidence)
    assert all(row.fresh_model_context for row in core.round_evidence)
    assert all(row.observation_unit == "nested-round" for row in core.round_evidence)
    assert len({row.output_tree_sha256 for row in core.round_evidence}) == 3


def test_round_two_receives_round_one_trusted_result_without_private_code():
    container = PersistentContainer(
        feedback={
            0: {"results.json": '{"winner":"Tie"}'},
            1: {
                "results.json": '{"winner":"player-a"}',
                "arena.log": "player-a survived",
                "opponent.py": "PRIVATE = True",
                "opponent_codebases/secret.txt": "do not expose",
            },
        }
    )
    adapter = IncrementingAdapter()
    core = _core(container, adapter)

    core.edit_turn(round_number=1)
    core.edit_turn(round_number=2)

    assert '"winner":"player-a"' in adapter.goals[1]
    assert "player-a survived" in adapter.goals[1]
    assert "PRIVATE = True" not in adapter.goals[1]
    assert "do not expose" not in adapter.goals[1]
    assert core.round_evidence[1].feedback_round == 1
    assert set(core.round_evidence[1].feedback_files) == {
        "arena.log",
        "results.json",
    }


def test_frozen_artifact_invokes_once_and_is_explicitly_nonadaptive():
    container = PersistentContainer()
    adapter = IncrementingAdapter()
    core = _core(container, adapter, adaptation=ADAPTATION_FROZEN)

    for round_number in range(1, 4):
        core.edit_turn(round_number=round_number)

    assert adapter.calls == 1
    assert container.tree["main.py"] == "ROUND = 1\n"
    assert core.round_evidence[0].fresh_harness_process is True
    assert core.round_evidence[1].fresh_harness_process is False
    assert core.round_evidence[1].replayed_from_round == 1
    assert all(row.adaptation == "frozen-artifact" for row in core.round_evidence)


@pytest.mark.parametrize("adaptation", [ADAPTATION_ITERATIVE, ADAPTATION_FROZEN])
def test_timeout_is_round_local_and_never_silently_replayed(adaptation):
    container = PersistentContainer()
    adapter = SequenceAdapter([AdapterStatus.TIMEOUT, AdapterStatus.OK])
    core = _core(container, adapter, adaptation=adaptation)

    first = core.edit_turn(round_number=1)
    second = core.edit_turn(round_number=2)

    assert first.status is AdapterStatus.TIMEOUT
    assert second.status is AdapterStatus.OK
    assert adapter.calls == 2
    assert container.tree["main.py"] == "ROUND = 99\n"
    assert core.round_evidence[0].status == "timeout"
    assert core.round_evidence[1].fresh_harness_process is True


def test_frozen_identity_changes_for_every_required_identity_facet():
    base = FrozenArtifactIdentity(
        harness_manifest_digest="1" * 64,
        harness_config_digest="2" * 64,
        adapter_version="adapter@1",
        model_policy_digest="3" * 64,
        budget={"max_turns": 1, "max_seconds": 2, "max_tokens": 3},
        task_digest="4" * 64,
        base_tree_digest="5" * 64,
        prompt_digest="6" * 64,
        player_id="player-a",
        game="LightCycles",
        protocol_version="atv.harness/v1",
    )
    changes = {
        "harness_manifest_digest": "a" * 64,
        "harness_config_digest": "b" * 64,
        "adapter_version": "adapter@2",
        "model_policy_digest": "c" * 64,
        "budget": {"max_turns": 9, "max_seconds": 2, "max_tokens": 3},
        "task_digest": "d" * 64,
        "base_tree_digest": "e" * 64,
        "prompt_digest": "f" * 64,
        "player_id": "player-b",
        "game": "BattleSnake",
        "protocol_version": "atv.harness/v2",
    }
    for field, value in changes.items():
        assert dataclasses.replace(base, **{field: value}).digest != base.digest


def test_requested_model_remains_unverified_and_memory_is_manifest_gated():
    class FactoryAdapter(IncrementingAdapter):
        instances = 0

        def __init__(self):
            super().__init__()
            type(self).instances += 1

    container = PersistentContainer()
    original = FactoryAdapter()
    core = _core(
        container,
        original,
        adapter_factory=FactoryAdapter,
        manifest_capabilities={"resumable": False},
    )
    core.edit_turn(round_number=1)
    core.edit_turn(round_number=2)
    assert FactoryAdapter.instances == 3  # original plus one fresh adapter per round
    assert all(row.requested_model == "requested-model" for row in core.round_evidence)
    assert all(row.model_verified is False for row in core.round_evidence)
    assert all(row.harness_memory_enabled is False for row in core.round_evidence)

    memory_adapter = FactoryAdapter()
    memory_core = _core(
        PersistentContainer(),
        memory_adapter,
        adapter_factory=FactoryAdapter,
        manifest_capabilities={"resumable": True},
    )
    memory_core.edit_turn(round_number=1)
    memory_core.edit_turn(round_number=2)
    assert memory_adapter.calls == 2
    assert all(row.harness_memory_enabled for row in memory_core.round_evidence)


def test_config_defaults_to_iterative_and_labels_rounds_as_nested():
    iterative = build_pvp_config(
        game="lightcycles",
        a="copilot-cli",
        b="claude-code",
        model="requested-model",
        rounds=3,
    )
    assert iterative["_meta"]["adaptation"] == "iterative"
    assert iterative["_meta"]["trial_unit"] == "tournament"
    assert iterative["_meta"]["rounds_nested"] is True
    assert iterative["tournament"]["round_observation_unit"] == "nested-round"

    frozen = build_pvp_config(
        game="lightcycles",
        a="copilot-cli",
        b="claude-code",
        model="requested-model",
        rounds=3,
        adaptation="frozen-artifact",
    )
    assert frozen["_meta"]["frozen_artifact_is_adaptation"] is False
    assert all(
        player["config"]["adaptation"] == "frozen-artifact"
        for player in frozen["players"]
    )
    with pytest.raises(ValueError, match="unknown adaptation"):
        build_pvp_config(
            game="lightcycles",
            a="copilot-cli",
            b="claude-code",
            model="m",
            rounds=1,
            adaptation="fake",
        )


def test_tournament_summary_is_tie_aware_bradley_terry_input_not_league_update():
    summary = build_bradley_terry_summary(
        [
            {"player_a": "a", "player_b": "b", "winner": "a"},
            {"player_a": "a", "player_b": "b", "winner": "tie"},
            {"player_a": "a", "player_b": "b", "winner": "b"},
        ]
    )
    assert summary["win_matrix"]["a::b"] == [1.5, 1.5]
    assert summary["independent_unit"] == "tournament"
    assert summary["tie_handling"] == "half-win-each"
    assert summary["sequential_league_updates"] is False
    assert "elo" not in json.dumps(summary).lower()


def test_summarize_tournament_emits_nested_round_evidence_and_unranked_metadata():
    cfg = RunConfig(
        game="lightcycles",
        a="copilot-cli",
        b="claude-code",
        model="requested-model",
        rounds=2,
        adaptation="iterative",
    )
    raw = {
        "pvp_config": {
            "players": [
                {"name": "copilot-cli", "agent": "copilot-cli"},
                {"name": "claude-code", "agent": "claude-code"},
            ]
        },
        "metadata": {
            "round_stats": {
                "0": {"winner": "Tie", "scores": {}},
                "1": {"winner": "copilot-cli", "scores": {}},
                "2": {"winner": "claude-code", "scores": {}},
            },
            "agents": [
                {
                    "name": "copilot-cli",
                    "atv": {
                        "rounds": {
                            "1": {
                                "observation_unit": "nested-round",
                                "status": "ok",
                            }
                        }
                    },
                }
            ],
        },
    }
    outcome, models = summarize_tournament(raw, cfg)
    assert outcome["winner"] == "tie"
    assert outcome["trial_unit"] == "tournament"
    assert outcome["rounds_nested"] is True
    assert outcome["ranking_published"] is False
    assert outcome["round_evidence"][0]["observation_unit"] == "nested-round"
    assert models["copilot-cli"] == ("requested-model", "recording")


def test_match_record_never_promotes_nested_rounds_to_trials():
    player = PlayerRecord(
        harness="copilot-cli",
        model="requested-model",
        model_source="parsed",
        verified=False,
        tools=[],
        nested_skills=[],
        fingerprint_sha256="a" * 64,
        adapter_version="1.0.0",
    )
    record = MatchRecord(
        game="lightcycles",
        game_version="lightcycles@1",
        prompt_version="edit@1",
        codeclash_version="git@pin",
        rounds=2,
        outcome={},
        replay_path="",
        players=[player],
        adaptation="iterative",
        round_evidence=[
            {"round": 1, "observation_unit": "nested-round"},
            {"round": 2, "observation_unit": "nested-round"},
        ],
    ).to_dict()
    assert record["trial_unit"] == "tournament"
    assert record["rounds_nested"] is True
    assert record["round_observation_unit"] == "nested-round"
    assert record["ranked"] is False
    assert all("trial_id" not in row for row in record["round_evidence"])


def test_cli_run_help_and_invalid_mode_are_explicit():
    runner = CliRunner()
    help_result = runner.invoke(app, ["run", "--help"])
    assert help_result.exit_code == 0
    assert "--adaptation" in help_result.stdout
    assert "iterative" in help_result.stdout
    assert "frozen-artifact" in help_result.stdout
    assert "Nested rounds" in help_result.stdout

    invalid = runner.invoke(
        app,
        [
            "run",
            "--a",
            "copilot-cli",
            "--b",
            "claude-code",
            "--model",
            "requested-model",
            "--adaptation",
            "fake",
            "--json",
        ],
    )
    assert invalid.exit_code == 2
    envelope = json.loads(invalid.stdout)
    assert envelope["error"]["code"] == "usage"
