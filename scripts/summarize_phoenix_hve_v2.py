#!/usr/bin/env python3
"""Aggregate local Phoenix/hve-core trials without turning games into trials."""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from atv_bench.comparison import (
    attest_copilot_model_receipt,
    parse_copilot_jsonl,
    verify_checksums,
    write_exact_text,
)

SCHEMA = "atv.phoenix-hve-case-study/v2"
PRACTICAL_MARGIN = 0.05


def _wilson(
    successes: int, trials: int, z: float = 1.96
) -> dict[str, float] | None:
    if trials <= 0:
        return None
    p = successes / trials
    denominator = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / denominator
    margin = (
        z
        * math.sqrt((p * (1 - p) + z * z / (4 * trials)) / trials)
        / denominator
    )
    return {
        "lo": round(max(0.0, center - margin), 4),
        "hi": round(min(1.0, center + margin), 4),
    }


def _bootstrap(
    values: list[float], *, samples: int = 10_000
) -> dict[str, Any] | None:
    """Bootstrap fresh paired trials; nested games never enter this sample."""
    if not values:
        return None
    rng = random.Random(0)
    draws: list[float] = []
    for _ in range(samples):
        sample = [values[rng.randrange(len(values))] for _ in values]
        draws.append(sum(sample) / len(sample))
    draws.sort()
    return {
        "mean": round(sum(values) / len(values), 6),
        "ci95": {
            "lo": round(draws[int(0.025 * (samples - 1))], 6),
            "hi": round(draws[int(0.975 * (samples - 1))], 6),
        },
        "samples": samples,
        "observation_count": len(values),
        "cluster_unit": "fresh_paired_harness_trial",
    }


def _two_sided_sign_test(phoenix_wins: int, hve_wins: int) -> dict[str, Any] | None:
    """Exact two-sided sign test over decisive fresh trials; ties are omitted."""
    decisive = phoenix_wins + hve_wins
    if decisive <= 0:
        return None
    smaller = min(phoenix_wins, hve_wins)
    tail = sum(math.comb(decisive, index) for index in range(smaller + 1))
    p_value = min(1.0, 2.0 * tail / (2**decisive))
    return {
        "phoenix_wins": phoenix_wins,
        "hve_wins": hve_wins,
        "ties_omitted": True,
        "decisive_trials": decisive,
        "p_value": round(p_value, 6),
    }


def _invalid_trial(directory: Path, reason: str) -> dict[str, Any]:
    return {
        "directory": directory.name,
        "load_error": reason,
        "checksums_ok": False,
        "checksum_errors": [reason],
        "schema_version": None,
        "run_id": None,
        "rankable": None,
        "official": None,
        "trust_tier": None,
        "independent_unit": None,
        "runner_script_sha256": None,
        "comparison_module_sha256": None,
        "arena_engine_sha256": None,
        "arena_referee_sha256": None,
        "model": None,
        "model_selection_source": None,
        "copilot_cli": None,
        "held_out_seed_count": None,
        "per_turn_timeout_seconds": None,
        "harness_timeout_seconds": None,
        "max_ai_credits": None,
        "reported_models": {"phoenix": None, "hve": None},
        "model_matches_request": {"phoenix": False, "hve": False},
        "model_receipt_attestation": {
            "phoenix": {"status": "fail", "reasons": ["trial-unreadable"]},
            "hve": {"status": "fail", "reasons": ["trial-unreadable"]},
        },
        "prompt_sha256": None,
        "tool_compatibility_shim": None,
        "tool_compatibility_shim_equal": None,
        "source_commits": {"phoenix": None, "hve": None},
        "source_git_trees": {"phoenix": None, "hve": None},
        "source_tree_listing_sha256": {"phoenix": None, "hve": None},
        "artifact_validity": {"phoenix": False, "hve": False},
        "build_status": {"phoenix": None, "hve": None},
        "bot_sha256": {"phoenix": None, "hve": None},
        "nested_games": {
            "games": 0,
            "harness_a_wins": 0,
            "harness_b_wins": 0,
            "draws": 0,
        },
        "task_contract_winner": None,
        "quality_winner": None,
        "trial_score_difference": None,
        "completed_games": {
            "games": 0,
            "phoenix_wins": 0,
            "hve_wins": 0,
            "draws": 0,
        },
        "forfeit_decomposition": {
            "total": 0,
            "phoenix_forfeits": 0,
            "hve_forfeits": 0,
            "unknown_forfeits": 0,
            "reasons": {},
        },
    }


def _decompose_games(games: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, Any]]:
    """Separate completed gameplay from task-contract forfeits.

    The formal trial score still includes forfeits because responding within the
    deadline is part of the task contract. This decomposition prevents that
    end-to-end score from being misread as pure tactical game strength.
    """
    completed: Counter[str] = Counter()
    forfeits: Counter[str] = Counter()
    reasons: dict[str, Counter[str]] = {
        "phoenix": Counter(),
        "hve": Counter(),
        "unknown": Counter(),
    }
    for game in games:
        if not isinstance(game, dict):
            continue
        outcome = game.get("outcome")
        winner = game.get("winner")
        if outcome in {"forfeit_a", "forfeit_b"}:
            if winner == "harness_a":
                loser = "hve"
            elif winner == "harness_b":
                loser = "phoenix"
            else:
                loser = "unknown"
            forfeits[f"{loser}_forfeits"] += 1
            reason = game.get("forfeit_reason")
            reasons[loser][str(reason or "UNKNOWN")] += 1
            continue
        if winner == "harness_a":
            completed["phoenix_wins"] += 1
        elif winner == "harness_b":
            completed["hve_wins"] += 1
        elif winner == "draw":
            completed["draws"] += 1
        else:
            continue
        completed["games"] += 1
    total_forfeits = sum(forfeits.values())
    return (
        {
            "games": completed["games"],
            "phoenix_wins": completed["phoenix_wins"],
            "hve_wins": completed["hve_wins"],
            "draws": completed["draws"],
        },
        {
            "total": total_forfeits,
            "phoenix_forfeits": forfeits["phoenix_forfeits"],
            "hve_forfeits": forfeits["hve_forfeits"],
            "unknown_forfeits": forfeits["unknown_forfeits"],
            "reasons": {
                name: dict(sorted(counter.items()))
                for name, counter in reasons.items()
                if counter
            },
        },
    )


def load_trial(directory: str | Path) -> dict[str, Any]:
    """Load one directory as one fresh paired trial, even if it has many games."""
    path = Path(directory)
    checksum_ok, checksum_errors = verify_checksums(path)
    try:
        document = json.loads((path / "comparison.json").read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        row = _invalid_trial(path, f"comparison.json is unreadable: {exc}")
        row["checksum_errors"] = checksum_errors or row["checksum_errors"]
        return row
    try:
        builds = document["builds"]
        series = document["series"]["phoenix_vs_hve"]
        summary = series["summary"]
        games = int(summary["games"])
        phoenix_valid = bool(builds["phoenix"].get("valid_artifact"))
        hve_valid = bool(builds["hve"].get("valid_artifact"))
    except (KeyError, TypeError, ValueError) as exc:
        row = _invalid_trial(path, f"comparison document is incomplete: {exc}")
        row["checksums_ok"] = checksum_ok
        row["checksum_errors"] = checksum_errors
        return row

    task_contract_winner = None
    score_difference = None
    if phoenix_valid and hve_valid and games > 0:
        phoenix_points = int(summary["harness_a_wins"]) + 0.5 * int(summary["draws"])
        hve_points = int(summary["harness_b_wins"]) + 0.5 * int(summary["draws"])
        score_difference = (phoenix_points - hve_points) / games
        if score_difference > 0:
            task_contract_winner = "phoenix"
        elif score_difference < 0:
            task_contract_winner = "hve"
        else:
            task_contract_winner = "tie"
    game_rows = series.get("games", [])
    if not isinstance(game_rows, list):
        game_rows = []
    completed_games, forfeit_decomposition = _decompose_games(game_rows)

    methodology = document.get("methodology", {})
    runner = methodology.get("runner", {})
    requested_model = methodology.get("model")
    model_selection_source = methodology.get("model_selection_source")
    receipt_attestation: dict[str, dict[str, Any]] = {}
    reported_models: dict[str, str | None] = {}
    for name in ("phoenix", "hve"):
        candidates = (
            path / "raw" / f"{name}.stdout.bin",
            path / "raw" / f"{name}.jsonl",
        )
        receipt_path = next((item for item in candidates if item.is_file()), None)
        if receipt_path is None:
            receipt_attestation[name] = {
                "status": "fail",
                "requested_model": requested_model,
                "observed_models": [],
                "provider_signed": False,
                "reasons": ["raw-model-receipt-missing"],
            }
            reported_models[name] = None
            continue
        runtime = parse_copilot_jsonl(receipt_path.read_bytes())
        if not isinstance(requested_model, str) or not requested_model:
            receipt = {
                "status": "fail",
                "requested_model": requested_model,
                "observed_models": runtime.get("observed_models", []),
                "provider_signed": False,
                "reasons": ["requested-model-missing"],
            }
        else:
            receipt = attest_copilot_model_receipt(
                runtime,
                requested_model=requested_model,
            )
        receipt["selection_source"] = (
            model_selection_source
            if model_selection_source == "explicit_cli"
            else "historical_runner_request_source_unattested"
        )
        receipt["artifact"] = receipt_path.relative_to(path).as_posix()
        receipt_attestation[name] = receipt
        observed = receipt.get("observed_models", [])
        reported_models[name] = (
            observed[0] if isinstance(observed, list) and len(observed) == 1 else None
        )
    model_matches_request = {
        name: receipt_attestation[name].get("status") == "pass"
        for name in ("phoenix", "hve")
    }
    sources = document.get("sources", {})
    phoenix_source = sources.get("atv_phoenix", {})
    hve_source = sources.get("hve_core", {})
    return {
        "directory": path.name,
        "load_error": None,
        "schema_version": document.get("schema_version"),
        "run_id": document.get("run_id"),
        "checksums_ok": checksum_ok,
        "checksum_errors": checksum_errors,
        "rankable": document.get("rankable"),
        "official": document.get("official"),
        "trust_tier": document.get("trust_tier"),
        "independent_unit": methodology.get("independent_unit"),
        "runner_script_sha256": runner.get("script_sha256"),
        "comparison_module_sha256": runner.get("comparison_module_sha256"),
        "arena_engine_sha256": runner.get("arena_engine_sha256"),
        "arena_referee_sha256": runner.get("arena_referee_sha256"),
        "model": requested_model,
        "model_selection_source": model_selection_source,
        "copilot_cli": methodology.get("copilot_cli"),
        "held_out_seed_count": methodology.get("held_out_seeds"),
        "per_turn_timeout_seconds": methodology.get("per_turn_timeout_seconds"),
        "harness_timeout_seconds": methodology.get("harness_timeout_seconds"),
        "max_ai_credits": methodology.get("max_ai_credits"),
        "reported_models": reported_models,
        "model_matches_request": model_matches_request,
        "model_receipt_attestation": receipt_attestation,
        "prompt_sha256": methodology.get("prompt_sha256"),
        "tool_compatibility_shim": methodology.get("tool_compatibility_shim"),
        "tool_compatibility_shim_equal": methodology.get(
            "tool_compatibility_shim_equal"
        ),
        "source_commits": {
            "phoenix": phoenix_source.get("commit"),
            "hve": hve_source.get("commit"),
        },
        "source_git_trees": {
            "phoenix": phoenix_source.get("git_tree"),
            "hve": hve_source.get("git_tree"),
        },
        "source_tree_listing_sha256": {
            "phoenix": phoenix_source.get("tracked_tree_listing_sha256"),
            "hve": hve_source.get("tracked_tree_listing_sha256"),
        },
        "artifact_validity": {
            "phoenix": phoenix_valid,
            "hve": hve_valid,
        },
        "build_status": {
            "phoenix": builds["phoenix"].get("status"),
            "hve": builds["hve"].get("status"),
        },
        "bot_sha256": {
            "phoenix": builds["phoenix"].get("bot_sha256"),
            "hve": builds["hve"].get("bot_sha256"),
        },
        "nested_games": summary,
        "task_contract_winner": task_contract_winner,
        # Backward-compatible alias. It includes forfeits and must not be read as
        # completed-game tactical quality.
        "quality_winner": task_contract_winner,
        "trial_score_difference": (
            round(score_difference, 6) if score_difference is not None else None
        ),
        "completed_games": completed_games,
        "forfeit_decomposition": forfeit_decomposition,
    }


def exclusion_reasons(
    row: dict[str, Any], reference: dict[str, Any]
) -> list[str]:
    reasons: list[str] = []
    if row["load_error"]:
        reasons.append(row["load_error"])
    if row["schema_version"] != 2:
        reasons.append("not schema v2")
    if not row["checksums_ok"]:
        reasons.append("checksum verification failed")
    if row["rankable"] is not False:
        reasons.append("run is not explicitly non-rankable")
    if row["official"] is not False:
        reasons.append("run is not explicitly unofficial")
    if row["trust_tier"] != "local-self-attested":
        reasons.append("trust tier differs")
    if row["independent_unit"] != "fresh_paired_harness_trial":
        reasons.append("fresh paired trial unit is missing")
    if row["runner_script_sha256"] != reference["runner_script_sha256"]:
        reasons.append("runner script differs")
    if row["comparison_module_sha256"] != reference["comparison_module_sha256"]:
        reasons.append("comparison module differs")
    if row["arena_engine_sha256"] != reference["arena_engine_sha256"]:
        reasons.append("arena engine differs")
    if row["arena_referee_sha256"] != reference["arena_referee_sha256"]:
        reasons.append("arena referee differs")
    if row["model"] != reference["model"]:
        reasons.append("model differs")
    if row["model_selection_source"] != "explicit_cli":
        reasons.append("model selection source is not explicitly attested")
    for field, label in (
        ("copilot_cli", "Copilot CLI"),
        ("held_out_seed_count", "held-out seed count"),
        ("per_turn_timeout_seconds", "per-turn timeout"),
        ("harness_timeout_seconds", "harness timeout"),
        ("max_ai_credits", "AI credit budget"),
    ):
        if row[field] != reference[field] or row[field] is None:
            reasons.append(f"{label} differs or is missing")
    if not all(row["model_matches_request"].values()):
        reasons.append("reported model does not exactly match requested model")
    for name, receipt in row["model_receipt_attestation"].items():
        if receipt.get("status") != "pass":
            detail = ",".join(receipt.get("reasons", [])) or "unknown"
            reasons.append(f"{name} model receipt attestation failed: {detail}")
    if row["prompt_sha256"] != reference["prompt_sha256"]:
        reasons.append("prompt differs")
    if row["tool_compatibility_shim"] is not True:
        reasons.append("tool compatibility shim is not enabled")
    if row["tool_compatibility_shim_equal"] is not True:
        reasons.append("tool compatibility shim was not recorded equally")
    if row["source_commits"] != reference["source_commits"]:
        reasons.append("source commits differ")
    if row["source_git_trees"] != reference["source_git_trees"]:
        reasons.append("source Git trees differ")
    if row["source_tree_listing_sha256"] != reference["source_tree_listing_sha256"]:
        reasons.append("source tree listing digests differ")
    if None in row["source_git_trees"].values():
        reasons.append("immutable Git tree identity missing")
    if None in row["source_tree_listing_sha256"].values():
        reasons.append("tracked tree listing digest missing")
    if not isinstance(row["run_id"], str) or not row["run_id"]:
        reasons.append("run id missing")
    return list(dict.fromkeys(reasons))


def _reference(rows: list[dict[str, Any]], runner_sha: str | None) -> dict[str, Any]:
    candidates = [
        row
        for row in rows
        if row["schema_version"] == 2
        and row["checksums_ok"]
        and row["runner_script_sha256"]
    ]
    if runner_sha is not None:
        candidates = [
            row for row in candidates if row["runner_script_sha256"] == runner_sha
        ]
    if not candidates:
        message = (
            f"no checksum-valid schema-v2 run for runner {runner_sha}"
            if runner_sha
            else "no checksum-valid schema-v2 comparison runs with runner identity"
        )
        raise ValueError(message)
    return candidates[-1]


def summarize_rows(
    rows: list[dict[str, Any]],
    *,
    runner_sha: str | None = None,
    minimum_trials: int = 5,
) -> dict[str, Any]:
    if minimum_trials < 5:
        raise ValueError("minimum_trials cannot be less than the credibility gate of 5")
    reference = _reference(rows, runner_sha)

    provisionally_included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        reasons = exclusion_reasons(row, reference)
        if reasons:
            excluded.append({"directory": row["directory"], "reasons": reasons})
        else:
            provisionally_included.append(row)

    run_id_counts = Counter(row["run_id"] for row in provisionally_included)
    included: list[dict[str, Any]] = []
    for row in provisionally_included:
        if run_id_counts[row["run_id"]] > 1:
            excluded.append(
                {
                    "directory": row["directory"],
                    "reasons": ["duplicate run id is not an independent fresh trial"],
                }
            )
        else:
            included.append(row)

    task_contract_trials = [
        row
        for row in included
        if all(row["artifact_validity"].values())
        and row["trial_score_difference"] is not None
    ]
    differences = [
        float(row["trial_score_difference"]) for row in task_contract_trials
    ]
    bootstrap = _bootstrap(differences)
    trial_wins = Counter(
        row["task_contract_winner"] for row in task_contract_trials
    )
    sign_test = _two_sided_sign_test(
        trial_wins["phoenix"],
        trial_wins["hve"],
    )

    validity: dict[str, dict[str, Any]] = {
        name: {
            "valid": sum(bool(row["artifact_validity"][name]) for row in included),
            "trials": len(included),
        }
        for name in ("phoenix", "hve")
    }
    for value in validity.values():
        value["rate"] = (
            round(value["valid"] / value["trials"], 4) if value["trials"] else None
        )
        value["ci95"] = _wilson(value["valid"], value["trials"])

    nested: Counter[str] = Counter()
    completed_nested: Counter[str] = Counter()
    forfeits_nested: Counter[str] = Counter()
    forfeit_reasons: dict[str, Counter[str]] = {
        "phoenix": Counter(),
        "hve": Counter(),
        "unknown": Counter(),
    }
    for row in task_contract_trials:
        summary = row["nested_games"]
        nested["games"] += int(summary["games"])
        nested["phoenix_wins"] += int(summary["harness_a_wins"])
        nested["hve_wins"] += int(summary["harness_b_wins"])
        nested["draws"] += int(summary["draws"])
        completed = row["completed_games"]
        for key in ("games", "phoenix_wins", "hve_wins", "draws"):
            completed_nested[key] += int(completed[key])
        forfeits = row["forfeit_decomposition"]
        for key in (
            "total",
            "phoenix_forfeits",
            "hve_forfeits",
            "unknown_forfeits",
        ):
            forfeits_nested[key] += int(forfeits[key])
        for harness, reason_counts in forfeits.get("reasons", {}).items():
            for reason_name, count in reason_counts.items():
                forfeit_reasons.setdefault(harness, Counter())[reason_name] += int(count)

    decision = "inconclusive"
    reason = (
        f"requires at least {minimum_trials} comparable both-valid fresh paired trials; "
        f"observed {len(task_contract_trials)}"
    )
    if len(task_contract_trials) >= minimum_trials and bootstrap is not None:
        low = bootstrap["ci95"]["lo"]
        high = bootstrap["ci95"]["hi"]
        mean = bootstrap["mean"]
        if mean > PRACTICAL_MARGIN and low > PRACTICAL_MARGIN:
            decision = "phoenix_better_on_this_task_contract"
            reason = (
                "fresh end-to-end trial interval, including forfeits, clears the "
                f"{PRACTICAL_MARGIN:.2f} practical margin"
            )
        elif mean < -PRACTICAL_MARGIN and high < -PRACTICAL_MARGIN:
            decision = "hve_better_on_this_task_contract"
            reason = (
                "fresh end-to-end trial interval, including forfeits, clears the "
                f"{PRACTICAL_MARGIN:.2f} practical margin in hve-core's direction"
            )
        elif low >= -PRACTICAL_MARGIN and high <= PRACTICAL_MARGIN:
            decision = "practically_equivalent_on_this_task_contract"
            reason = "fresh-trial interval is inside the configured equivalence region"
        else:
            reason = (
                "fresh-trial interval does not clear the configured practical "
                "superiority margin or equivalence gate"
            )

    return {
        "schema": SCHEMA,
        "trust_tier": "local-self-attested",
        "rankable": False,
        "official": False,
        "global_harness_winner": None,
        "independent_unit": "fresh_paired_harness_trial",
        "nested_games_are_not_independent_trials": True,
        "runner_script_sha256": reference["runner_script_sha256"],
        "comparison_module_sha256": reference["comparison_module_sha256"],
        "arena_engine_sha256": reference["arena_engine_sha256"],
        "arena_referee_sha256": reference["arena_referee_sha256"],
        "model": reference["model"],
        "reported_model_match_required": True,
        "copilot_cli": reference["copilot_cli"],
        "held_out_seed_count": reference["held_out_seed_count"],
        "per_turn_timeout_seconds": reference["per_turn_timeout_seconds"],
        "harness_timeout_seconds": reference["harness_timeout_seconds"],
        "max_ai_credits": reference["max_ai_credits"],
        "prompt_sha256": reference["prompt_sha256"],
        "source_commits": reference["source_commits"],
        "source_git_trees": reference["source_git_trees"],
        "source_tree_listing_sha256": reference["source_tree_listing_sha256"],
        "tool_compatibility_shim": True,
        "tool_compatibility_shim_equal": True,
        "included_trials": included,
        "excluded_runs": excluded,
        "trial_counts": {
            "included": len(included),
            "both_valid": len(task_contract_trials),
            "minimum_for_task_contract_decision": minimum_trials,
        },
        "artifact_validity": validity,
        "decision_basis": "end_to_end_task_contract_including_forfeits",
        "trial_level_task_contract": {
            "wins": dict(trial_wins),
            "score_difference_bootstrap": bootstrap,
            "exact_two_sided_sign_test": sign_test,
        },
        "trial_level_quality": {
            "deprecated_misleading_name": True,
            "includes_forfeits": True,
            "wins": dict(trial_wins),
            "score_difference_bootstrap": bootstrap,
        },
        "nested_game_totals_descriptive_only": dict(nested),
        "completed_game_totals_descriptive_only": dict(completed_nested),
        "forfeit_totals_descriptive_only": {
            **dict(forfeits_nested),
            "reasons": {
                name: dict(sorted(counter.items()))
                for name, counter in forfeit_reasons.items()
                if counter
            },
        },
        "decision": decision,
        "decision_reason": reason,
        "limitations": [
            "One model, one synthetic Lightcycles task contract, and local execution.",
            "Tool allowlists were compatibility-shimmed equally and hashes were recorded.",
            "Provider credentials entered the harness process.",
            "Network isolation was not technically enforced.",
            "Games are nested under generated artifacts and never counted as trials.",
            "The formal task-contract decision includes timeout/crash forfeits; completed-game tactical results are reported separately.",
            "Historical CRASH labels may conflate timeout, EOF, and invalid response in the pre-fix referee.",
            "This is not protocol-v1 OCI evidence or an overall harness-richness ranking.",
        ],
    }


def summarize_root(
    root: str | Path,
    *,
    runner_sha: str | None = None,
    minimum_trials: int = 5,
) -> dict[str, Any]:
    directory = Path(root).resolve()
    rows = [
        load_trial(path)
        for path in sorted(directory.iterdir())
        if path.is_dir() and (path / "comparison.json").is_file()
    ]
    if not rows:
        raise ValueError("no comparison trial directories found")
    return summarize_rows(
        rows, runner_sha=runner_sha, minimum_trials=minimum_trials
    )


def render_markdown(output: dict[str, Any]) -> str:
    lines = [
        "# NON-RANKABLE ATV-Phoenix vs hve-core local case study",
        "",
        f"Decision: **{output['decision']}**.",
        "",
        output["decision_reason"] + ".",
        "",
        "**This is unofficial local evidence. No global harness winner is claimed.**",
        "",
        "One row is one fresh paired harness trial. Game counts are nested descriptive evidence only.",
        "",
        "| Trial | Phoenix artifact | hve artifact | Phoenix wins | hve wins | Draws | Forfeits | End-to-end trial outcome |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in output["included_trials"]:
        summary = row["nested_games"]
        lines.append(
            f"| {row['directory']} | "
            f"{'valid' if row['artifact_validity']['phoenix'] else 'invalid'} | "
            f"{'valid' if row['artifact_validity']['hve'] else 'invalid'} | "
            f"{summary['harness_a_wins']} | {summary['harness_b_wins']} | "
            f"{summary['draws']} | {row['forfeit_decomposition']['total']} | "
            f"{row['task_contract_winner'] or 'reliability-only'} |"
        )
    counts = output["trial_counts"]
    validity = output["artifact_validity"]
    lines.extend(
        [
            "",
            f"Both-valid fresh trials: **{counts['both_valid']}** / required "
            f"**{counts['minimum_for_task_contract_decision']}**.",
            f"Phoenix valid-artifact rate: **{validity['phoenix']['valid']}/"
            f"{validity['phoenix']['trials']}**.",
            f"hve-core valid-artifact rate: **{validity['hve']['valid']}/"
            f"{validity['hve']['trials']}**.",
            "",
            "The formal decision includes forfeits because deadline compliance is part of the task contract.",
            (
                "Completed games only: Phoenix "
                f"**{output['completed_game_totals_descriptive_only'].get('phoenix_wins', 0)}**, "
                "hve-core "
                f"**{output['completed_game_totals_descriptive_only'].get('hve_wins', 0)}**, "
                "draws "
                f"**{output['completed_game_totals_descriptive_only'].get('draws', 0)}**."
            ),
            (
                "Recorded forfeits: Phoenix "
                f"**{output['forfeit_totals_descriptive_only'].get('phoenix_forfeits', 0)}**, "
                "hve-core "
                f"**{output['forfeit_totals_descriptive_only'].get('hve_forfeits', 0)}**."
            ),
            "",
            "No overall harness richness, sophistication, or production-readiness ranking is inferred.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--runner-sha")
    parser.add_argument("--minimum-trials", type=int, default=5)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    try:
        output = summarize_root(
            root,
            runner_sha=args.runner_sha,
            minimum_trials=args.minimum_trials,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    write_exact_text(
        root / "aggregate-v2.json",
        json.dumps(output, indent=2, sort_keys=True) + "\n",
    )
    write_exact_text(root / "SUMMARY-v2.md", render_markdown(output))
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
