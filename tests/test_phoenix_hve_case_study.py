from __future__ import annotations

import json
from pathlib import Path

from atv_bench.comparison import sha256_bytes, write_checksums, write_exact_bytes
from scripts import compare_phoenix_hve
from scripts import summarize_phoenix_hve_v2 as summarizer


def _trial_document(
    *,
    run_id: str,
    runner_sha: str = "runner-current",
    phoenix_valid: bool = True,
    hve_valid: bool = True,
    phoenix_wins: int = 2,
    hve_wins: int = 0,
    draws: int = 0,
    shim_equal: bool = True,
) -> dict:
    games = phoenix_wins + hve_wins + draws if phoenix_valid and hve_valid else 0
    summary = {
        "games": games,
        "harness_a_wins": phoenix_wins if games else 0,
        "harness_b_wins": hve_wins if games else 0,
        "draws": draws if games else 0,
    }
    return {
        "schema_version": 2,
        "schema": "atv.phoenix-hve-local-trial/v2",
        "run_id": run_id,
        "trust_tier": "local-self-attested",
        "rankable": False,
        "official": False,
        "methodology": {
            "runner": {
                "script_sha256": runner_sha,
                "comparison_module_sha256": "comparison-current",
            },
            "model": "model-fixed",
            "prompt_sha256": "prompt-fixed",
            "independent_unit": "fresh_paired_harness_trial",
            "nested_games_are_not_independent_trials": True,
            "tool_compatibility_shim": True,
            "tool_compatibility_shim_equal": shim_equal,
        },
        "sources": {
            "atv_phoenix": {
                "commit": "1" * 40,
                "git_tree": "2" * 40,
                "tracked_tree_listing_sha256": "3" * 64,
            },
            "hve_core": {
                "commit": "4" * 40,
                "git_tree": "5" * 40,
                "tracked_tree_listing_sha256": "6" * 64,
            },
        },
        "builds": {
            "phoenix": {
                "status": "ok" if phoenix_valid else "error",
                "valid_artifact": phoenix_valid,
                "bot_sha256": "7" * 64,
            },
            "hve": {
                "status": "ok" if hve_valid else "error",
                "valid_artifact": hve_valid,
                "bot_sha256": "8" * 64,
            },
        },
        "trial_outcome": {
            "independent_unit": "fresh_paired_harness_trial",
            "artifact_validity": {
                "phoenix": phoenix_valid,
                "hve": hve_valid,
            },
            "quality_winner_claimed": False,
        },
        "series": {
            "phoenix_vs_hve": {
                "status": "ok" if games else "not_run_invalid_artifact",
                "summary": summary,
                "games": [
                    {"winner": "nested-descriptive-only", "index": index}
                    for index in range(games)
                ],
            }
        },
    }


def _write_trial(
    root: Path,
    name: str,
    *,
    runner_sha: str = "runner-current",
    phoenix_valid: bool = True,
    hve_valid: bool = True,
    phoenix_wins: int = 2,
    hve_wins: int = 0,
    draws: int = 0,
    shim_equal: bool = True,
) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    document = _trial_document(
        run_id=name,
        runner_sha=runner_sha,
        phoenix_valid=phoenix_valid,
        hve_valid=hve_valid,
        phoenix_wins=phoenix_wins,
        hve_wins=hve_wins,
        draws=draws,
        shim_equal=shim_equal,
    )
    write_exact_bytes(
        directory / "comparison.json",
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    write_exact_bytes(directory / "raw" / "phoenix.stdout.bin", b"phoenix\r\n\x00")
    write_exact_bytes(directory / "raw" / "hve.stdout.bin", b"hve\n\xff")
    if not phoenix_valid:
        write_exact_bytes(
            directory / "artifacts" / "phoenix" / "main.py",
            b"this is an invalid candidate\n",
        )
    if not hve_valid:
        write_exact_bytes(
            directory / "artifacts" / "hve" / "main.py",
            b"def broken(:\n",
        )
    write_checksums(directory)
    return directory


def test_games_are_not_inflated_into_independent_trials(tmp_path):
    _write_trial(
        tmp_path,
        "trial-1",
        phoenix_wins=60,
        hve_wins=30,
        draws=10,
    )
    output = summarizer.summarize_root(tmp_path)
    bootstrap = output["trial_level_quality"]["score_difference_bootstrap"]
    assert output["trial_counts"]["included"] == 1
    assert output["trial_counts"]["both_valid"] == 1
    assert bootstrap["observation_count"] == 1
    assert output["nested_game_totals_descriptive_only"]["games"] == 100
    assert output["decision"] == "inconclusive"


def test_checksum_tamper_excludes_trial_without_crashing_aggregate(tmp_path):
    _write_trial(tmp_path, "trial-reference")
    tampered = _write_trial(tmp_path, "trial-tampered")
    comparison = tampered / "comparison.json"
    comparison.write_bytes(comparison.read_bytes() + b"\n")

    output = summarizer.summarize_root(
        tmp_path, runner_sha="runner-current"
    )
    assert output["trial_counts"]["included"] == 1
    excluded = {
        row["directory"]: row["reasons"] for row in output["excluded_runs"]
    }
    assert "checksum verification failed" in excluded["trial-tampered"]


def test_stale_runner_is_excluded(tmp_path):
    _write_trial(tmp_path, "trial-current", runner_sha="runner-current")
    _write_trial(tmp_path, "trial-stale", runner_sha="runner-old")
    output = summarizer.summarize_root(
        tmp_path, runner_sha="runner-current"
    )
    assert [row["directory"] for row in output["included_trials"]] == [
        "trial-current"
    ]
    excluded = {
        row["directory"]: row["reasons"] for row in output["excluded_runs"]
    }
    assert "runner script differs" in excluded["trial-stale"]


def test_invalid_build_is_persisted_and_counted_as_reliability_only(tmp_path):
    trial = _write_trial(
        tmp_path,
        "trial-invalid-hve",
        phoenix_valid=True,
        hve_valid=False,
        phoenix_wins=999,
    )
    invalid_candidate = trial / "artifacts" / "hve" / "main.py"
    assert invalid_candidate.is_file()
    invalid_digest = sha256_bytes(invalid_candidate.read_bytes())

    output = summarizer.summarize_root(tmp_path)
    assert output["trial_counts"] == {
        "included": 1,
        "both_valid": 0,
        "minimum_for_task_contract_decision": 5,
    }
    assert output["artifact_validity"]["phoenix"]["valid"] == 1
    assert output["artifact_validity"]["hve"]["valid"] == 0
    assert output["nested_game_totals_descriptive_only"] == {}
    assert output["included_trials"][0]["quality_winner"] is None
    assert len(invalid_digest) == 64


def test_winner_gate_is_inconclusive_under_five_both_valid_trials(tmp_path):
    for index in range(4):
        _write_trial(tmp_path, f"trial-{index}", phoenix_wins=10, hve_wins=0)
    output = summarizer.summarize_root(tmp_path)
    assert output["trial_counts"]["both_valid"] == 4
    assert output["decision"] == "inconclusive"
    assert "requires at least 5" in output["decision_reason"]
    assert output["global_harness_winner"] is None
    assert output["rankable"] is False
    assert output["official"] is False


def test_winner_gate_can_only_make_a_task_contract_decision_at_five(tmp_path):
    for index in range(5):
        _write_trial(tmp_path, f"trial-{index}", phoenix_wins=10, hve_wins=0)
    output = summarizer.summarize_root(tmp_path)
    assert output["trial_counts"]["both_valid"] == 5
    assert output["decision"] == "phoenix_better_on_this_task_contract"
    assert output["global_harness_winner"] is None


def test_unequal_tool_shim_is_excluded(tmp_path):
    _write_trial(tmp_path, "trial-reference")
    _write_trial(tmp_path, "trial-unequal-shim", shim_equal=False)
    output = summarizer.summarize_root(tmp_path)
    excluded = {
        row["directory"]: row["reasons"] for row in output["excluded_runs"]
    }
    assert (
        "tool compatibility shim was not recorded equally"
        in excluded["trial-unequal-shim"]
    )


def test_tool_compatibility_shim_has_equal_exact_transformation(tmp_path):
    source = (
        b"---\nname: Test Agent\ntools: ['old-tool']\ndescription: x\n---\nBody\n"
    )
    first = tmp_path / "phoenix.agent.md"
    second = tmp_path / "hve.agent.md"
    first.write_bytes(source)
    second.write_bytes(source)
    first_record = compare_phoenix_hve._agent_tool_compatibility_shim(first)
    second_record = compare_phoenix_hve._agent_tool_compatibility_shim(second)
    assert first.read_bytes() == second.read_bytes()
    assert first_record["before_sha256"] == second_record["before_sha256"]
    assert first_record["after_sha256"] == second_record["after_sha256"]
    assert b"tools: ['*']" in first.read_bytes()
