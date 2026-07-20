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

from atv_bench.comparison import verify_checksums, write_exact_text

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
        "model": None,
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
        "quality_winner": None,
        "trial_score_difference": None,
    }


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

    quality_winner = None
    score_difference = None
    if phoenix_valid and hve_valid and games > 0:
        phoenix_points = int(summary["harness_a_wins"]) + 0.5 * int(summary["draws"])
        hve_points = int(summary["harness_b_wins"]) + 0.5 * int(summary["draws"])
        score_difference = (phoenix_points - hve_points) / games
        if score_difference > 0:
            quality_winner = "phoenix"
        elif score_difference < 0:
            quality_winner = "hve"
        else:
            quality_winner = "tie"

    methodology = document.get("methodology", {})
    runner = methodology.get("runner", {})
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
        "model": methodology.get("model"),
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
        "quality_winner": quality_winner,
        "trial_score_difference": (
            round(score_difference, 6) if score_difference is not None else None
        ),
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
    if row["model"] != reference["model"]:
        reasons.append("model differs")
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
        and row["rankable"] is False
        and row["official"] is False
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

    quality = [
        row
        for row in included
        if all(row["artifact_validity"].values())
        and row["trial_score_difference"] is not None
    ]
    differences = [float(row["trial_score_difference"]) for row in quality]
    bootstrap = _bootstrap(differences)

    validity = {
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

    nested = Counter()
    for row in quality:
        summary = row["nested_games"]
        nested["games"] += int(summary["games"])
        nested["phoenix_wins"] += int(summary["harness_a_wins"])
        nested["hve_wins"] += int(summary["harness_b_wins"])
        nested["draws"] += int(summary["draws"])

    decision = "inconclusive"
    reason = (
        f"requires at least {minimum_trials} comparable both-valid fresh paired trials; "
        f"observed {len(quality)}"
    )
    if len(quality) >= minimum_trials and bootstrap is not None:
        low = bootstrap["ci95"]["lo"]
        high = bootstrap["ci95"]["hi"]
        mean = bootstrap["mean"]
        if mean > PRACTICAL_MARGIN and low > 0:
            decision = "phoenix_better_on_this_task_contract"
            reason = (
                "fresh-trial interval excludes zero and mean exceeds the "
                f"{PRACTICAL_MARGIN:.2f} practical margin"
            )
        elif mean < -PRACTICAL_MARGIN and high < 0:
            decision = "hve_better_on_this_task_contract"
            reason = (
                "fresh-trial interval excludes zero and mean exceeds the "
                f"{PRACTICAL_MARGIN:.2f} practical margin in hve-core's direction"
            )
        elif low >= -PRACTICAL_MARGIN and high <= PRACTICAL_MARGIN:
            decision = "practically_equivalent_on_this_task_contract"
            reason = "fresh-trial interval is inside the preregistered equivalence region"
        else:
            reason = "fresh-trial interval does not pass winner or equivalence gates"

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
        "model": reference["model"],
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
            "both_valid": len(quality),
            "minimum_for_task_contract_decision": minimum_trials,
        },
        "artifact_validity": validity,
        "trial_level_quality": {
            "wins": dict(Counter(row["quality_winner"] for row in quality)),
            "score_difference_bootstrap": bootstrap,
        },
        "nested_game_totals_descriptive_only": dict(nested),
        "decision": decision,
        "decision_reason": reason,
        "limitations": [
            "One model, one synthetic Lightcycles task contract, and local execution.",
            "Tool allowlists were compatibility-shimmed equally and hashes were recorded.",
            "Provider credentials entered the harness process.",
            "Network isolation was not technically enforced.",
            "Games are nested under generated artifacts and never counted as trials.",
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
        "| Trial | Phoenix artifact | hve artifact | Phoenix wins | hve wins | Draws | Trial outcome |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in output["included_trials"]:
        summary = row["nested_games"]
        lines.append(
            f"| {row['directory']} | "
            f"{'valid' if row['artifact_validity']['phoenix'] else 'invalid'} | "
            f"{'valid' if row['artifact_validity']['hve'] else 'invalid'} | "
            f"{summary['harness_a_wins']} | {summary['harness_b_wins']} | "
            f"{summary['draws']} | {row['quality_winner'] or 'reliability-only'} |"
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
