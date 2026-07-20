from __future__ import annotations

import json
from pathlib import Path

import pytest

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
                "arena_engine_sha256": "engine-current",
                "arena_referee_sha256": "referee-current",
            },
            "model": "model-fixed",
            "model_selection_source": "explicit_cli",
            "copilot_cli": "GitHub Copilot CLI fixed",
            "held_out_seeds": 5,
            "per_turn_timeout_seconds": 3.0,
            "harness_timeout_seconds": 900,
            "max_ai_credits": 30,
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
                "reported_model": "model-fixed",
                "model_matches_request": True,
            },
            "hve": {
                "status": "ok" if hve_valid else "error",
                "valid_artifact": hve_valid,
                "bot_sha256": "8" * 64,
                "reported_model": "model-fixed",
                "model_matches_request": True,
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
    receipt = (
        json.dumps(
            {
                "type": "assistant.message",
                "data": {"model": "model-fixed", "content": "done"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "exitCode": 0,
                "sessionId": f"session-{name}",
                "usage": {},
            }
        )
        + "\n"
    ).encode("utf-8")
    write_exact_bytes(directory / "raw" / "phoenix.stdout.bin", receipt)
    write_exact_bytes(directory / "raw" / "hve.stdout.bin", receipt)
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


def test_forfeits_are_separate_from_completed_gameplay(tmp_path):
    trial = _write_trial(
        tmp_path,
        "trial-mixed",
        phoenix_wins=2,
        hve_wins=0,
        draws=0,
    )
    comparison = trial / "comparison.json"
    document = json.loads(comparison.read_text(encoding="utf-8"))
    document["series"]["phoenix_vs_hve"]["games"] = [
        {
            "seed": 1,
            "swapped": False,
            "winner": "harness_a",
            "outcome": "a_wins",
            "forfeit_reason": None,
        },
        {
            "seed": 2,
            "swapped": False,
            "winner": "harness_a",
            "outcome": "forfeit_b",
            "forfeit_reason": "TIMEOUT",
        },
    ]
    write_exact_bytes(
        comparison,
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    write_checksums(trial)

    output = summarizer.summarize_root(tmp_path)

    assert output["nested_game_totals_descriptive_only"] == {
        "games": 2,
        "phoenix_wins": 2,
        "hve_wins": 0,
        "draws": 0,
    }
    assert output["completed_game_totals_descriptive_only"] == {
        "games": 1,
        "phoenix_wins": 1,
        "hve_wins": 0,
        "draws": 0,
    }
    assert output["forfeit_totals_descriptive_only"] == {
        "total": 1,
        "phoenix_forfeits": 0,
        "hve_forfeits": 1,
        "unknown_forfeits": 0,
        "reasons": {"hve": {"TIMEOUT": 1}},
    }
    assert output["decision_basis"] == "end_to_end_task_contract_including_forfeits"
    assert output["trial_level_task_contract"]["wins"] == {"phoenix": 1}


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


def test_reported_model_mismatch_is_excluded(tmp_path):
    _write_trial(tmp_path, "trial-reference")
    mismatched = _write_trial(tmp_path, "trial-model-mismatch")
    receipt = (
        json.dumps(
            {
                "type": "assistant.message",
                "data": {"model": "different-model", "content": "done"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "result",
                "exitCode": 0,
                "sessionId": "session",
                "usage": {},
            }
        )
        + "\n"
    ).encode("utf-8")
    write_exact_bytes(
        mismatched / "raw" / "hve.stdout.bin",
        receipt,
    )
    write_checksums(mismatched)

    output = summarizer.summarize_root(tmp_path)
    excluded = {
        row["directory"]: row["reasons"] for row in output["excluded_runs"]
    }
    assert (
        "reported model does not exactly match requested model"
        in excluded["trial-model-mismatch"]
    )


def test_malformed_or_truncated_raw_model_receipt_is_excluded(tmp_path):
    _write_trial(tmp_path, "trial-reference")
    malformed = _write_trial(tmp_path, "trial-malformed-receipt")
    receipt_path = malformed / "raw" / "phoenix.stdout.bin"
    write_exact_bytes(
        receipt_path,
        b"truncated-prefix\n" + receipt_path.read_bytes(),
    )
    write_checksums(malformed)

    output = summarizer.summarize_root(tmp_path)
    excluded = {
        row["directory"]: row["reasons"] for row in output["excluded_runs"]
    }
    assert any(
        "phoenix model receipt attestation failed: malformed-jsonl" in reason
        for reason in excluded["trial-malformed-receipt"]
    )


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


def test_winner_gate_requires_interval_to_clear_the_practical_margin(tmp_path):
    _write_trial(tmp_path, "trial-0", phoenix_wins=10, hve_wins=0)
    _write_trial(tmp_path, "trial-1", phoenix_wins=6, hve_wins=4)
    _write_trial(
        tmp_path,
        "trial-2",
        phoenix_wins=5,
        hve_wins=0,
        draws=1,
    )
    _write_trial(tmp_path, "trial-3", phoenix_wins=1, hve_wins=1)
    _write_trial(tmp_path, "trial-4", phoenix_wins=1, hve_wins=1)

    output = summarizer.summarize_root(tmp_path)
    interval = output["trial_level_task_contract"][
        "score_difference_bootstrap"
    ]["ci95"]

    assert interval == {"lo": 0.04, "hi": 0.773333}
    assert output["decision"] == "inconclusive"
    assert "practical superiority margin" in output["decision_reason"]
    assert output["trial_level_task_contract"][
        "exact_two_sided_sign_test"
    ]["p_value"] == 0.25


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


def test_comparison_requires_an_explicit_model():
    parser = compare_phoenix_hve._argument_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [
                "--phoenix-repo",
                "phoenix",
                "--hve-repo",
                "hve",
            ]
        )
    assert exc.value.code == 2

    parsed = parser.parse_args(
        [
            "--phoenix-repo",
            "phoenix",
            "--hve-repo",
            "hve",
            "--model",
            "explicit-model-id",
        ]
    )
    assert parsed.model == "explicit-model-id"


def test_comparison_accepts_a_unique_explicit_balanced_seed_set():
    parser = compare_phoenix_hve._argument_parser()
    parsed = parser.parse_args(
        [
            "--phoenix-repo",
            "phoenix",
            "--hve-repo",
            "hve",
            "--model",
            "explicit-model-id",
            "--held-out-seed",
            "10000",
            "--held-out-seed",
            "10006",
        ]
    )
    assert parsed.held_out_seed == [10000, 10006]


@pytest.mark.parametrize(
    "value",
    ["", " ", " unknown", "unknown", "default", "auto", "model with space"],
)
def test_comparison_rejects_implicit_or_ambiguous_model_values(value):
    parser = compare_phoenix_hve._argument_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(
            [
                "--phoenix-repo",
                "phoenix",
                "--hve-repo",
                "hve",
                "--model",
                value,
            ]
        )
    assert exc.value.code == 2


def test_candidate_validity_is_separate_from_execution_and_model_provenance(tmp_path):
    bot = tmp_path / "main.py"
    bot.write_text("print('up')\n", encoding="utf-8")
    validation = {"compile_ok": True, "smoke_ok": True}

    assert compare_phoenix_hve._candidate_is_valid(
        bot_path=bot,
        validation=validation,
    )
    validation["smoke_ok"] = False
    assert not compare_phoenix_hve._candidate_is_valid(
        bot_path=bot,
        validation=validation,
    )


def test_model_attestation_requires_complete_single_exact_successful_receipt():
    passing = {
        "observed_models": ["model-a"],
        "model_event_count": 3,
        "model_event_types": {"assistant.message": 3},
        "parse_error_count": 0,
        "utf8_decode_errors": 0,
        "non_object_event_count": 0,
        "terminal_result_count": 1,
        "terminal_success": True,
    }
    assert compare_phoenix_hve._model_attestation(
        passing,
        requested_model="model-a",
    )["status"] == "pass"

    cases = [
        ({**passing, "observed_models": ["model-a", "model-b"]}, "mixed-model-evidence"),
        ({**passing, "observed_models": []}, "model-evidence-missing"),
        ({**passing, "parse_error_count": 1}, "malformed-jsonl"),
        (
            {**passing, "observed_models": ["same-wrong-model"]},
            "requested-reported-model-mismatch",
        ),
        ({**passing, "terminal_result_count": 0}, "terminal-result-count-invalid"),
        ({**passing, "terminal_success": False}, "terminal-result-unsuccessful"),
    ]
    for runtime, expected_reason in cases:
        receipt = compare_phoenix_hve._model_attestation(
            runtime,
            requested_model="model-a",
        )
        assert receipt["status"] == "fail"
        assert expected_reason in receipt["reasons"]


def test_readme_does_not_claim_requested_model_when_attestation_failed():
    document = {
        "run_id": "trial",
        "methodology": {"requested_model": "requested-model"},
        "builds": {
            "phoenix": {
                "model_attestation": {
                    "status": "pass",
                    "observed_models": ["requested-model"],
                }
            },
            "hve": {
                "model_attestation": {
                    "status": "fail",
                    "observed_models": ["different-model"],
                }
            },
        },
        "trial_outcome": {
            "classification": "model-attestation-failed",
            "comparable": False,
        },
        "series": {
            "phoenix_vs_hve": {
                "summary": {
                    "games": 0,
                    "harness_a_wins": 0,
                    "harness_b_wins": 0,
                    "draws": 0,
                }
            }
        },
    }

    rendered = compare_phoenix_hve._render_readme(document)

    assert "This trial is noncomparable" in rendered
    assert "different-model" in rendered
    assert "Both Copilot JSONL receipts consistently reported" not in rendered
