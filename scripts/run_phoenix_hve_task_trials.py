#!/usr/bin/env python3
"""Run resumable Phoenix-versus-hve trials over ATV task packages.

Each task has one atomically replaced checkpoint document using
``atv.phoenix-hve-task-trial/v1``.  A completed document contains exactly five
nested paired attempts, which is the shape consumed by
``summarize_phoenix_hve_tasks.py``.

This remains local, self-attested, and explicitly non-rankable.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

from atv_bench.comparison import (
    git_commit,
    git_tree,
    parse_copilot_jsonl,
    sha256_file,
    tracked_tree_listing_sha256,
)
from atv_bench.eval._canonical import (
    DEFAULT_MAX_FILE_BYTES,
    TreeLimits,
    canonical_json_bytes,
    sha256_bytes,
    sha256_json,
    snapshot_regular_tree,
    tree_digest,
    tree_digest_from_snapshots,
)
from atv_bench.eval.grader import (
    ControllerAssertedLifecycleReceipt,
    FileAssertionsGrader,
    GradeResult,
)
from atv_bench.eval.tasks import TaskPackage, TaskPackageError, load_task_suite
from scripts.compare_phoenix_hve import (
    HarnessExecution,
    _ambient_skill_names,
    _command,
    _common_env,
    _copilot_argv,
    _github_token,
    _model_attestation,
    _model_identifier,
    _prepare_hve,
    _prepare_phoenix,
    _run_harness,
    _run_text,
)
from scripts.phoenix_hve_oci_runtime import (
    OciBuildConfig,
    OciImage,
    OciRunConfig,
    OciRuntimeError,
    ResourceLimits,
    build_or_reuse_images,
    run_harness as run_oci_harness,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
TRIAL_SCHEMA = "atv.phoenix-hve-task-trial/v1"
EXPERIMENT_SCHEMA = "atv.phoenix-hve-task-experiment/v1"
SUMMARY_SCHEMA = "atv.phoenix-hve-task-run-summary/v1"
ATTEMPTS_PER_TASK = 5
HARNESSES = ("phoenix", "hve")
DIRECTORY_PUBLISH_ATTEMPTS = 10
FORMAL_ANALYSIS_POLICY = {
    "superiority_equivalence_margin": 0.05,
    "confidence": 0.95,
    "bootstrap_samples": 10_000,
    "bootstrap_seed": 20_260_721,
    "reliability_alpha": 0.05,
    "superiority_min_at_least_one_reliable_suite_rate": 0.90,
    "superiority_min_at_least_one_reliable_per_task": 4,
    "equivalence_min_both_reliable_suite_rate": 0.90,
    "equivalence_min_both_reliable_per_task": 4,
}
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


class TaskTrialRunnerError(RuntimeError):
    """The runner cannot continue without weakening or corrupting evidence."""


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    phoenix_repo: Path
    hve_repo: Path
    task_roots: tuple[Path, ...]
    model: str
    max_ai_credits: int
    timeout_seconds: int
    randomization_seed: int
    ledger_dir: Path
    evidence_root: Path
    task_ids: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    selection_file: Path | None = None
    preregistration_file: Path | None = None
    calibration_plan_file: Path | None = None
    calibration_summary_file: Path | None = None
    expected_phoenix_commit: str | None = None
    expected_hve_commit: str | None = None
    execution_backend: str = "process"
    oci_copilot_package: Path | None = None
    oci_runtime_base_image: str | None = None
    oci_rust_builder_image: str | None = None
    oci_image_evidence_dir: Path | None = None
    oci_docker: str = "docker"
    tool_compat_shim: bool = True
    work_root: Path | None = None


@dataclass(slots=True)
class _HarnessState:
    name: str
    workspace: Path
    execution: HarnessExecution
    runtime: dict[str, Any]
    attestation: dict[str, Any]
    command_sha256: str | None
    runtime_assets: dict[str, Any] | None
    runner_error: dict[str, str] | None
    oci_evidence_dir: Path | None = None


@dataclass(slots=True)
class _GradeState:
    result: GradeResult | None
    error: dict[str, str] | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _error(exc: BaseException) -> dict[str, str]:
    return {
        "type": type(exc).__name__,
        "message": str(exc).replace("\x00", "")[:4096],
    }


def _safe_component(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("-", value).strip("._-")
    return (cleaned or "task")[:96]


def _confined_relative(value: str, *, field: str) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise TaskTrialRunnerError(f"{field} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise TaskTrialRunnerError(f"{field} is not a confined canonical path")
    return path


def _seal(value: dict[str, Any], *, field: str) -> dict[str, Any]:
    if field in value:
        raise TaskTrialRunnerError(f"payload already contains {field}")
    return {**value, field: sha256_json(value)}


def _verify_seal(value: dict[str, Any], *, field: str, label: str) -> None:
    observed = value.get(field)
    unsigned = {key: item for key, item in value.items() if key != field}
    if not isinstance(observed, str) or sha256_json(unsigned) != observed:
        raise TaskTrialRunnerError(f"{label} {field} is invalid")


def _verify_evidence_descriptor(
    root: Path,
    descriptor: Any,
    *,
    label: str,
) -> bytes:
    if not isinstance(descriptor, dict) or set(descriptor) != {
        "path",
        "sha256",
        "size_bytes",
    }:
        raise TaskTrialRunnerError(f"{label} evidence descriptor is invalid")
    relative = _confined_relative(descriptor["path"], field=f"{label}.path")
    target = root.joinpath(*relative.parts)
    try:
        resolved_root = root.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
        metadata_before = os.lstat(target)
    except (OSError, ValueError) as exc:
        raise TaskTrialRunnerError(f"{label} evidence is unavailable: {exc}") from exc
    if stat.S_ISLNK(metadata_before.st_mode) or not stat.S_ISREG(
        metadata_before.st_mode
    ):
        raise TaskTrialRunnerError(f"{label} evidence is not a regular file")
    expected_size = descriptor["size_bytes"]
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 0
        or expected_size > 64 * 1024 * 1024
    ):
        raise TaskTrialRunnerError(f"{label} evidence size is invalid")
    if metadata_before.st_size != expected_size:
        raise TaskTrialRunnerError(f"{label} evidence size changed")
    try:
        payload = target.read_bytes()
        metadata_after = os.lstat(target)
    except OSError as exc:
        raise TaskTrialRunnerError(
            f"{label} evidence could not be read: {exc}"
        ) from exc
    if (
        metadata_before.st_dev,
        metadata_before.st_ino,
        metadata_before.st_size,
        getattr(metadata_before, "st_mtime_ns", None),
    ) != (
        metadata_after.st_dev,
        metadata_after.st_ino,
        metadata_after.st_size,
        getattr(metadata_after, "st_mtime_ns", None),
    ):
        raise TaskTrialRunnerError(f"{label} evidence changed during verification")
    if len(payload) != expected_size or sha256_bytes(payload) != descriptor["sha256"]:
        raise TaskTrialRunnerError(f"{label} evidence digest does not match")
    return payload


def _verify_attempt_evidence(
    config: RunnerConfig,
    attempt: dict[str, Any],
) -> None:
    evidence = attempt.get("evidence")
    if (
        not isinstance(evidence, dict)
        or evidence.get("committed_before_checkpoint") is not True
    ):
        raise TaskTrialRunnerError("attempt evidence commitment is invalid")
    relative = _confined_relative(
        evidence.get("relative_path"),
        field="attempt.evidence.relative_path",
    )
    root = config.evidence_root.joinpath(*relative.parts)
    attempt_path = root / "attempt.json"
    try:
        stored_attempt = json.loads(attempt_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskTrialRunnerError(
            f"attempt evidence document is unavailable: {exc}"
        ) from exc
    if stored_attempt != attempt:
        raise TaskTrialRunnerError(
            "attempt checkpoint does not match committed attempt evidence"
        )
    for name in HARNESSES:
        receipt = attempt[name]["receipt"]
        execution = receipt.get("execution")
        artifact = receipt.get("artifact")
        if not isinstance(execution, dict) or not isinstance(artifact, dict):
            raise TaskTrialRunnerError(f"{name} evidence receipt is incomplete")
        runtime_payload: bytes | None = None
        for field in ("stdout", "stderr", "diff", "runtime"):
            payload = _verify_evidence_descriptor(
                root,
                execution.get(field),
                label=f"{name}.{field}",
            )
            if field == "runtime":
                runtime_payload = payload
        assert runtime_payload is not None
        try:
            runtime_document = json.loads(runtime_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskTrialRunnerError(
                f"{name} runtime evidence is invalid: {exc}"
            ) from exc
        if not isinstance(runtime_document, dict) or runtime_document.get(
            "model_attestation"
        ) != receipt.get("model_attestation"):
            raise TaskTrialRunnerError(
                f"{name} runtime/model attestation evidence is inconsistent"
            )
        grade_payload = _verify_evidence_descriptor(
            root,
            artifact.get("grade_evidence"),
            label=f"{name}.grade",
        )
        try:
            grade_document = json.loads(grade_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskTrialRunnerError(
                f"{name} grade evidence is invalid: {exc}"
            ) from exc
        row = attempt[name]
        reliability = receipt.get("reliability")
        if not isinstance(reliability, dict):
            raise TaskTrialRunnerError(f"{name} reliability receipt is invalid")
        artifact_valid = artifact.get("valid") is True
        if artifact_valid:
            if (
                not isinstance(grade_document, dict)
                or grade_document.get("schema") != "atv.grade-result/v1"
            ):
                raise TaskTrialRunnerError(
                    f"{name} grade evidence is not a grade result"
                )
            raw_score = grade_document.get("score")
            if (
                isinstance(raw_score, bool)
                or not isinstance(raw_score, (int, float))
                or not 0.0 <= float(raw_score) <= 1.0
            ):
                raise TaskTrialRunnerError(f"{name} raw grade score is invalid")
            if (
                artifact.get("raw_score") != raw_score
                or artifact.get("passed") != grade_document.get("passed")
                or artifact.get("output_tree_digest")
                != grade_document.get("output_tree_digest")
                or artifact.get("grade_result_digest")
                != grade_document.get("result_digest")
                or row.get("artifact_score") != raw_score
                or row.get("passed") != grade_document.get("passed")
            ):
                raise TaskTrialRunnerError(
                    f"{name} grade-derived result fields are inconsistent"
                )
        else:
            if not isinstance(grade_document, dict) or "error" not in grade_document:
                raise TaskTrialRunnerError(
                    f"{name} invalid artifact evidence lacks an error"
                )
            raw_score = None
            if (
                artifact.get("raw_score") is not None
                or row.get("artifact_score") is not None
                or row.get("passed") is not None
            ):
                raise TaskTrialRunnerError(
                    f"{name} invalid artifact fields are inconsistent"
                )
        expected_reliable = bool(
            execution.get("valid") is True
            and receipt.get("model_attestation", {}).get("status") == "pass"
            and artifact_valid
        )
        expected_score = float(raw_score) if expected_reliable else 0.0
        if (
            row.get("reliable") is not expected_reliable
            or float(row.get("score", -1.0)) != expected_score
            or reliability.get("reliable") is not expected_reliable
            or reliability.get("execution_valid")
            is not (execution.get("valid") is True)
            or reliability.get("model_attestation_valid")
            is not (receipt.get("model_attestation", {}).get("status") == "pass")
            or reliability.get("artifact_valid") is not artifact_valid
        ):
            raise TaskTrialRunnerError(
                f"{name} reliability/score fields are inconsistent with evidence"
            )
        manifest_descriptor = artifact.get("artifact_manifest")
        if manifest_descriptor is None:
            manifest = None
        else:
            manifest_payload = _verify_evidence_descriptor(
                root,
                manifest_descriptor,
                label=f"{name}.artifact-manifest",
            )
            try:
                manifest = json.loads(manifest_payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise TaskTrialRunnerError(
                    f"{name} artifact manifest is invalid: {exc}"
                ) from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema") != "atv.phoenix-hve-task-artifact/v1"
                or not isinstance(manifest.get("files"), list)
            ):
                raise TaskTrialRunnerError(f"{name} artifact manifest shape is invalid")
            artifact_root = root / "artifacts" / name
            for index, row in enumerate(manifest["files"]):
                if not isinstance(row, dict):
                    raise TaskTrialRunnerError(
                        f"{name} artifact manifest row {index} is invalid"
                    )
                descriptor = {
                    "path": row.get("path"),
                    "sha256": row.get("sha256"),
                    "size_bytes": row.get("size"),
                }
                _verify_evidence_descriptor(
                    artifact_root,
                    descriptor,
                    label=f"{name}.artifact[{index}]",
                )
        runtime_assets = receipt.get("runtime_assets")
        if not isinstance(runtime_assets, dict):
            continue
        oci_descriptor = runtime_assets.get("oci_evidence_manifest")
        if oci_descriptor is None:
            continue
        oci_payload = _verify_evidence_descriptor(
            root,
            oci_descriptor,
            label=f"{name}.oci-manifest",
        )
        try:
            oci_manifest = json.loads(oci_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskTrialRunnerError(
                f"{name} OCI evidence manifest is invalid: {exc}"
            ) from exc
        if (
            not isinstance(oci_manifest, dict)
            or oci_manifest.get("schema") != "atv.phoenix-hve-oci-evidence-manifest/v1"
            or not isinstance(oci_manifest.get("files"), list)
        ):
            raise TaskTrialRunnerError(f"{name} OCI evidence manifest shape is invalid")
        oci_root = root / "oci" / name
        for index, row in enumerate(oci_manifest["files"]):
            if not isinstance(row, dict):
                raise TaskTrialRunnerError(
                    f"{name} OCI evidence row {index} is invalid"
                )
            _verify_evidence_descriptor(
                oci_root,
                {
                    "path": row.get("path"),
                    "sha256": row.get("sha256"),
                    "size_bytes": row.get("size"),
                },
                label=f"{name}.oci[{index}]",
            )


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_directory(temporary: Path, final: Path) -> None:
    last_error: OSError | None = None
    for attempt in range(DIRECTORY_PUBLISH_ATTEMPTS):
        try:
            os.replace(temporary, final)
            return
        except (PermissionError, FileExistsError) as exc:
            last_error = exc
            if final.exists():
                raise TaskTrialRunnerError(
                    "attempt evidence destination appeared during publish"
                ) from exc
            if attempt + 1 < DIRECTORY_PUBLISH_ATTEMPTS:
                time.sleep(min(0.05 * (attempt + 1), 0.5))
    raise TaskTrialRunnerError(
        "attempt evidence publish remained locked after bounded retries"
    ) from last_error


@contextlib.contextmanager
def _ledger_lock(ledger_dir: Path) -> Iterator[None]:
    lock_path = ledger_dir / ".runner.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _discover_task_roots(roots: Sequence[Path]) -> tuple[Path, ...]:
    found: list[Path] = []
    seen: set[Path] = set()
    for raw_root in roots:
        root = raw_root.resolve()
        if not root.exists():
            raise TaskTrialRunnerError(f"task root does not exist: {root}")
        candidates = (
            [root]
            if (root / "task.json").is_file()
            else sorted(
                child
                for child in root.iterdir()
                if child.is_dir() and (child / "task.json").is_file()
            )
        )
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)
    if not found:
        raise TaskTrialRunnerError("no task packages were discovered")
    return tuple(found)


def _load_selection(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise TaskTrialRunnerError(f"task selection could not be read: {exc}") from exc
    if len(raw) > 4 * 1024 * 1024:
        raise TaskTrialRunnerError("task selection exceeds 4 MiB")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskTrialRunnerError(
            f"task selection is not valid UTF-8 JSON: {exc}"
        ) from exc
    if not isinstance(document, dict) or document.get("schema") != (
        "atv.phoenix-hve-task-selection/v1"
    ):
        raise TaskTrialRunnerError(
            "task selection must use atv.phoenix-hve-task-selection/v1"
        )
    rows = document.get("tasks")
    if not isinstance(rows, list) or not rows:
        raise TaskTrialRunnerError("task selection must contain a non-empty tasks list")
    if len(rows) != 20:
        raise TaskTrialRunnerError(
            "formal task selection must contain exactly 20 tasks"
        )
    expected_fields = {
        "category",
        "official_review_eligible",
        "path",
        "review_level",
        "task_digest",
        "task_id",
    }
    task_ids: list[str] = []
    task_digests: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != expected_fields:
            raise TaskTrialRunnerError(
                f"task selection row {index} has unexpected fields"
            )
        task_id = row.get("task_id")
        task_digest = row.get("task_digest")
        if not isinstance(task_id, str) or not task_id:
            raise TaskTrialRunnerError(
                f"task selection row {index} has an invalid task_id"
            )
        if (
            not isinstance(task_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", task_digest) is None
        ):
            raise TaskTrialRunnerError(
                f"task selection row {index} has an invalid task_digest"
            )
        task_ids.append(task_id)
        task_digests.append(task_digest)
    if len(set(task_ids)) != len(task_ids):
        raise TaskTrialRunnerError("task selection contains duplicate task IDs")
    if len(set(task_digests)) != len(task_digests):
        raise TaskTrialRunnerError("task selection contains duplicate task digests")
    category_counts: dict[str, int] = {}
    for row in rows:
        category = row["category"]
        if not isinstance(category, str) or not category:
            raise TaskTrialRunnerError("task selection contains an invalid category")
        category_counts[category] = category_counts.get(category, 0) + 1
    if len(category_counts) != 5 or set(category_counts.values()) != {4}:
        raise TaskTrialRunnerError(
            "formal task selection must contain four tasks in each of five categories"
        )
    return {
        "path": str(path.resolve()),
        "document": document,
        "digest": sha256_json(document),
    }


def _load_tasks(
    config: RunnerConfig,
    *,
    selection: dict[str, Any] | None = None,
) -> tuple[TaskPackage, ...]:
    try:
        packages = load_task_suite(_discover_task_roots(config.task_roots))
    except (OSError, TaskPackageError) as exc:
        raise TaskTrialRunnerError(f"task suite could not be loaded: {exc}") from exc
    if selection is not None and (config.task_ids or config.categories):
        raise TaskTrialRunnerError(
            "task selection cannot be combined with task-id/category filters"
        )
    selection_rows = selection["document"]["tasks"] if selection is not None else None
    requested_ids = (
        {str(row["task_id"]) for row in selection_rows}
        if selection_rows is not None
        else set(config.task_ids)
    )
    requested_categories = set(config.categories)
    available_ids = {package.id for package in packages}
    available_categories = {package.category for package in packages}
    if missing := sorted(requested_ids - available_ids):
        raise TaskTrialRunnerError(f"unknown task ids: {missing}")
    if missing := sorted(requested_categories - available_categories):
        raise TaskTrialRunnerError(f"unknown task categories: {missing}")
    selected = tuple(
        sorted(
            (
                package
                for package in packages
                if (not requested_ids or package.id in requested_ids)
                and (
                    not requested_categories or package.category in requested_categories
                )
            ),
            key=lambda package: (package.id, package.digest),
        )
    )
    if not selected:
        raise TaskTrialRunnerError("task filters selected no packages")
    if selection_rows is not None:
        by_id = {package.id: package for package in selected}
        if len(by_id) != len(selection_rows):
            raise TaskTrialRunnerError(
                "loaded task count does not match the frozen selection"
            )
        for row in selection_rows:
            package = by_id.get(str(row["task_id"]))
            if package is None:
                raise TaskTrialRunnerError(
                    f"frozen task is unavailable: {row['task_id']}"
                )
            try:
                relative = package.root.resolve().relative_to(REPO_ROOT.resolve())
            except ValueError as exc:
                raise TaskTrialRunnerError(
                    f"selected task is outside the benchmark repository: {package.id}"
                ) from exc
            observed = {
                "category": package.category,
                "official_review_eligible": package.official_review_eligible,
                "path": relative.as_posix(),
                "review_level": package.review_level.value,
                "task_digest": package.digest,
                "task_id": package.id,
            }
            if row != observed:
                raise TaskTrialRunnerError(
                    f"selected task identity changed: {package.id}"
                )
    return selected


def _git_bytes(repo: Path, *args: str) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", errors="replace")[:2048]
        raise TaskTrialRunnerError(f"git {' '.join(args)} failed: {detail}")
    return process.stdout


def _source_identity(repo: Path, *, repository: str) -> dict[str, Any]:
    status = _git_bytes(
        repo,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    )
    diff = _git_bytes(repo, "diff", "--binary", "HEAD", "--")
    remotes = tuple(
        line.strip()
        for line in _git_bytes(repo, "remote", "get-url", "--all", "origin")
        .decode("utf-8", errors="strict")
        .splitlines()
        if line.strip()
    )
    return {
        "repository": repository,
        "commit": git_commit(repo),
        "git_tree": git_tree(repo),
        "tracked_tree_listing_sha256": tracked_tree_listing_sha256(repo),
        "worktree_status_sha256": sha256_bytes(status),
        "tracked_diff_sha256": sha256_bytes(diff),
        "dirty": bool(status),
        "origin_urls": list(remotes),
    }


def _validate_source_cell(
    config: RunnerConfig,
    sources: dict[str, Any],
) -> None:
    expected = {
        "atv_phoenix": (
            config.expected_phoenix_commit,
            "github.com/All-The-Vibes/ATV-Phoenix",
        ),
        "hve_core": (
            config.expected_hve_commit,
            "github.com/microsoft/hve-core",
        ),
    }
    for name, (expected_commit, expected_remote) in expected.items():
        source = sources[name]
        if source.get("dirty") is not False:
            raise TaskTrialRunnerError(f"{name} source checkout is not clean")
        if expected_commit is not None and source.get("commit") != expected_commit:
            raise TaskTrialRunnerError(
                f"{name} source commit does not match the frozen commit"
            )
        if expected_commit is not None:
            remotes = source.get("origin_urls")
            if not isinstance(remotes, list) or not any(
                expected_remote.casefold() in str(remote).casefold()
                for remote in remotes
            ):
                raise TaskTrialRunnerError(
                    f"{name} origin does not match the frozen repository"
                )


def _build_oci_images(config: RunnerConfig) -> dict[str, OciImage]:
    if (
        config.expected_phoenix_commit is None
        or config.expected_hve_commit is None
        or config.oci_copilot_package is None
        or config.oci_runtime_base_image is None
        or config.oci_rust_builder_image is None
        or config.oci_image_evidence_dir is None
    ):
        raise TaskTrialRunnerError(
            "OCI execution requires pinned commits, Copilot package, base images, "
            "and an image-evidence directory"
        )
    try:
        return build_or_reuse_images(
            OciBuildConfig(
                phoenix_repo=config.phoenix_repo,
                phoenix_commit=config.expected_phoenix_commit,
                hve_repo=config.hve_repo,
                hve_commit=config.expected_hve_commit,
                copilot_package=config.oci_copilot_package,
                runtime_base_image=config.oci_runtime_base_image,
                rust_builder_image=config.oci_rust_builder_image,
                evidence_dir=config.oci_image_evidence_dir,
                docker=config.oci_docker,
                image_namespace="atv-bench/phoenix-hve-task-v1",
                tool_compat_shim=config.tool_compat_shim,
            )
        )
    except OciRuntimeError as exc:
        raise TaskTrialRunnerError(f"OCI image preparation failed: {exc}") from exc


def _copilot_runtime_identity() -> tuple[str, str, dict[str, Any]]:
    node, loader = _copilot_argv()
    lines = _run_text(node, loader, "--version").splitlines()
    if not lines:
        raise TaskTrialRunnerError("Copilot CLI returned no version")
    return (
        node,
        loader,
        {
            "copilot_cli": lines[0],
            "node": _run_text(node, "--version"),
            "loader_sha256": sha256_file(loader),
        },
    )


def _code_identity() -> dict[str, str]:
    paths = {
        "runner": Path(__file__),
        "harness_preparation": REPO_ROOT / "scripts" / "compare_phoenix_hve.py",
        "jsonl_attestation": REPO_ROOT / "src" / "atv_bench" / "comparison.py",
        "task_loader": REPO_ROOT / "src" / "atv_bench" / "eval" / "tasks.py",
        "hidden_grader": REPO_ROOT / "src" / "atv_bench" / "eval" / "grader.py",
        "task_analyzer": REPO_ROOT / "scripts" / "summarize_phoenix_hve_tasks.py",
        "oci_runtime": REPO_ROOT / "scripts" / "phoenix_hve_oci_runtime.py",
    }
    return {name: sha256_file(path) for name, path in paths.items()}


def _task_identity(package: TaskPackage) -> dict[str, Any]:
    manifest = package.manifest
    return {
        "task_id": package.id,
        "category": package.category,
        "task_digest": package.digest,
        "task_version": package.version,
        "prompt_sha256": manifest["prompt"]["digest"]["value"],
        "workspace_tree_digest": manifest["source"]["tree_digest"]["value"],
    }


def _experiment(
    config: RunnerConfig,
    packages: Sequence[TaskPackage],
    *,
    sources: dict[str, Any],
    runtime: dict[str, Any],
    selection: dict[str, Any] | None,
    schedule: Sequence[dict[str, Any]],
    oci_images: Mapping[str, OciImage] | None,
) -> dict[str, Any]:
    descriptor = {
        "schema": EXPERIMENT_SCHEMA,
        "rankable": False,
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "tasks": [_task_identity(package) for package in packages],
        "analysis_policy": dict(FORMAL_ANALYSIS_POLICY),
        "selection": (
            {
                "schema": selection["document"]["schema"],
                "sha256": selection["digest"],
                "task_count": len(selection["document"]["tasks"]),
            }
            if selection is not None
            else None
        ),
        "model": {
            "requested": config.model,
            "selection_source": "explicit_cli",
            "provider_signed": False,
        },
        "budget": {
            "max_ai_credits": config.max_ai_credits,
            "timeout_seconds": config.timeout_seconds,
            "same_for_both_harnesses": True,
        },
        "randomization": {
            "schedule_algorithm": "category-interleaved-balanced-v1",
            "harness_order_algorithm": "per-task-alternating-balanced-v1",
            "seed": config.randomization_seed,
            "schedule": list(schedule),
        },
        "tool_compatibility_shim": config.tool_compat_shim,
        "execution_backend": config.execution_backend,
        "oci_images": (
            {name: image.evidence() for name, image in sorted(oci_images.items())}
            if oci_images is not None
            else None
        ),
        "sources": sources,
        "runtime": {
            **runtime,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "code": _code_identity(),
        "methodology": {
            "fresh_workspace_per_harness_attempt": True,
            "fresh_copilot_home_per_harness_attempt": True,
            "hidden_grader_loaded_after_both_harnesses_exit": True,
            "exact_jsonl_model_attestation": True,
            "unreliable_attempt_score_for_analysis": 0.0,
            "infrastructure_invalid_attempts_make_the_task_ineligible": True,
            "task_is_the_inferential_cluster": True,
            "separate_oci_container_per_harness": (config.execution_backend == "oci"),
        },
    }
    return {
        "schema": EXPERIMENT_SCHEMA,
        "experiment_digest": sha256_json(descriptor),
        "descriptor": descriptor,
    }


def _ensure_experiment_file(root: Path, experiment: dict[str, Any]) -> None:
    path = root / "experiment.json"
    root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            observed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TaskTrialRunnerError(
                f"existing experiment file is unreadable: {exc}"
            ) from exc
        if observed != experiment:
            raise TaskTrialRunnerError(
                "evidence root belongs to a different experiment configuration"
            )
        return
    _atomic_write_json(path, experiment)


def _read_json_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskTrialRunnerError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise TaskTrialRunnerError(f"{label} must contain a JSON object")
    return value


def _validate_formal_preregistration(
    config: RunnerConfig,
    *,
    experiment: dict[str, Any],
    packages: Sequence[TaskPackage],
    selection: dict[str, Any],
    schedule: Sequence[dict[str, Any]],
    oci_images: Mapping[str, OciImage],
) -> str:
    if (
        config.preregistration_file is None
        or config.calibration_plan_file is None
        or config.calibration_summary_file is None
    ):
        raise TaskTrialRunnerError(
            "formal execution requires preregistration, calibration plan, and "
            "calibration summary files"
        )
    preregistration = _read_json_object(
        config.preregistration_file,
        label="formal preregistration",
    )
    if preregistration.get("schema") != ("atv.phoenix-hve-task-preregistration/v1"):
        raise TaskTrialRunnerError("formal preregistration schema does not match")
    _verify_seal(
        preregistration,
        field="preregistration_sha256",
        label="formal preregistration",
    )
    preregistration_sha256 = str(preregistration["preregistration_sha256"])
    descriptor = experiment["descriptor"]
    expected_tasks = [
        {
            "task_id": package.id,
            "category": package.category,
            "task_digest": package.digest,
        }
        for package in packages
    ]
    expected_images = {
        name: {
            "image_id": image.image_id,
            "build_spec_sha256": image.build_spec_sha256,
            "proxy_image_id": image.proxy.image_id,
            "proxy_build_spec_sha256": image.proxy.build_spec_sha256,
        }
        for name, image in sorted(oci_images.items())
    }
    expected_sources = {
        "phoenix_commit": config.expected_phoenix_commit,
        "hve_commit": config.expected_hve_commit,
    }
    expected_static = {
        "experiment_digest": experiment["experiment_digest"],
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "model": descriptor["model"],
        "budget": descriptor["budget"],
        "analysis_policy": FORMAL_ANALYSIS_POLICY,
        "tasks": expected_tasks,
        "selection_sha256": selection["digest"],
        "schedule_sha256": sha256_json(list(schedule)),
        "execution_backend": "oci",
        "source_commits": expected_sources,
        "oci_images": expected_images,
    }
    for field, expected in expected_static.items():
        if preregistration.get(field) != expected:
            raise TaskTrialRunnerError(
                f"formal preregistration {field} does not match the frozen cell"
            )

    calibration_plan = _read_json_object(
        config.calibration_plan_file,
        label="calibration plan",
    )
    plan_descriptor = calibration_plan.get("descriptor")
    plan_digest = calibration_plan.get("calibration_plan_digest")
    if (
        calibration_plan.get("schema") != "atv.phoenix-hve-task-calibration-plan/v1"
        or not isinstance(plan_descriptor, dict)
        or not isinstance(plan_digest, str)
        or sha256_json(plan_descriptor) != plan_digest
    ):
        raise TaskTrialRunnerError("calibration plan seal is invalid")
    if plan_descriptor.get("execution_backend") != "oci":
        raise TaskTrialRunnerError("calibration did not use the OCI backend")
    if plan_descriptor.get("model") != descriptor["model"]:
        raise TaskTrialRunnerError("calibration model does not match evaluation")
    plan_images = plan_descriptor.get("oci_images")
    if not isinstance(plan_images, dict):
        raise TaskTrialRunnerError("calibration plan lacks OCI image evidence")
    for name, expected in expected_images.items():
        observed = plan_images.get(name)
        if (
            not isinstance(observed, dict)
            or {
                "image_id": observed.get("image_id"),
                "build_spec_sha256": observed.get("build_spec_sha256"),
                "proxy_image_id": observed.get("proxy", {}).get("image_id"),
                "proxy_build_spec_sha256": observed.get("proxy", {}).get(
                    "build_spec_sha256"
                ),
            }
            != expected
        ):
            raise TaskTrialRunnerError(
                f"calibration OCI image does not match evaluation for {name}"
            )

    calibration_summary = _read_json_object(
        config.calibration_summary_file,
        label="calibration summary",
    )
    _verify_seal(
        calibration_summary,
        field="summary_sha256",
        label="calibration summary",
    )
    if calibration_summary.get("calibration_plan_digest") != plan_digest:
        raise TaskTrialRunnerError(
            "calibration summary does not belong to the supplied plan"
        )
    if (
        calibration_summary.get("decision") != "selected"
        or calibration_summary.get("selected_max_ai_credits") != config.max_ai_credits
        or calibration_summary.get("model", {}).get("requested") != config.model
    ):
        raise TaskTrialRunnerError(
            "calibration summary does not select the evaluation budget/model"
        )
    expected_calibration = {
        "plan_digest": plan_digest,
        "summary_sha256": calibration_summary["summary_sha256"],
        "selected_max_ai_credits": config.max_ai_credits,
    }
    if preregistration.get("calibration") != expected_calibration:
        raise TaskTrialRunnerError(
            "formal preregistration calibration binding does not match"
        )
    return preregistration_sha256


def _fallback_randomized_order(
    seed: int,
    task_digest: str,
    repetition: int,
) -> tuple[tuple[str, str], str]:
    key = canonical_json_bytes(
        {
            "seed": seed,
            "task_digest": task_digest,
            "repetition": repetition,
        }
    )
    digest = hashlib.sha256(key).digest()
    order = HARNESSES if digest[0] % 2 == 0 else tuple(reversed(HARNESSES))
    return (order[0], order[1]), digest.hex()


def _schedule_plan(
    packages: Sequence[TaskPackage],
    *,
    seed: int,
) -> tuple[dict[str, Any], ...]:
    if not packages:
        raise TaskTrialRunnerError("cannot schedule an empty task portfolio")
    grouped: dict[str, list[TaskPackage]] = {}
    for package in packages:
        grouped.setdefault(package.category, []).append(package)
    category_sizes = {category: len(rows) for category, rows in grouped.items()}
    if len(set(category_sizes.values())) != 1:
        raise TaskTrialRunnerError(
            "balanced task scheduling requires equal task counts per category"
        )
    category_order = sorted(
        grouped,
        key=lambda category: sha256_json(
            {
                "algorithm": "category-order-v1",
                "seed": seed,
                "category": category,
            }
        ),
    )
    ordered_by_category = {
        category: sorted(
            rows,
            key=lambda package: sha256_json(
                {
                    "algorithm": "task-order-v1",
                    "seed": seed,
                    "task_digest": package.digest,
                }
            ),
        )
        for category, rows in grouped.items()
    }
    base_first: dict[str, str] = {}
    for category in category_order:
        balance_rank = sorted(
            ordered_by_category[category],
            key=lambda package: sha256_json(
                {
                    "algorithm": "category-first-harness-balance-v1",
                    "seed": seed,
                    "category": category,
                    "task_digest": package.digest,
                }
            ),
        )
        phoenix_base_count = len(balance_rank) // 2
        for index, package in enumerate(balance_rank):
            base_first[package.digest] = (
                "phoenix" if index < phoenix_base_count else "hve"
            )
    entries: list[dict[str, Any]] = []
    per_category = next(iter(category_sizes.values()))
    for repetition in range(ATTEMPTS_PER_TASK):
        rotated_categories = (
            category_order[repetition % len(category_order) :]
            + category_order[: repetition % len(category_order)]
        )
        for slot in range(per_category):
            for category in rotated_categories:
                rows = ordered_by_category[category]
                package = rows[(slot + repetition) % len(rows)]
                first = base_first[package.digest]
                if repetition % 2:
                    first = "hve" if first == "phoenix" else "phoenix"
                second = "hve" if first == "phoenix" else "phoenix"
                randomization_key = sha256_json(
                    {
                        "algorithm": "per-task-alternating-balanced-v1",
                        "seed": seed,
                        "task_digest": package.digest,
                        "repetition": repetition,
                        "base_first": base_first[package.digest],
                    }
                )
                entries.append(
                    {
                        "schedule_index": len(entries),
                        "task_id": package.id,
                        "task_digest": package.digest,
                        "category": package.category,
                        "repetition": repetition,
                        "randomized_order": [first, second],
                        "randomization_key_sha256": randomization_key,
                    }
                )
    expected_cells = len(packages) * ATTEMPTS_PER_TASK
    if len(entries) != expected_cells:
        raise TaskTrialRunnerError("balanced schedule has the wrong cell count")
    keys = {(row["task_digest"], row["repetition"]) for row in entries}
    if len(keys) != expected_cells:
        raise TaskTrialRunnerError("balanced schedule contains duplicate cells")
    phoenix_first = sum(row["randomized_order"][0] == "phoenix" for row in entries)
    hve_first = len(entries) - phoenix_first
    if len(packages) % 2 == 0 and phoenix_first != hve_first:
        raise TaskTrialRunnerError("balanced schedule is not globally 50/50")
    for category in category_order:
        category_packages = grouped[category]
        category_phoenix_base = sum(
            base_first[package.digest] == "phoenix" for package in category_packages
        )
        category_hve_base = len(category_packages) - category_phoenix_base
        if len(category_packages) % 2 == 0 and (
            category_phoenix_base != category_hve_base
        ):
            raise TaskTrialRunnerError(
                f"balanced schedule is not 50/50 within category {category}"
            )
    return tuple(entries)


def _schedule_lookup(
    schedule: Sequence[dict[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    return {(str(row["task_digest"]), int(row["repetition"])): row for row in schedule}


def _attempt_id(experiment_digest: str, package: TaskPackage, repetition: int) -> str:
    return sha256_json(
        {
            "experiment_digest": experiment_digest,
            "task_digest": package.digest,
            "repetition": repetition,
        }
    )


def _task_document_path(config: RunnerConfig, package: TaskPackage) -> Path:
    return (
        config.ledger_dir / f"{_safe_component(package.id)}-{package.digest[:12]}.json"
    )


def _attempt_evidence_relative(
    package: TaskPackage,
    repetition: int,
    attempt_id: str,
) -> str:
    return (
        f"{_safe_component(package.id)}-{package.digest[:12]}/"
        f"attempt-{repetition}-{attempt_id[:12]}"
    )


def _task_goal(package: TaskPackage) -> str:
    return (
        package.prompt_path.read_text(encoding="utf-8").rstrip()
        + "\n\nATV-Bench local task conditions:\n"
        "- Treat the current repository as the complete public task workspace.\n"
        "- Do not inspect the benchmark package or hidden grader inputs.\n"
        "- Do not use network access.\n"
        "- Leave the completed artifact in the current workspace and exit.\n"
    )


def _initialize_workspace(source: Path, destination: Path) -> None:
    if destination.exists():
        raise TaskTrialRunnerError(f"fresh workspace already exists: {destination}")
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    commands = (
        ["git", "init", "-q"],
        ["git", "config", "core.autocrlf", "false"],
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=atv@bench.local",
            "-c",
            "user.name=ATV Bench",
            "commit",
            "-qm",
            "task seed",
        ],
    )
    for command in commands:
        process = subprocess.run(
            command,
            cwd=destination,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace")[:2048]
            raise TaskTrialRunnerError(
                f"could not initialize fresh task workspace: {detail}"
            )


def _remove_git_metadata(workspace: Path) -> None:
    root = workspace.resolve()
    target = workspace / ".git"
    if target.parent.resolve() != root or target.name != ".git":
        raise TaskTrialRunnerError("refusing to remove unconfined Git metadata")
    if target.is_dir():

        def remove_readonly(function, path, _error_info):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(target, onerror=remove_readonly)
    elif target.exists():
        target.unlink()


def _prepared_assets(name: str, prepared: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "copilot_home_tree_digest": tree_digest(Path(prepared["copilot_home"])),
        "tool_compatibility_shim": prepared.get("tool_compatibility_shim"),
    }
    if name == "phoenix":
        result["phoenix_mcp_sha256"] = sha256_file(Path(prepared["binary"]))
    else:
        result["materialized_plugin_tree_digest"] = tree_digest(
            Path(prepared["plugin"])
        )
        result["materialized_pointer_files"] = int(prepared["resolved_pointers"])
    return result


def _synthetic_execution(
    status: str,
    started: float,
    error: dict[str, str],
) -> HarnessExecution:
    return HarnessExecution(
        status=status,
        exit_code=None,
        duration_seconds=time.monotonic() - started,
        stdout=b"",
        stderr=canonical_json_bytes({"status": status, "error": error}) + b"\n",
        diff="",
    )


def _execute(
    name: str,
    *,
    config: RunnerConfig,
    workspace: Path,
    runtime_root: Path,
    disabled_skills: list[str],
    node: str,
    loader: str,
    token: str,
    goal: str,
) -> _HarnessState:
    started = time.monotonic()
    command: list[str] | None = None
    runner_error: dict[str, str] | None = None
    runtime_assets: dict[str, Any] | None = None
    try:
        if name == "phoenix":
            prepared = _prepare_phoenix(
                config.phoenix_repo,
                runtime_root,
                disabled_skills,
                tool_compat_shim=config.tool_compat_shim,
            )
            agent = "phoenix"
            plugin = None
        else:
            prepared = _prepare_hve(
                config.hve_repo,
                runtime_root,
                disabled_skills,
                tool_compat_shim=config.tool_compat_shim,
            )
            agent = "hve-core:rpi-agent"
            plugin = Path(prepared["plugin"])
        if config.tool_compat_shim and prepared.get("tool_compatibility_shim") is None:
            raise TaskTrialRunnerError(
                f"{name} did not receive the requested compatibility shim"
            )
        command = _command(
            node=node,
            loader=loader,
            model=config.model,
            credits=config.max_ai_credits,
            agent=agent,
            goal=goal,
            plugin=plugin,
        )
        environment = _common_env(
            copilot_home=Path(prepared["copilot_home"]),
            user_home=Path(prepared["user_home"]),
            token=token,
        )
        execution = _run_harness(
            command,
            workspace=workspace,
            env=environment,
            timeout_seconds=config.timeout_seconds,
        )
        runtime_assets = _prepared_assets(name, prepared)
    except BaseException as exc:
        runner_error = _error(exc)
        execution = _synthetic_execution(
            "preparation_error" if command is None else "runner_error",
            started,
            runner_error,
        )
    runtime = parse_copilot_jsonl(execution.stdout)
    attestation = _model_attestation(runtime, requested_model=config.model)
    runtime["model_attestation"] = attestation
    return _HarnessState(
        name=name,
        workspace=workspace,
        execution=execution,
        runtime=runtime,
        attestation=attestation,
        command_sha256=sha256_json(command) if command is not None else None,
        runtime_assets=runtime_assets,
        runner_error=runner_error,
        oci_evidence_dir=None,
    )


def _execute_oci(
    name: str,
    *,
    config: RunnerConfig,
    workspace: Path,
    runtime_root: Path,
    token: str,
    goal: str,
    image: OciImage,
    forbidden_roots: tuple[Path, ...],
    run_id: str,
) -> _HarnessState:
    started = time.monotonic()
    evidence_dir = runtime_root / "oci-evidence"
    command_identity = {
        "backend": "oci",
        "image_id": image.image_id,
        "image_build_spec_sha256": image.build_spec_sha256,
        "harness": name,
        "model": config.model,
        "max_ai_credits": config.max_ai_credits,
        "timeout_seconds": config.timeout_seconds,
        "goal_sha256": sha256_bytes(goal.encode("utf-8")),
    }
    runner_error: dict[str, str] | None = None
    try:
        execution = run_oci_harness(
            OciRunConfig(
                docker=config.oci_docker,
                image=image,
                harness=name,
                workspace=workspace,
                evidence_dir=evidence_dir,
                run_id=run_id,
                model=config.model,
                max_ai_credits=config.max_ai_credits,
                timeout_seconds=config.timeout_seconds,
                limits=ResourceLimits(),
                forbidden_roots=forbidden_roots,
            ),
            goal=goal,
            token=token,
        )
    except BaseException as exc:
        runner_error = _error(exc)
        execution = _synthetic_execution(
            "oci_runner_error",
            started,
            runner_error,
        )
    runtime = parse_copilot_jsonl(execution.stdout)
    attestation = _model_attestation(runtime, requested_model=config.model)
    runtime["model_attestation"] = attestation
    return _HarnessState(
        name=name,
        workspace=workspace,
        execution=execution,
        runtime=runtime,
        attestation=attestation,
        command_sha256=sha256_json(command_identity),
        runtime_assets={
            "execution_backend": "oci",
            "image": image.evidence(),
            "network": {
                "mode": "internal-connect-proxy",
                "endpoint_allowlisted": True,
                "allowlist": image.proxy.evidence()["allowlist"],
                "explicit_denylist": image.proxy.evidence()["explicit_denylist"],
            },
        },
        runner_error=runner_error,
        oci_evidence_dir=evidence_dir if evidence_dir.is_dir() else None,
    )


def _load_hidden_grader(package: TaskPackage) -> FileAssertionsGrader:
    return FileAssertionsGrader.from_task(package)


def _grade_after_both_exits(
    package: TaskPackage,
    states: dict[str, _HarnessState],
    *,
    attempt_id: str,
) -> dict[str, _GradeState]:
    for state in states.values():
        _remove_git_metadata(state.workspace)
    try:
        grader = _load_hidden_grader(package)
    except BaseException as exc:
        raise TaskTrialRunnerError(f"hidden grader could not load: {exc}") from exc
    grades: dict[str, _GradeState] = {}
    for name in HARNESSES:
        try:
            result = grader.grade(
                package,
                states[name].workspace,
                lifecycle_receipt=ControllerAssertedLifecycleReceipt.completed(
                    controller_id=f"phoenix-hve-task:{attempt_id}:{name}"
                ),
            )
            grades[name] = _GradeState(result=result, error=None)
        except BaseException as exc:
            grades[name] = _GradeState(result=None, error=_error(exc))
    return grades


def _output_limits(package: TaskPackage) -> TreeLimits:
    output = package.manifest["output"]
    total = int(output["max_total_bytes"])
    return TreeLimits(
        max_files=int(output["max_files"]),
        max_total_bytes=total,
        max_file_bytes=min(total, DEFAULT_MAX_FILE_BYTES),
    )


def _write_evidence_file(
    root: Path,
    relative: str,
    payload: bytes,
) -> dict[str, Any]:
    pure = _confined_relative(relative, field="evidence path")
    target = root.joinpath(*pure.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return {
        "path": pure.as_posix(),
        "sha256": sha256_bytes(payload),
        "size_bytes": len(payload),
    }


def _write_evidence_json(
    root: Path,
    relative: str,
    value: Any,
) -> dict[str, Any]:
    payload = (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )
    return _write_evidence_file(root, relative, payload)


def _preserve_artifact(
    root: Path,
    name: str,
    package: TaskPackage,
    workspace: Path,
    grade: GradeResult,
) -> dict[str, Any]:
    snapshots = snapshot_regular_tree(workspace, limits=_output_limits(package))
    digest = tree_digest_from_snapshots(snapshots)
    if digest != grade.output_tree_digest:
        raise TaskTrialRunnerError(
            f"{name} artifact changed between grading and evidence capture"
        )
    rows = []
    for snapshot in snapshots:
        _write_evidence_file(
            root,
            f"artifacts/{name}/{snapshot.path}",
            snapshot.data,
        )
        rows.append(snapshot.manifest_entry())
    return _write_evidence_json(
        root,
        f"artifacts/{name}.manifest.json",
        {
            "schema": "atv.phoenix-hve-task-artifact/v1",
            "tree_digest": digest,
            "files": rows,
        },
    )


def _preserve_oci_evidence(
    root: Path,
    name: str,
    source: Path,
) -> dict[str, Any]:
    snapshots = snapshot_regular_tree(
        source,
        limits=TreeLimits(
            max_files=256,
            max_total_bytes=32 * 1024 * 1024,
            max_file_bytes=16 * 1024 * 1024,
        ),
    )
    rows: list[dict[str, Any]] = []
    for snapshot in snapshots:
        _write_evidence_file(
            root,
            f"oci/{name}/{snapshot.path}",
            snapshot.data,
        )
        rows.append(snapshot.manifest_entry())
    return _write_evidence_json(
        root,
        f"oci/{name}.manifest.json",
        {
            "schema": "atv.phoenix-hve-oci-evidence-manifest/v1",
            "tree_digest": tree_digest_from_snapshots(snapshots),
            "files": rows,
        },
    )


def _execution_valid(state: _HarnessState) -> bool:
    return bool(
        state.runner_error is None
        and state.execution.exit_code == 0
        and state.execution.status in {"ok", "no_edit"}
        and state.runtime.get("terminal_success") is True
    )


def _write_harness_result(
    root: Path,
    state: _HarnessState,
    grade: _GradeState,
    package: TaskPackage,
    *,
    order_index: int,
) -> dict[str, Any]:
    name = state.name
    stdout = _write_evidence_file(
        root,
        f"raw/{name}.stdout.bin",
        state.execution.stdout,
    )
    stderr = _write_evidence_file(
        root,
        f"raw/{name}.stderr.bin",
        state.execution.stderr,
    )
    diff = _write_evidence_file(
        root,
        f"diffs/{name}.patch",
        state.execution.diff.encode("utf-8"),
    )
    runtime = _write_evidence_json(
        root,
        f"runtime/{name}.json",
        state.runtime,
    )
    artifact_valid = grade.result is not None
    if grade.result is not None:
        grade_evidence = _write_evidence_json(
            root,
            f"grades/{name}.json",
            grade.result.to_dict(),
        )
        artifact_manifest = _preserve_artifact(
            root,
            name,
            package,
            state.workspace,
            grade.result,
        )
        raw_score = grade.result.score
        passed = grade.result.passed
        output_digest = grade.result.output_tree_digest
        grade_digest = grade.result.result_digest
    else:
        grade_evidence = _write_evidence_json(
            root,
            f"grades/{name}.error.json",
            {"error": grade.error},
        )
        artifact_manifest = None
        raw_score = None
        passed = None
        output_digest = None
        grade_digest = None

    execution_valid = _execution_valid(state)
    attestation_valid = state.attestation["status"] == "pass"
    reliable = execution_valid and attestation_valid and artifact_valid
    runtime_assets = dict(state.runtime_assets or {})
    if state.oci_evidence_dir is not None:
        runtime_assets["oci_evidence_manifest"] = _preserve_oci_evidence(
            root,
            name,
            state.oci_evidence_dir,
        )
    if state.runner_error is not None:
        classification = "runner-error"
    elif not execution_valid:
        classification = "execution-invalid"
    elif not attestation_valid:
        classification = "model-attestation-invalid"
    elif not artifact_valid:
        classification = "artifact-invalid"
    else:
        classification = "reliable-completion"
    receipt = _seal(
        {
            "order_index": order_index,
            "execution": {
                "status": state.execution.status,
                "exit_code": state.execution.exit_code,
                "duration_seconds": round(state.execution.duration_seconds, 6),
                "valid": execution_valid,
                "terminal_success": state.runtime.get("terminal_success") is True,
                "command_sha256": state.command_sha256,
                "stdout": stdout,
                "stderr": stderr,
                "diff": diff,
                "runtime": runtime,
                "runner_error": state.runner_error,
            },
            "model_attestation": state.attestation,
            "artifact": {
                "valid": artifact_valid,
                "raw_score": raw_score,
                "passed": passed,
                "output_tree_digest": output_digest,
                "grade_result_digest": grade_digest,
                "grade_evidence": grade_evidence,
                "artifact_manifest": artifact_manifest,
                "grade_error": grade.error,
            },
            "reliability": {
                "reliable": reliable,
                "classification": classification,
                "execution_valid": execution_valid,
                "model_attestation_valid": attestation_valid,
                "artifact_valid": artifact_valid,
            },
            "runtime_assets": runtime_assets or None,
            "reported_usage": state.runtime.get("result", {}).get("usage", {}),
        },
        field="receipt_sha256",
    )
    return {
        # The downstream summarizer requires unreliable scores to be zero.
        "score": float(raw_score) if reliable and raw_score is not None else 0.0,
        "reliable": reliable,
        "artifact_score": raw_score,
        "passed": passed,
        "receipt": receipt,
    }


def _run_paired_attempt(
    config: RunnerConfig,
    package: TaskPackage,
    repetition: int,
    *,
    experiment_digest: str,
    node: str,
    loader: str,
    token: str,
    disabled_skills: list[str],
    planned_order: tuple[str, str] | None = None,
    planned_randomization_key: str | None = None,
    schedule_index: int | None = None,
    oci_images: Mapping[str, OciImage] | None = None,
) -> dict[str, Any]:
    attempt_id = _attempt_id(experiment_digest, package, repetition)
    if planned_order is None:
        order, randomization_key = _fallback_randomized_order(
            config.randomization_seed,
            package.digest,
            repetition,
        )
    else:
        if sorted(planned_order) != sorted(HARNESSES):
            raise TaskTrialRunnerError("planned harness order is invalid")
        if not isinstance(planned_randomization_key, str):
            raise TaskTrialRunnerError("planned randomization key is missing")
        order = planned_order
        randomization_key = planned_randomization_key
    base_work = config.work_root
    if base_work is not None:
        base_work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"atv-task-{attempt_id[:12]}-",
        dir=base_work,
    ) as temporary:
        work = Path(temporary)
        workspaces = {name: work / name / "workspace" for name in HARNESSES}
        for workspace in workspaces.values():
            _initialize_workspace(package.public_workspace, workspace)
        goal = _task_goal(package)
        states: dict[str, _HarnessState] = {}
        for name in order:
            if config.execution_backend == "oci":
                if oci_images is None or name not in oci_images:
                    raise TaskTrialRunnerError(
                        f"verified OCI image is missing for {name}"
                    )
                other = "hve" if name == "phoenix" else "phoenix"
                states[name] = _execute_oci(
                    name,
                    config=config,
                    workspace=workspaces[name],
                    runtime_root=work / name / "runtime",
                    token=token,
                    goal=goal,
                    image=oci_images[name],
                    forbidden_roots=(
                        workspaces[other],
                        package.root,
                        config.ledger_dir,
                        config.evidence_root,
                    ),
                    run_id=f"{attempt_id}:{name}",
                )
            else:
                states[name] = _execute(
                    name,
                    config=config,
                    workspace=workspaces[name],
                    runtime_root=work / name / "runtime",
                    disabled_skills=disabled_skills,
                    node=node,
                    loader=loader,
                    token=token,
                    goal=goal,
                )

        if config.execution_backend == "oci":
            failed = [
                name for name, state in states.items() if state.runner_error is not None
            ]
            if failed:
                raise TaskTrialRunnerError(
                    "OCI execution did not complete with verified cleanup for "
                    f"{failed}; hidden grader was not loaded"
                )

        # Both execution calls have returned before the hidden grader is loaded.
        grades = _grade_after_both_exits(
            package,
            states,
            attempt_id=attempt_id,
        )

        relative = _attempt_evidence_relative(package, repetition, attempt_id)
        final_root = config.evidence_root.joinpath(
            *_confined_relative(relative, field="attempt evidence").parts
        )
        if final_root.exists():
            raise TaskTrialRunnerError(
                f"attempt evidence already exists without a checkpoint: {final_root}"
            )
        staging_parent = config.evidence_root / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f"{attempt_id[:12]}-", dir=staging_parent))
        committed = False
        try:
            harness_rows = {
                name: _write_harness_result(
                    stage,
                    states[name],
                    grades[name],
                    package,
                    order_index=order.index(name),
                )
                for name in HARNESSES
            }
            infrastructure_reasons: list[str] = []
            for name in HARNESSES:
                state = states[name]
                if state.runner_error is not None:
                    infrastructure_reasons.append(f"{name}:runner-error")
                if state.attestation["status"] != "pass":
                    infrastructure_reasons.append(
                        f"{name}:model-attestation-{state.attestation['status']}"
                    )
            infrastructure_valid = not infrastructure_reasons
            attempt = _seal(
                {
                    "attempt_id": attempt_id,
                    "repetition": repetition,
                    "schedule_index": schedule_index,
                    "infrastructure_valid": infrastructure_valid,
                    "infrastructure_reasons": infrastructure_reasons,
                    "randomized_order": list(order),
                    "randomization_key_sha256": randomization_key,
                    "model": {
                        "requested": config.model,
                        "selection_source": "explicit_cli",
                    },
                    "budget": {
                        "max_ai_credits": config.max_ai_credits,
                        "timeout_seconds": config.timeout_seconds,
                    },
                    "phoenix": harness_rows["phoenix"],
                    "hve": harness_rows["hve"],
                    "paired_score_difference_phoenix_minus_hve": round(
                        harness_rows["phoenix"]["score"] - harness_rows["hve"]["score"],
                        12,
                    ),
                    "evidence": {
                        "relative_path": relative,
                        "committed_before_checkpoint": True,
                    },
                },
                field="attempt_sha256",
            )
            _atomic_write_json(stage / "attempt.json", attempt)
            final_root.parent.mkdir(parents=True, exist_ok=True)
            _publish_directory(stage, final_root)
            committed = True
            return attempt
        finally:
            if not committed:
                resolved = stage.resolve()
                parent = staging_parent.resolve()
                try:
                    resolved.relative_to(parent)
                except ValueError as exc:
                    raise TaskTrialRunnerError(
                        "refusing to clean an unconfined staging directory"
                    ) from exc
                shutil.rmtree(resolved, ignore_errors=True)


def _initial_document(
    config: RunnerConfig,
    package: TaskPackage,
    *,
    experiment_digest: str,
    preregistration_sha256: str | None = None,
) -> dict[str, Any]:
    identity = _task_identity(package)
    unsigned = {
        "schema": TRIAL_SCHEMA,
        "task_id": identity["task_id"],
        "category": identity["category"],
        "task_digest": identity["task_digest"],
        "task_version": identity["task_version"],
        "prompt_sha256": identity["prompt_sha256"],
        "workspace_tree_digest": identity["workspace_tree_digest"],
        "eligible": False,
        "rankable": False,
        "official": False,
        "experiment_digest": experiment_digest,
        "model": {
            "requested": config.model,
            "selection_source": "explicit_cli",
            "provider_signed": False,
        },
        "budget": {
            "max_ai_credits": config.max_ai_credits,
            "timeout_seconds": config.timeout_seconds,
        },
        "attempts": [],
        "checkpoint": {
            "completed_attempts": 0,
            "required_attempts": ATTEMPTS_PER_TASK,
            "complete": False,
            "updated_at": _utc_now(),
        },
    }
    if preregistration_sha256 is not None:
        unsigned["preregistration_sha256"] = preregistration_sha256
    return _seal(unsigned, field="document_sha256")


def _document_with_attempt(
    document: dict[str, Any],
    attempt: dict[str, Any],
) -> dict[str, Any]:
    attempts = sorted(
        [*document["attempts"], attempt],
        key=lambda row: row["repetition"],
    )
    unsigned = {
        key: value for key, value in document.items() if key != "document_sha256"
    }
    unsigned["attempts"] = attempts
    complete = len(attempts) == ATTEMPTS_PER_TASK
    unsigned["eligible"] = complete and all(
        attempt["infrastructure_valid"] is True for attempt in attempts
    )
    unsigned["checkpoint"] = {
        "completed_attempts": len(attempts),
        "required_attempts": ATTEMPTS_PER_TASK,
        "complete": complete,
        "updated_at": _utc_now(),
    }
    return _seal(unsigned, field="document_sha256")


def _verify_attempt(
    attempt: Any,
    *,
    config: RunnerConfig,
    package: TaskPackage,
    experiment_digest: str,
    schedule_lookup: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(attempt, dict):
        raise TaskTrialRunnerError("attempt checkpoint must be an object")
    _verify_seal(attempt, field="attempt_sha256", label="attempt")
    repetition = attempt.get("repetition")
    if (
        isinstance(repetition, bool)
        or not isinstance(repetition, int)
        or not 0 <= repetition < ATTEMPTS_PER_TASK
    ):
        raise TaskTrialRunnerError("attempt repetition is invalid")
    expected_id = _attempt_id(experiment_digest, package, repetition)
    if attempt.get("attempt_id") != expected_id:
        raise TaskTrialRunnerError("attempt_id does not match the planned repetition")
    planned = (
        schedule_lookup.get((package.digest, repetition))
        if schedule_lookup is not None
        else None
    )
    if schedule_lookup is not None and planned is None:
        raise TaskTrialRunnerError("attempt is absent from the frozen schedule")
    if planned is None:
        expected_order, expected_key = _fallback_randomized_order(
            config.randomization_seed,
            package.digest,
            repetition,
        )
        expected_schedule_index = None
    else:
        expected_order = tuple(planned["randomized_order"])
        expected_key = str(planned["randomization_key_sha256"])
        expected_schedule_index = int(planned["schedule_index"])
    if attempt.get("randomized_order") != list(expected_order):
        raise TaskTrialRunnerError("attempt randomized_order does not match")
    if attempt.get("randomization_key_sha256") != expected_key:
        raise TaskTrialRunnerError("attempt randomization key does not match")
    if attempt.get("schedule_index") != expected_schedule_index:
        raise TaskTrialRunnerError("attempt schedule_index does not match")
    infrastructure_valid = attempt.get("infrastructure_valid")
    if not isinstance(infrastructure_valid, bool):
        raise TaskTrialRunnerError("attempt infrastructure_valid must be boolean")
    infrastructure_reasons = attempt.get("infrastructure_reasons")
    if not isinstance(infrastructure_reasons, list) or any(
        not isinstance(reason, str) or not reason for reason in infrastructure_reasons
    ):
        raise TaskTrialRunnerError("attempt infrastructure_reasons are invalid")
    if infrastructure_valid is bool(infrastructure_reasons):
        raise TaskTrialRunnerError(
            "attempt infrastructure validity/reasons are inconsistent"
        )
    for name in HARNESSES:
        row = attempt.get(name)
        if not isinstance(row, dict):
            raise TaskTrialRunnerError(f"attempt {name} result is missing")
        if not isinstance(row.get("reliable"), bool):
            raise TaskTrialRunnerError(f"attempt {name}.reliable must be boolean")
        score = row.get("score")
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not 0.0 <= float(score) <= 1.0
        ):
            raise TaskTrialRunnerError(f"attempt {name}.score is invalid")
        if row["reliable"] is False and float(score) != 0.0:
            raise TaskTrialRunnerError(
                f"unreliable {name} attempt must have analysis score 0"
            )
        receipt = row.get("receipt")
        if not isinstance(receipt, dict):
            raise TaskTrialRunnerError(f"attempt {name} receipt is missing")
        _verify_seal(receipt, field="receipt_sha256", label=f"{name} receipt")
    _verify_attempt_evidence(config, attempt)
    return attempt


def _verify_document(
    document: Any,
    *,
    config: RunnerConfig,
    package: TaskPackage,
    experiment_digest: str,
    schedule_lookup: dict[tuple[str, int], dict[str, Any]] | None = None,
    preregistration_sha256: str | None = None,
) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise TaskTrialRunnerError("task checkpoint must be an object")
    _verify_seal(document, field="document_sha256", label="task checkpoint")
    identity = _task_identity(package)
    for field in (
        "task_id",
        "category",
        "task_digest",
        "task_version",
        "prompt_sha256",
        "workspace_tree_digest",
    ):
        if document.get(field) != identity[field]:
            raise TaskTrialRunnerError(f"task checkpoint {field} does not match")
    if document.get("schema") != TRIAL_SCHEMA:
        raise TaskTrialRunnerError("task checkpoint schema does not match")
    if document.get("rankable") is not False:
        raise TaskTrialRunnerError("task checkpoint must remain rankable=false")
    if document.get("experiment_digest") != experiment_digest:
        raise TaskTrialRunnerError("task checkpoint belongs to another experiment")
    if preregistration_sha256 is None:
        if "preregistration_sha256" in document:
            raise TaskTrialRunnerError(
                "task checkpoint unexpectedly contains a preregistration binding"
            )
    elif document.get("preregistration_sha256") != preregistration_sha256:
        raise TaskTrialRunnerError(
            "task checkpoint preregistration binding does not match"
        )
    if document.get("model", {}).get("requested") != config.model:
        raise TaskTrialRunnerError("task checkpoint model does not match")
    if document.get("budget") != {
        "max_ai_credits": config.max_ai_credits,
        "timeout_seconds": config.timeout_seconds,
    }:
        raise TaskTrialRunnerError("task checkpoint budget does not match")
    attempts = document.get("attempts")
    if not isinstance(attempts, list) or len(attempts) > ATTEMPTS_PER_TASK:
        raise TaskTrialRunnerError("task checkpoint attempts are invalid")
    verified = [
        _verify_attempt(
            attempt,
            config=config,
            package=package,
            experiment_digest=experiment_digest,
            schedule_lookup=schedule_lookup,
        )
        for attempt in attempts
    ]
    repetitions = [attempt["repetition"] for attempt in verified]
    if repetitions != list(range(len(repetitions))):
        raise TaskTrialRunnerError(
            "task checkpoint repetitions must be contiguous from zero"
        )
    complete = len(verified) == ATTEMPTS_PER_TASK
    eligible = complete and all(
        attempt["infrastructure_valid"] is True for attempt in verified
    )
    if document.get("eligible") is not eligible:
        raise TaskTrialRunnerError("task checkpoint eligible flag is inconsistent")
    checkpoint = document.get("checkpoint")
    if (
        not isinstance(checkpoint, dict)
        or checkpoint.get("completed_attempts") != len(verified)
        or checkpoint.get("required_attempts") != ATTEMPTS_PER_TASK
        or checkpoint.get("complete") is not complete
    ):
        raise TaskTrialRunnerError("task checkpoint progress metadata is inconsistent")
    return document


def _load_document(
    path: Path,
    *,
    config: RunnerConfig,
    package: TaskPackage,
    experiment_digest: str,
    schedule_lookup: dict[tuple[str, int], dict[str, Any]] | None = None,
    preregistration_sha256: str | None = None,
) -> dict[str, Any]:
    if not path.exists():
        document = _initial_document(
            config,
            package,
            experiment_digest=experiment_digest,
            preregistration_sha256=preregistration_sha256,
        )
        _atomic_write_json(path, document)
        return document
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskTrialRunnerError(f"task checkpoint is unreadable: {exc}") from exc
    return _verify_document(
        document,
        config=config,
        package=package,
        experiment_digest=experiment_digest,
        schedule_lookup=schedule_lookup,
        preregistration_sha256=preregistration_sha256,
    )


def _load_orphan_attempt(
    config: RunnerConfig,
    package: TaskPackage,
    repetition: int,
    *,
    experiment_digest: str,
    schedule_lookup: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    attempt_id = _attempt_id(experiment_digest, package, repetition)
    relative = _attempt_evidence_relative(package, repetition, attempt_id)
    root = config.evidence_root.joinpath(
        *_confined_relative(relative, field="orphan attempt evidence").parts
    )
    if not root.exists():
        return None
    path = root / "attempt.json"
    if not path.is_file():
        raise TaskTrialRunnerError(f"orphan evidence has no attempt.json: {root}")
    try:
        attempt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TaskTrialRunnerError(f"orphan attempt is unreadable: {exc}") from exc
    return _verify_attempt(
        attempt,
        config=config,
        package=package,
        experiment_digest=experiment_digest,
        schedule_lookup=schedule_lookup,
    )


def _validate_config(config: RunnerConfig) -> None:
    try:
        normalized = _model_identifier(config.model)
    except argparse.ArgumentTypeError as exc:
        raise TaskTrialRunnerError(str(exc)) from exc
    if normalized != config.model:
        raise TaskTrialRunnerError("model must be an explicit normalized identifier")
    for name, value in (
        ("max_ai_credits", config.max_ai_credits),
        ("timeout_seconds", config.timeout_seconds),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise TaskTrialRunnerError(f"{name} must be positive")
    if isinstance(config.randomization_seed, bool) or not isinstance(
        config.randomization_seed, int
    ):
        raise TaskTrialRunnerError("randomization_seed must be an integer")
    if not config.task_roots:
        raise TaskTrialRunnerError("at least one task root is required")
    if config.execution_backend not in {"process", "oci"}:
        raise TaskTrialRunnerError("execution_backend must be process or oci")
    for name, value in (
        ("expected_phoenix_commit", config.expected_phoenix_commit),
        ("expected_hve_commit", config.expected_hve_commit),
    ):
        if value is not None and re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise TaskTrialRunnerError(f"{name} must be a full lowercase Git SHA")
    if config.selection_file is not None and (
        config.expected_phoenix_commit is None or config.expected_hve_commit is None
    ):
        raise TaskTrialRunnerError(
            "a frozen task selection requires both expected source commits"
        )
    if config.selection_file is not None and config.execution_backend != "oci":
        raise TaskTrialRunnerError(
            "formal task selection requires the OCI execution backend"
        )
    if config.execution_backend == "oci":
        for name, value in (
            ("oci_copilot_package", config.oci_copilot_package),
            ("oci_image_evidence_dir", config.oci_image_evidence_dir),
        ):
            if value is None:
                raise TaskTrialRunnerError(f"{name} is required for OCI execution")
        if not config.oci_copilot_package.is_dir():
            raise TaskTrialRunnerError("oci_copilot_package is not a directory")
        for name, value in (
            ("oci_runtime_base_image", config.oci_runtime_base_image),
            ("oci_rust_builder_image", config.oci_rust_builder_image),
        ):
            if not isinstance(value, str) or "@sha256:" not in value:
                raise TaskTrialRunnerError(
                    f"{name} must be an explicit digest-pinned image"
                )
    if not (config.phoenix_repo / "Cargo.toml").is_file():
        raise TaskTrialRunnerError(
            f"not an ATV-Phoenix checkout: {config.phoenix_repo}"
        )
    if not (config.hve_repo / "plugins" / "hve-core").is_dir():
        raise TaskTrialRunnerError(f"not an hve-core checkout: {config.hve_repo}")
    if config.ledger_dir.resolve() == config.evidence_root.resolve():
        raise TaskTrialRunnerError("ledger_dir and evidence_root must differ")


def run_experiment(
    config: RunnerConfig,
    *,
    plan_only: bool = False,
) -> dict[str, Any]:
    """Run missing repetitions and atomically checkpoint one document per task."""

    _validate_config(config)
    selection = _load_selection(config.selection_file)
    packages = _load_tasks(config, selection=selection)
    schedule = _schedule_plan(packages, seed=config.randomization_seed)
    schedule_lookup = _schedule_lookup(schedule)
    sources = {
        "atv_phoenix": _source_identity(
            config.phoenix_repo,
            repository="All-The-Vibes/ATV-Phoenix",
        ),
        "hve_core": _source_identity(
            config.hve_repo,
            repository="microsoft/hve-core",
        ),
    }
    _validate_source_cell(config, sources)
    node, loader, runtime = _copilot_runtime_identity()
    oci_images = (
        _build_oci_images(config) if config.execution_backend == "oci" else None
    )
    experiment = _experiment(
        config,
        packages,
        sources=sources,
        runtime=runtime,
        selection=selection,
        schedule=schedule,
        oci_images=oci_images,
    )
    experiment_digest = experiment["experiment_digest"]
    _ensure_experiment_file(config.evidence_root, experiment)
    if plan_only:
        return {
            "schema": SUMMARY_SCHEMA,
            "rankable": False,
            "plan_only": True,
            "experiment_digest": experiment_digest,
            "experiment_path": str(
                (config.evidence_root / "experiment.json").resolve()
            ),
            "tasks": len(packages),
            "attempts_per_task": ATTEMPTS_PER_TASK,
            "planned_attempts": len(schedule),
            "executed_this_run": 0,
        }
    preregistration_sha256: str | None = None
    if selection is not None:
        if oci_images is None:
            raise TaskTrialRunnerError(
                "formal execution has no verified OCI image binding"
            )
        preregistration_sha256 = _validate_formal_preregistration(
            config,
            experiment=experiment,
            packages=packages,
            selection=selection,
            schedule=schedule,
            oci_images=oci_images,
        )
    config.ledger_dir.mkdir(parents=True, exist_ok=True)

    executed = 0
    recovered = 0
    resumed = 0
    with _ledger_lock(config.ledger_dir):
        loaded: dict[str, tuple[TaskPackage, Path, dict[str, Any]]] = {}
        pending_exists = False
        for package in packages:
            path = _task_document_path(config, package)
            document = _load_document(
                path,
                config=config,
                package=package,
                experiment_digest=experiment_digest,
                schedule_lookup=schedule_lookup,
                preregistration_sha256=preregistration_sha256,
            )
            pending_exists |= len(document["attempts"]) < ATTEMPTS_PER_TASK
            resumed += len(document["attempts"])
            loaded[package.digest] = (package, path, document)

        token = _github_token() if pending_exists else ""
        disabled_skills = _ambient_skill_names() if pending_exists else []
        for planned in schedule:
            package, path, document = loaded[str(planned["task_digest"])]
            repetition = int(planned["repetition"])
            if any(
                attempt["repetition"] == repetition for attempt in document["attempts"]
            ):
                continue
            attempt = _load_orphan_attempt(
                config,
                package,
                repetition,
                experiment_digest=experiment_digest,
                schedule_lookup=schedule_lookup,
            )
            if attempt is not None:
                recovered += 1
            else:
                attempt = _run_paired_attempt(
                    config,
                    package,
                    repetition,
                    experiment_digest=experiment_digest,
                    node=node,
                    loader=loader,
                    token=token,
                    disabled_skills=disabled_skills,
                    planned_order=tuple(planned["randomized_order"]),
                    planned_randomization_key=str(planned["randomization_key_sha256"]),
                    schedule_index=int(planned["schedule_index"]),
                    oci_images=oci_images,
                )
                executed += 1
            document = _document_with_attempt(document, attempt)
            _verify_document(
                document,
                config=config,
                package=package,
                experiment_digest=experiment_digest,
                schedule_lookup=schedule_lookup,
                preregistration_sha256=preregistration_sha256,
            )
            _atomic_write_json(path, document)
            loaded[package.digest] = (package, path, document)

        final_documents = [loaded[package.digest][2] for package in packages]

    return {
        "schema": SUMMARY_SCHEMA,
        "rankable": False,
        "experiment_digest": experiment_digest,
        "preregistration_sha256": preregistration_sha256,
        "ledger_dir": str(config.ledger_dir.resolve()),
        "evidence_root": str(config.evidence_root.resolve()),
        "tasks": len(packages),
        "attempts_per_task": ATTEMPTS_PER_TASK,
        "completed_task_documents": sum(
            document["checkpoint"]["complete"] for document in final_documents
        ),
        "completed_attempts": sum(
            len(document["attempts"]) for document in final_documents
        ),
        "executed_this_run": executed,
        "recovered_from_evidence": recovered,
        "resumed_from_checkpoints": resumed,
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run or resume five Phoenix-versus-hve attempts per ATV task package."
        )
    )
    parser.add_argument("--phoenix-repo", required=True)
    parser.add_argument("--hve-repo", required=True)
    parser.add_argument("--tasks-root", action="append", default=[])
    parser.add_argument("--task-id", action="append", default=[])
    parser.add_argument("--category", action="append", default=[])
    parser.add_argument("--selection-file")
    parser.add_argument("--preregistration-file")
    parser.add_argument("--calibration-plan-file")
    parser.add_argument("--calibration-summary-file")
    parser.add_argument("--expected-phoenix-commit")
    parser.add_argument("--expected-hve-commit")
    parser.add_argument(
        "--execution-backend",
        choices=("process", "oci"),
        default="process",
    )
    parser.add_argument("--oci-copilot-package")
    parser.add_argument("--oci-runtime-base-image")
    parser.add_argument("--oci-rust-builder-image")
    parser.add_argument("--oci-image-evidence-dir")
    parser.add_argument("--oci-docker", default="docker")
    parser.add_argument("--model", required=True, type=_model_identifier)
    parser.add_argument("--max-ai-credits", required=True, type=_positive_int)
    parser.add_argument("--timeout-seconds", required=True, type=_positive_int)
    parser.add_argument("--randomization-seed", required=True, type=int)
    parser.add_argument("--ledger-dir", required=True)
    parser.add_argument("--evidence-root")
    parser.add_argument("--work-root")
    parser.add_argument("--plan-only", action="store_true")
    parser.add_argument(
        "--tool-compat-shim",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    roots = tuple(
        Path(value).resolve()
        for value in (args.tasks_root or [str(REPO_ROOT / "tasks" / "pilot")])
    )
    ledger_dir = Path(args.ledger_dir).resolve()
    evidence_root = (
        Path(args.evidence_root).resolve()
        if args.evidence_root
        else ledger_dir.parent / f"{ledger_dir.name}-evidence"
    )
    config = RunnerConfig(
        phoenix_repo=Path(args.phoenix_repo).resolve(),
        hve_repo=Path(args.hve_repo).resolve(),
        task_roots=roots,
        model=args.model,
        max_ai_credits=args.max_ai_credits,
        timeout_seconds=args.timeout_seconds,
        randomization_seed=args.randomization_seed,
        ledger_dir=ledger_dir,
        evidence_root=evidence_root.resolve(),
        task_ids=tuple(args.task_id),
        categories=tuple(args.category),
        selection_file=(
            Path(args.selection_file).resolve() if args.selection_file else None
        ),
        preregistration_file=(
            Path(args.preregistration_file).resolve()
            if args.preregistration_file
            else None
        ),
        calibration_plan_file=(
            Path(args.calibration_plan_file).resolve()
            if args.calibration_plan_file
            else None
        ),
        calibration_summary_file=(
            Path(args.calibration_summary_file).resolve()
            if args.calibration_summary_file
            else None
        ),
        expected_phoenix_commit=args.expected_phoenix_commit,
        expected_hve_commit=args.expected_hve_commit,
        execution_backend=args.execution_backend,
        oci_copilot_package=(
            Path(args.oci_copilot_package).resolve()
            if args.oci_copilot_package
            else None
        ),
        oci_runtime_base_image=args.oci_runtime_base_image,
        oci_rust_builder_image=args.oci_rust_builder_image,
        oci_image_evidence_dir=(
            Path(args.oci_image_evidence_dir).resolve()
            if args.oci_image_evidence_dir
            else None
        ),
        oci_docker=args.oci_docker,
        tool_compat_shim=bool(args.tool_compat_shim),
        work_root=Path(args.work_root).resolve() if args.work_root else None,
    )
    try:
        summary = run_experiment(config, plan_only=bool(args.plan_only))
    except TaskTrialRunnerError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
