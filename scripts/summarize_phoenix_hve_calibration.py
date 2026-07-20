#!/usr/bin/env python3
"""Select a non-scored Phoenix/hve budget only after both can finish reliably."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from atv_bench.comparison import write_exact_text
from scripts.summarize_phoenix_hve_v2 import (
    _reference,
    exclusion_reasons,
    load_trial,
)

SCHEMA = "atv.phoenix-hve-calibration/v1"


def summarize_calibration_rows(
    rows: list[dict[str, Any]],
    *,
    required_attempts: int = 2,
    minimum_pass_rate: float = 1.0,
) -> dict[str, Any]:
    if required_attempts < 1:
        raise ValueError("required_attempts must be positive")
    if not 0.0 < minimum_pass_rate <= 1.0:
        raise ValueError("minimum_pass_rate must be in (0, 1]")
    calibration_rows = [row for row in rows if row.get("phase") == "calibration"]
    if not calibration_rows:
        raise ValueError("no calibration-phase trials found")
    reference = _reference(calibration_rows, runner_sha=None)

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in calibration_rows:
        reasons = exclusion_reasons(
            row,
            reference,
            expected_phase="calibration",
            compare_budget=False,
        )
        if reasons:
            excluded.append({"directory": row["directory"], "reasons": reasons})
        else:
            included.append(row)

    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in included:
        budget = row.get("max_ai_credits")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
            excluded.append(
                {
                    "directory": row["directory"],
                    "reasons": ["AI credit budget is invalid"],
                }
            )
            continue
        grouped[budget].append(row)

    budgets: list[dict[str, Any]] = []
    selected_budget: int | None = None
    for budget, attempts in sorted(grouped.items()):
        phoenix_valid = sum(
            bool(row["artifact_validity"]["phoenix"])
            and bool(row["execution_validity"]["phoenix"])
            and bool(row["model_matches_request"]["phoenix"])
            for row in attempts
        )
        hve_valid = sum(
            bool(row["artifact_validity"]["hve"])
            and bool(row["execution_validity"]["hve"])
            and bool(row["model_matches_request"]["hve"])
            for row in attempts
        )
        both_valid = sum(bool(row["calibration_pass"]) for row in attempts)
        count = len(attempts)
        phoenix_rate = phoenix_valid / count if count else 0.0
        hve_rate = hve_valid / count if count else 0.0
        both_rate = both_valid / count if count else 0.0
        passed = bool(
            count >= required_attempts
            and phoenix_rate >= minimum_pass_rate
            and hve_rate >= minimum_pass_rate
            and both_rate >= minimum_pass_rate
        )
        budgets.append(
            {
                "max_ai_credits": budget,
                "attempts": count,
                "phoenix_valid": phoenix_valid,
                "hve_valid": hve_valid,
                "both_valid": both_valid,
                "phoenix_valid_rate": round(phoenix_rate, 6),
                "hve_valid_rate": round(hve_rate, 6),
                "both_valid_rate": round(both_rate, 6),
                "passed": passed,
                "directories": [row["directory"] for row in attempts],
            }
        )
        if passed and selected_budget is None:
            selected_budget = budget

    decision = "calibrated" if selected_budget is not None else "failed"
    reason = (
        f"selected the smallest budget with at least {required_attempts} attempts "
        f"and pass rate >= {minimum_pass_rate:.3f} for both harnesses"
        if selected_budget is not None
        else "no tested budget passed the paired completion-feasibility gate"
    )
    return {
        "schema": SCHEMA,
        "rankable": False,
        "official": False,
        "scored": False,
        "decision": decision,
        "decision_reason": reason,
        "selected_max_ai_credits": selected_budget,
        "required_attempts": required_attempts,
        "minimum_pass_rate": minimum_pass_rate,
        "budgets": budgets,
        "included_attempts": len(included),
        "excluded_attempts": excluded,
        "runner_script_sha256": reference["runner_script_sha256"],
        "model": reference["model"],
        "source_commits": reference["source_commits"],
        "limitations": [
            "Calibration is public and non-scored.",
            "A passing budget establishes completion feasibility, not harness quality.",
            "Changing the selected budget after evaluation starts creates a new cell.",
        ],
    }


def summarize_calibration_root(
    root: str | Path,
    *,
    required_attempts: int = 2,
    minimum_pass_rate: float = 1.0,
) -> dict[str, Any]:
    directory = Path(root).resolve()
    rows = [
        load_trial(path)
        for path in sorted(directory.iterdir())
        if path.is_dir() and (path / "comparison.json").is_file()
    ]
    return summarize_calibration_rows(
        rows,
        required_attempts=required_attempts,
        minimum_pass_rate=minimum_pass_rate,
    )


def render_markdown(output: dict[str, Any]) -> str:
    lines = [
        "# NON-SCORED Phoenix/hve completion calibration",
        "",
        f"Decision: **{output['decision']}**.",
        "",
        output["decision_reason"] + ".",
        "",
        "| AI credits | Attempts | Phoenix valid | hve valid | Both valid | Pass |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in output["budgets"]:
        lines.append(
            f"| {row['max_ai_credits']} | {row['attempts']} | "
            f"{row['phoenix_valid']} | {row['hve_valid']} | "
            f"{row['both_valid']} | {'yes' if row['passed'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            f"Selected budget: **{output['selected_max_ai_credits']}**.",
            "",
            "Calibration is never scored as harness quality.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root")
    parser.add_argument("--required-attempts", type=int, default=2)
    parser.add_argument("--minimum-pass-rate", type=float, default=1.0)
    args = parser.parse_args()
    try:
        output = summarize_calibration_root(
            args.root,
            required_attempts=args.required_attempts,
            minimum_pass_rate=args.minimum_pass_rate,
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    root = Path(args.root).resolve()
    write_exact_text(
        root / "calibration.json",
        json.dumps(output, indent=2, sort_keys=True) + "\n",
    )
    write_exact_text(root / "CALIBRATION.md", render_markdown(output))
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
