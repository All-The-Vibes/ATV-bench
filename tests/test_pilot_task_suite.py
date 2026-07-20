"""Deterministic integrity and credibility gates for the public pilot corpus."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest

from atv_bench.eval._canonical import canonical_json_bytes, tree_digest
from atv_bench.eval.grader import FileAssertionsGrader
from atv_bench.eval.tasks import (
    ReviewLevel,
    ReviewSuiteStatus,
    TaskPackageValidator,
    load_task_suite,
)
from atv_bench.protocol.schemas import SchemaKind, default_schema_store


ROOT = Path(__file__).resolve().parents[1]
PILOT_ROOT = ROOT / "tasks" / "pilot"
GENERATOR = ROOT / "scripts" / "build_pilot_tasks.py"
EXPECTED_COUNTS = {
    "greenfield": 10,
    "repair": 10,
    "debugging": 10,
    "recovery": 10,
    "context-retrieval": 10,
}


@pytest.fixture(scope="module")
def pilot_packages():
    roots = sorted(path for path in PILOT_ROOT.iterdir() if path.is_dir())
    return load_task_suite(roots)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(
            (candidate for candidate in root.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(root).as_posix(),
        )
    }


def _semantic_fingerprint(package) -> str:
    manifest = package.manifest
    grader = json.loads(package.grader_path.read_text(encoding="utf-8"))
    normalized_assertions = [
        {key: value for key, value in assertion.items() if key != "id"}
        for assertion in grader["assertions"]
    ]
    payload = {
        "category": package.category,
        "prompt": package.prompt_path.read_text(encoding="utf-8"),
        "source_digest": manifest["source"]["tree_digest"]["value"],
        "operation_tags": sorted(
            tag
            for tag in manifest["capability_tags"]
            if tag.startswith("operation-")
        ),
        "assertions": normalized_assertions,
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def test_pilot_has_exact_category_counts_and_unique_versioned_ids(pilot_packages):
    assert len(pilot_packages) == 50
    assert Counter(package.category for package in pilot_packages) == EXPECTED_COUNTS
    assert len({package.id for package in pilot_packages}) == 50
    assert all(package.id.startswith(f"pilot.{package.category}.") for package in pilot_packages)
    assert all(package.version == "1.0.0" for package in pilot_packages)


def test_pilot_is_public_machine_reviewed_and_not_official(pilot_packages):
    for package in pilot_packages:
        manifest = package.manifest
        assert manifest["visibility"] == "public"
        assert package.review_level is ReviewLevel.MACHINE_DUAL_REVIEW
        assert package.suite_status is ReviewSuiteStatus.INTERNAL_MACHINE_REVIEWED
        assert package.official_review_eligible is False
        assert package.review_evidence.human_reviewer_count == 0
        assert len(package.review_evidence.reviewers) == 2
        assert all(
            reviewer.reviewer_kind == "machine"
            for reviewer in package.review_evidence.reviewers
        )


def test_all_pilot_tasks_conform_and_pass_every_machine_gate(pilot_packages):
    store = default_schema_store()
    validator = TaskPackageValidator()
    started = time.perf_counter()
    for package in pilot_packages:
        store.validate(package.manifest, SchemaKind.TASK)
        report = validator.validate(
            package,
            FileAssertionsGrader.from_task(package),
        )
        assert report.eligible is True
        assert report.machine_eligible is True
        assert report.official_review_eligible is False
        assert report.official_eligible is False
        assert report.suite_status is ReviewSuiteStatus.INTERNAL_MACHINE_REVIEWED
        assert all(gate.passed for gate in report.gates)
    elapsed = time.perf_counter() - started
    print(f"pilot_machine_validation_seconds={elapsed:.3f}")
    assert elapsed < 180


def test_oracle_alternative_exploit_mutation_and_noop_are_content_distinct(
    pilot_packages,
):
    for package in pilot_packages:
        candidates = {
            case: path
            for _, case, path, _ in package.validation_cases()
        }
        digests = {
            "noop": tree_digest(candidates["no_op"]),
            "oracle": tree_digest(candidates["oracle"]),
            "alternative": tree_digest(candidates["alternative_solutions[0]"]),
            "exploit": tree_digest(candidates["exploit_cases[0]"]),
            "mutation": tree_digest(candidates["mutation_cases[0]"]),
        }
        assert len(set(digests.values())) == 5


def test_corpus_diversity_rejects_clone_inflation(pilot_packages):
    assert len({package.digest for package in pilot_packages}) == 50
    assert len(
        {
            package.manifest["source"]["tree_digest"]["value"]
            for package in pilot_packages
        }
    ) == 50
    assert len(
        {
            package.manifest["prompt"]["digest"]["value"]
            for package in pilot_packages
        }
    ) == 50
    assert len(
        {
            package.manifest["grader"]["hidden_inputs_digest"]["value"]
            for package in pilot_packages
        }
    ) == 50
    assert len({_semantic_fingerprint(package) for package in pilot_packages}) == 50

    for category, expected_count in EXPECTED_COUNTS.items():
        category_packages = [
            package for package in pilot_packages if package.category == category
        ]
        operation_tags = {
            tag
            for package in category_packages
            for tag in package.manifest["capability_tags"]
            if tag.startswith("operation-")
        }
        assert len(category_packages) == expected_count
        assert len(operation_tags) == expected_count
        assert len(
            {package.prompt_path.read_bytes() for package in category_packages}
        ) == expected_count


def test_content_contracts_digests_licenses_and_budgets_are_complete_and_bounded(
    pilot_packages,
):
    total_task_wall_time = 0
    total_grader_wall_time = 0
    for package in pilot_packages:
        manifest = package.manifest
        total_task_wall_time += manifest["budget_limits"]["wall_time_ms"]
        total_grader_wall_time += manifest["grader"]["budget_limits"]["wall_time_ms"]
        assert manifest["budget_limits"]["wall_time_ms"] <= 15_000
        assert manifest["grader"]["budget_limits"]["wall_time_ms"] <= 5_000
        assert manifest["output"]["max_files"] <= 16
        assert manifest["output"]["max_total_bytes"] <= 262_144
        assert "@sha256:" in manifest["environment"]["image"]
        assert "@sha256:" in manifest["grader"]["image"]
        assert manifest["license"] == {
            "spdx": "MIT",
            "redistribution": "allowed",
        }
        workspace_files = sorted(
            path.relative_to(package.public_workspace).as_posix()
            for path in package.public_workspace.rglob("*")
            if path.is_file()
        )
        assert manifest["output"]["allow_any_relative_path"] is False
        assert manifest["output"]["required_paths"] == workspace_files
        assert manifest["output"]["allowed_paths"] == workspace_files
        assert manifest["source"]["tree_digest"]["value"] == tree_digest(
            package.public_workspace
        )
    assert total_task_wall_time == 750_000
    assert total_grader_wall_time == 250_000


def test_pilot_contains_only_safe_utf8_regular_files_without_credentials():
    forbidden_fragments = (
        b"-----begin private key-----",
        b"sk-proj-",
        b"password=",
        b"api_key",
        b"authorization: bearer",
    )
    reserved_windows_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
    casefolded_paths: set[str] = set()
    for path in sorted(PILOT_ROOT.rglob("*")):
        relative = path.relative_to(PILOT_ROOT).as_posix()
        observed = os.lstat(path)
        assert not stat.S_ISLNK(observed.st_mode)
        if path.is_dir():
            assert stat.S_ISDIR(observed.st_mode)
            continue
        assert stat.S_ISREG(observed.st_mode)
        assert getattr(observed, "st_nlink", 1) == 1
        assert relative.casefold() not in casefolded_paths
        casefolded_paths.add(relative.casefold())
        assert all(
            part.split(".", 1)[0].upper() not in reserved_windows_names
            for part in path.relative_to(PILOT_ROOT).parts
        )
        data = path.read_bytes()
        data.decode("utf-8", errors="strict")
        assert b"\x00" not in data
        assert b"\r" not in data
        lowered = data.lower()
        assert not any(fragment in lowered for fragment in forbidden_fragments)
        assert b":\\" not in data


def test_generator_regeneration_is_byte_identical(tmp_path):
    regenerated = tmp_path / "pilot-regenerated"
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "7919"
    completed = subprocess.run(
        [
            sys.executable,
            str(GENERATOR),
            "--output",
            str(regenerated),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    summary = json.loads(completed.stdout)
    assert summary["count"] == 50
    assert summary["categories"] == EXPECTED_COUNTS
    assert summary["suite_status"] == "internal-machine-reviewed"
    assert summary["official_eligible"] is False
    assert _snapshot(regenerated) == _snapshot(PILOT_ROOT)
