"""Local-only benchmark CLI subapplications.

This module is intentionally not wired into ``atv_bench.cli`` yet. It performs no
publication, GitHub, remote upload, or trust promotion. Every executed trial is
local-self-attested and non-rankable.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import tempfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

import typer

from atv_bench.control_plane import (
    ControllerLedger,
    ControllerModelPolicy,
    ControllerProblem,
    ControllerRunRequest,
    ControllerTaskSet,
    TrialController,
)
from atv_bench.control_plane.trial_controller import decode_output_snapshot
from atv_bench.eval import (
    Budget,
    BudgetProfile,
    ControllerAssertedLifecycleReceipt,
    FileAssertionsGrader,
    HarnessRef,
    HarnessStatus,
    InfrastructureStatus,
    ModelPolicyRef,
    PublicationPolicy,
    TaskPackage,
    TaskPackageValidator,
    TaskRef,
    TrialObservation,
    analyze_paired,
    build_paired_schedule,
    verify_public_protocol_export,
)
from atv_bench.eval.bundle import ContentAddressedStore
from atv_bench.eval.protocol_export import (
    budget_analysis_id,
    model_policy_analysis_id,
)
from atv_bench.eval.report import (
    CanonicalBundleInput,
    ReportMetadata,
    generate_report,
    write_static_report,
)
from atv_bench.harness_manifest import (
    HarnessManifestRegistry,
    LoadedHarnessManifest,
)
from atv_bench.protocol import (
    SchemaStore,
    canonical_digest,
    canonical_json_bytes,
    strict_json_loads,
)
from atv_bench.sandbox import (
    CliOciEngine,
    EngineUnavailableError,
    OciNetworkPolicy,
    OciTrialRunner,
)

_REPRODUCTION_SCHEMA = "atv.reproduction-evidence/v1"
_REPRODUCTION_GRADER_SCHEMA = "atv.grader.file-assertions/v1"
_REPRODUCTION_SNAPSHOT_SCHEMAS = {
    "atv.output-snapshot/v1",
    "atv.output-snapshot/v2",
}


class ExitCode(enum.IntEnum):
    OK = 0
    USAGE = 2
    VALIDATION = 3
    UNAVAILABLE = 4
    EXECUTION = 5
    VERIFICATION = 6
    ANALYSIS = 7
    REPRODUCTION = 8


@dataclasses.dataclass(frozen=True, slots=True)
class CliProblem(Exception):
    code: str
    problem: str
    cause: str
    fix: str
    evidence: str
    exit_code: ExitCode

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "problem": self.problem,
            "cause": self.cause,
            "fix": self.fix,
            "evidence": self.evidence,
            "exit_code": int(self.exit_code),
        }


benchmark_app = typer.Typer(
    name="benchmark",
    help="Local-only harness benchmark tooling.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
schema_app = typer.Typer(help="Validate protocol schemas.", no_args_is_help=True)
harness_app = typer.Typer(help="Validate harness manifests.", no_args_is_help=True)
task_app = typer.Typer(help="Validate task packages.", no_args_is_help=True)
trial_app = typer.Typer(help="Run one local smoke trial.", no_args_is_help=True)
eval_app = typer.Typer(help="Plan, run, verify, and analyze local evaluations.", no_args_is_help=True)
benchmark_app.add_typer(schema_app, name="schema")
benchmark_app.add_typer(harness_app, name="harness")
benchmark_app.add_typer(task_app, name="task")
benchmark_app.add_typer(trial_app, name="trial")
benchmark_app.add_typer(eval_app, name="eval")


def _ascii(value: Any) -> str:
    return str(value).encode("ascii", errors="backslashreplace").decode("ascii")


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("ascii")


def _emit(command: str, data: Mapping[str, Any], *, json_output: bool) -> None:
    envelope = {
        "ok": True,
        "command": command,
        "trust_tier": "local-self-attested",
        "rankable": False,
        "data": dict(data),
        "error": None,
    }
    if json_output:
        typer.echo(_json_bytes(envelope).decode("ascii"), nl=False)
        return
    typer.echo(_ascii(f"OK: {command}"))
    for key, value in sorted(data.items()):
        typer.echo(_ascii(f"{key}: {value}"))


def _abort(command: str, problem: CliProblem, *, json_output: bool) -> None:
    envelope = {
        "ok": False,
        "command": command,
        "trust_tier": "local-self-attested",
        "rankable": False,
        "data": None,
        "error": problem.to_dict(),
    }
    if json_output:
        typer.echo(_json_bytes(envelope).decode("ascii"), nl=False)
    else:
        typer.echo(_ascii(f"Problem: {problem.problem}"), err=True)
        typer.echo(_ascii(f"Cause: {problem.cause}"), err=True)
        typer.echo(_ascii(f"Fix: {problem.fix}"), err=True)
        typer.echo(_ascii(f"Evidence: {problem.evidence}"), err=True)
    raise typer.Exit(code=int(problem.exit_code))


def _execute(
    command: str,
    *,
    json_output: bool,
    operation: Callable[[], Mapping[str, Any]],
    fallback_exit: ExitCode,
) -> None:
    try:
        data = operation()
    except CliProblem as problem:
        _abort(command, problem, json_output=json_output)
    except Exception as exc:
        _abort(
            command,
            CliProblem(
                code="unexpected_error",
                problem="The local benchmark command failed closed.",
                cause=type(exc).__name__,
                fix="Inspect the local evidence path and correct the input.",
                evidence=command,
                exit_code=fallback_exit,
            ),
            json_output=json_output,
        )
    _emit(command, data, json_output=json_output)


def _safe_relative(value: str, *, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise CliProblem(
            "path_traversal",
            f"{field} is not a confined relative path.",
            value,
            "Use forward-slash relative paths without '..'.",
            value,
            ExitCode.VERIFICATION,
        )
    return path


def _write_canonical(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(dict(value)) + b"\n")


def _write_relaxed(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(dict(value)))


def _load_json_object(
    path: Path,
    *,
    exit_code: ExitCode,
    relaxed: bool = False,
) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
        value = json.loads(text) if relaxed else strict_json_loads(text)
    except Exception as exc:
        raise CliProblem(
            "json_invalid",
            "A required local JSON document is malformed.",
            f"{path}: {type(exc).__name__}",
            "Regenerate the local artifact from canonical evidence.",
            str(path),
            exit_code,
        ) from None
    if not isinstance(value, dict):
        raise CliProblem(
            "json_not_object",
            "A required local JSON document is not an object.",
            str(path),
            "Use the documented versioned object format.",
            str(path),
            exit_code,
        )
    return value


@schema_app.command("check")
def schema_check(
    directory: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        try:
            store = SchemaStore.from_directory(directory)
        except Exception as exc:
            raise CliProblem(
                "schema_invalid",
                "The schema directory is incomplete or invalid.",
                str(exc),
                "Provide all ATV v1 schemas with valid Draft 2020-12 definitions.",
                str(directory),
                ExitCode.VALIDATION,
            ) from None
        return {
            "directory": str(store.directory),
            "schema_count": len(store.documents_by_kind),
            "schemas": sorted(kind.value for kind in store.documents_by_kind),
        }

    _execute(
        "schema.check",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.VALIDATION,
    )


@harness_app.command("validate")
def harness_validate(
    manifest: Path = typer.Argument(..., exists=True, dir_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        try:
            registry = HarnessManifestRegistry()
            loaded = registry.load(manifest)
        except Exception as exc:
            raise CliProblem(
                "harness_invalid",
                "The harness manifest failed validation.",
                str(exc),
                "Correct schema, digest, runtime, and security declarations.",
                str(manifest),
                ExitCode.VALIDATION,
            ) from None
        return {
            "id": loaded.id,
            "version": loaded.version,
            "digest": loaded.digest,
            "runtime_kind": loaded.runtime_kind,
            "official_eligible": False,
        }

    _execute(
        "harness.validate",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.VALIDATION,
    )


@task_app.command("validate")
def task_validate(
    task: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        try:
            package = TaskPackage.load(task)
            report = TaskPackageValidator().validate(
                package,
                FileAssertionsGrader.from_task(package),
            )
        except Exception as exc:
            raise CliProblem(
                "task_invalid",
                "The task package failed validation.",
                str(exc),
                "Repair task schema, fixtures, grader, or evidence digests.",
                str(task),
                ExitCode.VALIDATION,
            ) from None
        if not report.eligible:
            raise CliProblem(
                "task_ineligible",
                "The task did not pass every eligibility gate.",
                json.dumps(report.to_dict(), sort_keys=True),
                "Fix failed oracle/no-op/alternative/exploit/mutation gates.",
                str(task),
                ExitCode.VALIDATION,
            )
        return report.to_dict()

    _execute(
        "task.validate",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.VALIDATION,
    )


def _model_free_policy() -> ControllerModelPolicy:
    return ControllerModelPolicy.model_free()


def _load_unique_harnesses(paths: Sequence[Path]) -> tuple[LoadedHarnessManifest, ...]:
    registry = HarnessManifestRegistry()
    try:
        return tuple(registry.load(path) for path in paths)
    except Exception as exc:
        raise CliProblem(
            "harness_set_invalid",
            "The harness set contains an invalid or duplicate identity.",
            str(exc),
            "Use unique id/version manifests with immutable artifacts.",
            ", ".join(str(path) for path in paths),
            ExitCode.VALIDATION,
        ) from None


def _load_unique_tasks(paths: Sequence[Path]) -> tuple[TaskPackage, ...]:
    packages: list[TaskPackage] = []
    seen: set[str] = set()
    for path in paths:
        try:
            package = TaskPackage.load(path)
        except Exception as exc:
            raise CliProblem(
                "task_set_invalid",
                "A task package in the suite is invalid.",
                str(exc),
                "Validate every task before planning.",
                str(path),
                ExitCode.VALIDATION,
            ) from None
        if package.id in seen:
            raise CliProblem(
                "duplicate_task_id",
                "The task suite contains a duplicate task id.",
                package.id,
                "Use unique task ids.",
                str(path),
                ExitCode.VALIDATION,
            )
        seen.add(package.id)
        packages.append(package)
    if not packages:
        raise CliProblem(
            "task_set_empty",
            "The evaluation plan has no tasks.",
            "No --task values were provided.",
            "Provide at least one validated task.",
            "eval.plan",
            ExitCode.USAGE,
        )
    return tuple(packages)


def _plan_document(
    *,
    tasks: Sequence[Path],
    harnesses: Sequence[Path],
    repetitions: int,
    seed: int,
    worker: str,
    benchmark_release: str,
) -> dict[str, Any]:
    packages = _load_unique_tasks(tasks)
    loaded = _load_unique_harnesses(harnesses)
    non_oci = sorted(item.id for item in loaded if item.runtime_kind != "oci")
    if non_oci:
        raise CliProblem(
            "eval_requires_oci_harness",
            "The isolated benchmark evaluator requires protocol-v1 OCI harnesses.",
            "Non-OCI harnesses: " + ", ".join(non_oci),
            (
                "Use a digest-pinned OCI manifest that implements the attached "
                "request/hello/accepted/result protocol. Process manifests remain "
                "available for local adapter conformance and harness-run."
            ),
            ", ".join(str(path) for path in harnesses),
            ExitCode.VALIDATION,
        )
    policy = _model_free_policy()
    budget = BudgetProfile("local-smoke", Budget(60, 1, 1, 1))
    schedule = build_paired_schedule(
        benchmark_release=benchmark_release,
        protocol_version="atv.trial/v1",
        tasks=tuple(
            TaskRef(
                package.id,
                package.version,
                canonical_digest(package.manifest)["value"],
            )
            for package in packages
        ),
        harnesses=tuple(
            HarnessRef(item.id, item.version, item.digest) for item in loaded
        ),
        model_policies=(
            ModelPolicyRef(policy.id, policy.version, policy.digest),
        ),
        budget_profiles=(budget,),
        repetitions=repetitions,
        seed=seed,
        workers=(worker,),
    )
    document = {
        "schema": "atv.eval-plan/v1",
        "benchmark_release": benchmark_release,
        "protocol_version": "atv.trial/v1",
        "seed": seed,
        "repetitions": repetitions,
        "workers": [worker],
        "tasks": [
            {
                "path": str(path.resolve()),
                "id": package.id,
                "version": package.version,
                "digest": canonical_digest(package.manifest)["value"],
            }
            for path, package in zip(tasks, packages, strict=True)
        ],
        "harnesses": [
            {
                "path": str(path.resolve()),
                "id": item.id,
                "version": item.version,
                "digest": item.digest,
            }
            for path, item in zip(harnesses, loaded, strict=True)
        ],
        "model_policy": {
            "id": policy.id,
            "version": policy.version,
            "digest": policy.digest,
            "model_free": True,
        },
        "budget_profile": budget.to_dict(),
        "schedule": [item.to_dict() for item in schedule],
        "trust_tier": "local-self-attested",
        "rankable": False,
    }
    return {**document, "plan_digest": canonical_digest(document)["value"]}


@eval_app.command("plan")
def eval_plan(
    task: list[Path] = typer.Option(..., "--task", exists=True, file_okay=False),
    harness: list[Path] = typer.Option(..., "--harness", exists=True, dir_okay=False),
    out: Path = typer.Option(..., "--out"),
    repetitions: int = typer.Option(1, "--repetitions", min=1),
    seed: int = typer.Option(0, "--seed"),
    worker: str = typer.Option("linux-amd64", "--worker"),
    benchmark_release: str = typer.Option("ATV-2026.07", "--benchmark-release"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        plan = _plan_document(
            tasks=task,
            harnesses=harness,
            repetitions=repetitions,
            seed=seed,
            worker=worker,
            benchmark_release=benchmark_release,
        )
        _write_canonical(out, plan)
        return {
            "path": str(out),
            "plan_digest": plan["plan_digest"],
            "trial_count": len(plan["schedule"]),
        }

    _execute(
        "eval.plan",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.VALIDATION,
    )


def _load_plan(path: Path) -> tuple[dict[str, Any], tuple[TaskPackage, ...], tuple[LoadedHarnessManifest, ...], tuple[Any, ...]]:
    plan = _load_json_object(path, exit_code=ExitCode.VALIDATION)
    expected = {
        "schema",
        "benchmark_release",
        "protocol_version",
        "seed",
        "repetitions",
        "workers",
        "tasks",
        "harnesses",
        "model_policy",
        "budget_profile",
        "schedule",
        "trust_tier",
        "rankable",
        "plan_digest",
    }
    if set(plan) != expected or plan.get("schema") != "atv.eval-plan/v1":
        raise CliProblem(
            "plan_malformed",
            "The evaluation plan has an unsupported shape.",
            f"fields={sorted(plan)}",
            "Regenerate it with eval plan.",
            str(path),
            ExitCode.VALIDATION,
        )
    digest_payload = {key: value for key, value in plan.items() if key != "plan_digest"}
    if plan["plan_digest"] != canonical_digest(digest_payload)["value"]:
        raise CliProblem(
            "plan_digest_mismatch",
            "The evaluation plan digest does not match its content.",
            str(plan["plan_digest"]),
            "Regenerate the immutable plan.",
            str(path),
            ExitCode.VALIDATION,
        )
    for collection in ("tasks", "harnesses"):
        for item in plan[collection]:
            raw = str(item.get("path", ""))
            if not Path(raw).is_absolute():
                _safe_relative(raw, field=f"{collection}.path")
    task_paths = tuple(Path(item["path"]) for item in plan["tasks"])
    harness_paths = tuple(Path(item["path"]) for item in plan["harnesses"])
    regenerated = _plan_document(
        tasks=task_paths,
        harnesses=harness_paths,
        repetitions=int(plan["repetitions"]),
        seed=int(plan["seed"]),
        worker=str(plan["workers"][0]),
        benchmark_release=str(plan["benchmark_release"]),
    )
    if regenerated != plan:
        raise CliProblem(
            "plan_reproduction_mismatch",
            "The plan no longer matches its task or harness inputs.",
            "Immutable input identity changed.",
            "Restore exact inputs or create a new plan.",
            str(path),
            ExitCode.VALIDATION,
        )
    packages = _load_unique_tasks(task_paths)
    harnesses = _load_unique_harnesses(harness_paths)
    policy = _model_free_policy()
    schedule = build_paired_schedule(
        benchmark_release=plan["benchmark_release"],
        protocol_version=plan["protocol_version"],
        tasks=tuple(
            TaskRef(item["id"], item["version"], item["digest"])
            for item in plan["tasks"]
        ),
        harnesses=tuple(
            HarnessRef(item["id"], item["version"], item["digest"])
            for item in plan["harnesses"]
        ),
        model_policies=(ModelPolicyRef(policy.id, policy.version, policy.digest),),
        budget_profiles=(
            BudgetProfile(
                plan["budget_profile"]["id"],
                Budget(**plan["budget_profile"]["budget"]),
            ),
        ),
        repetitions=plan["repetitions"],
        seed=plan["seed"],
        workers=tuple(plan["workers"]),
    )
    return plan, packages, harnesses, schedule


def _engine() -> CliOciEngine:
    try:
        engine = CliOciEngine.auto()
        ok, detail = engine.daemon_status()
    except EngineUnavailableError as exc:
        raise CliProblem(
            "oci_engine_missing",
            "No local OCI engine is installed.",
            str(exc),
            "Install and start Docker or rootless Podman.",
            "local engine",
            ExitCode.UNAVAILABLE,
        ) from None
    if not ok:
        raise CliProblem(
            "oci_engine_unavailable",
            "The local OCI engine daemon is unavailable.",
            detail,
            "Start Docker Desktop or the rootless Podman service.",
            engine.executable,
            ExitCode.UNAVAILABLE,
        )
    return engine


def _write_export(directory: Path, result: Any, *, task_path: Path, harness_path: Path) -> None:
    directory.mkdir(parents=True, exist_ok=False)
    exported = result.protocol_export
    if exported is None:
        raise CliProblem(
            "local_export_missing",
            "The local trial did not produce a canonical export.",
            result.problem.code if result.problem else "unknown",
            "Inspect controller evidence and rerun.",
            str(directory),
            ExitCode.EXECUTION,
        )
    _write_canonical(directory / "bundle.json", exported.bundle)
    documents_root = directory / "documents"
    for relative, data in exported.documents.items():
        rel = _safe_relative(relative, field="bundle document path")
        target = documents_root.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    output_root = directory / "immutable-output"
    output_manifest: list[dict[str, Any]] = []
    bundle = result.internal_bundle
    if bundle is not None:
        for record in bundle.manifest["artifacts"]:
            if not record["path"].startswith("output/"):
                continue
            relative = record["path"][len("output/") :]
            rel = _safe_relative(relative, field="output path")
            data = bundle.store.read_bytes(record["sha256"])
            target = output_root.joinpath(*rel.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            output_manifest.append(
                {
                    "path": relative,
                    "sha256": record["sha256"],
                    "size": record["size"],
                }
            )
    local = {
        "schema": "atv.local-run/v2",
        "source_inputs": {
            "task_name": task_path.name,
            "harness_name": harness_path.name,
        },
        "attempt_id": result.request.scheduled.attempt.attempt_id,
        "trial_id": result.request.scheduled.spec.trial_id,
        "internal_bundle_digest": (
            result.internal_bundle.digest if result.internal_bundle else None
        ),
        "grade": result.grade.to_dict() if result.grade else None,
        "output_manifest": output_manifest,
        "canonical_reproduction": (
            {
                "bundle": "bundle.json",
                "manifest": "documents/reproduction/manifest.json",
            }
            if result.grade is not None
            else None
        ),
        "trust_tier": "local-self-attested",
        "rankable": False,
    }
    _write_relaxed(directory / "local-run.json", local)
    (directory / "controller-ledger.jsonl").write_bytes(
        b"".join(
            canonical_json_bytes(entry.to_dict()) + b"\n"
            for entry in result.ledger_entries
        )
    )


def _run_schedule(
    *,
    plan: Mapping[str, Any],
    packages: Sequence[TaskPackage],
    harnesses: Sequence[LoadedHarnessManifest],
    schedule: Sequence[Any],
    output: Path,
) -> list[Any]:
    engine = _engine()
    output.mkdir(parents=True, exist_ok=True)
    work = output / ".work"
    work.mkdir(exist_ok=True)
    cas = ContentAddressedStore(output / ".cas")
    ledger = ControllerLedger(output / "controller-ledger.jsonl")
    package_by_id = {item.id: item for item in packages}
    harness_by_id = {item.id: item for item in harnesses}
    policy = _model_free_policy()
    results = []
    for item in schedule:
        task = package_by_id[item.spec.task.id]
        harness = harness_by_id[item.spec.harness.id]
        attempt_dir = output / item.attempt.attempt_id
        controller = TrialController(
            oci_runner=OciTrialRunner(engine, work_root=work),
            ledger=ledger,
            store=cas,
        )
        result = controller.run(
            ControllerRunRequest(
                scheduled=item,
                task=task,
                harness=harness,
                model_policy=policy,
                task_set=ControllerTaskSet(
                    "local-plan",
                    "1.0.0",
                    plan["plan_digest"],
                ),
                run_id=f"local-{plan['plan_digest'][:24]}",
                network=OciNetworkPolicy.none(),
            )
        )
        _write_export(
            attempt_dir,
            result,
            task_path=Path(
                next(row["path"] for row in plan["tasks"] if row["id"] == task.id)
            ),
            harness_path=Path(
                next(
                    row["path"]
                    for row in plan["harnesses"]
                    if row["id"] == harness.id
                )
            ),
        )
        results.append(result)
    return results


@trial_app.command("smoke")
def trial_smoke(
    harness: Path = typer.Option(..., "--harness", exists=True, dir_okay=False),
    task: Path = typer.Option(..., "--task", exists=True, file_okay=False),
    out: Path = typer.Option(..., "--out"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        plan = _plan_document(
            tasks=(task,),
            harnesses=(harness,),
            repetitions=1,
            seed=0,
            worker="linux-amd64",
            benchmark_release="ATV-2026.07",
        )
        _write_canonical(out / "plan.json", plan)
        loaded, packages, harnesses, schedule = _load_plan(out / "plan.json")
        results = _run_schedule(
            plan=loaded,
            packages=packages,
            harnesses=harnesses,
            schedule=schedule,
            output=out,
        )
        result = results[0]
        if result.problem:
            raise CliProblem(
                result.problem.code,
                result.problem.problem,
                result.problem.cause,
                result.problem.fix,
                result.problem.evidence,
                ExitCode.EXECUTION,
            )
        return {
            "attempt_id": result.request.scheduled.attempt.attempt_id,
            "score": result.grade.score if result.grade else 0.0,
            "bundle_id": result.protocol_export.bundle["bundle_id"],
            "output": str(out),
            "trust_tier": result.trust_tier,
            "rankable": result.rankable,
        }

    _execute(
        "trial.smoke",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.EXECUTION,
    )


@eval_app.command("run")
def eval_run(
    plan: Path = typer.Argument(..., exists=True, dir_okay=False),
    out: Path = typer.Option(..., "--out"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        loaded, packages, harnesses, schedule = _load_plan(plan)
        results = _run_schedule(
            plan=loaded,
            packages=packages,
            harnesses=harnesses,
            schedule=schedule,
            output=out,
        )
        failures = [item for item in results if item.problem is not None]
        if failures:
            first = failures[0].problem
            raise CliProblem(
                first.code,
                first.problem,
                first.cause,
                first.fix,
                first.evidence,
                ExitCode.EXECUTION,
            )
        return {
            "trial_count": len(results),
            "attempt_ids": [
                item.request.scheduled.attempt.attempt_id for item in results
            ],
            "output": str(out),
            "trust_tier": "local-self-attested",
            "rankable": False,
        }

    _execute(
        "eval.run",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.EXECUTION,
    )


def _export_directories(root: Path) -> tuple[Path, ...]:
    if (root / "bundle.json").is_file():
        return (root,)
    directories = tuple(
        sorted(
            path
            for path in root.iterdir()
            if path.is_dir() and (path / "bundle.json").is_file()
        )
    )
    if not directories:
        raise CliProblem(
            "bundle_not_found",
            "No canonical local bundle was found.",
            str(root),
            "Point to a trial directory or eval-run output directory.",
            str(root),
            ExitCode.VERIFICATION,
        )
    return directories


def _load_export(directory: Path) -> CanonicalBundleInput:
    bundle_path = directory / "bundle.json"
    bundle = _load_json_object(bundle_path, exit_code=ExitCode.VERIFICATION)
    documents: dict[str, bytes] = {}
    for path in (directory / "documents").rglob("*"):
        if path.is_symlink():
            raise CliProblem(
                "bundle_symlink",
                "Bundle documents contain a symlink.",
                str(path),
                "Use regular immutable files only.",
                str(directory),
                ExitCode.VERIFICATION,
            )
        if path.is_file():
            relative = path.relative_to(directory / "documents").as_posix()
            _safe_relative(relative, field="bundle document")
            documents[relative] = path.read_bytes()
    try:
        verify_public_protocol_export(bundle, documents)
    except Exception as exc:
        raise CliProblem(
            "bundle_verification_failed",
            "Canonical bundle verification failed.",
            str(exc),
            "Restore untampered bundle and document bytes.",
            str(directory),
            ExitCode.VERIFICATION,
        ) from None
    return CanonicalBundleInput(bundle=bundle, documents=documents)


@eval_app.command("verify")
def eval_verify(
    path: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        rows = []
        for directory in _export_directories(path):
            item = _load_export(directory)
            result = verify_public_protocol_export(item.bundle, item.documents)
            rows.append(
                {
                    "directory": str(directory),
                    "bundle_id": item.bundle["bundle_id"],
                    "trial_id": result["trial_id"],
                    "status": result["status"],
                    "trust_tier": result["trust_tier"],
                    "rankable": result["rankable"],
                }
            )
        return {"verified_count": len(rows), "results": rows, "offline": True}

    _execute(
        "eval.verify",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.VERIFICATION,
    )


def _contains_nested_observations(value: Any) -> bool:
    forbidden = {"games", "rounds", "simulations", "nested_outcomes"}
    if isinstance(value, Mapping):
        if forbidden & {str(key).lower() for key in value}:
            return True
        return any(_contains_nested_observations(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_nested_observations(item) for item in value)
    return False


def _reject_raw_nested_observations(directory: Path) -> None:
    bundle = _load_json_object(
        directory / "bundle.json",
        exit_code=ExitCode.ANALYSIS,
    )
    if _contains_nested_observations(bundle):
        raise CliProblem(
            "nested_observation_forbidden",
            "Analysis input contains nested games, rounds, or simulations.",
            str(directory / "bundle.json"),
            "Provide one independent harness trial per canonical bundle.",
            str(directory),
            ExitCode.ANALYSIS,
        )
    documents = directory / "documents"
    for path in documents.rglob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if _contains_nested_observations(value):
            raise CliProblem(
                "nested_observation_forbidden",
                "Analysis input contains nested games, rounds, or simulations.",
                str(path),
                "Provide one independent harness trial per canonical bundle.",
                str(directory),
                ExitCode.ANALYSIS,
            )


def _observation(item: CanonicalBundleInput) -> TrialObservation:
    result = verify_public_protocol_export(item.bundle, item.documents)
    if _contains_nested_observations(item.bundle) or _contains_nested_observations(result):
        raise CliProblem(
            "nested_observation_forbidden",
            "Analysis input contains nested games, rounds, or simulations.",
            result["trial_id"],
            "Provide one independent harness trial per canonical bundle.",
            item.bundle["bundle_id"],
            ExitCode.ANALYSIS,
        )
    request_descriptor = item.bundle["contents"]["trial_request"]
    request = strict_json_loads(
        item.documents[request_descriptor["path"]].decode("utf-8")
    )
    failure = result["failure"]
    infrastructure = bool(failure and failure["infrastructure"])
    model_policy = result["model_policy"]
    budget = result["budget"]
    status_map = {
        "success": HarnessStatus.COMPLETED,
        "task_failed": HarnessStatus.COMPLETED,
        "partial": HarnessStatus.COMPLETED,
        "no_edit": HarnessStatus.NO_EDIT,
        "invalid_artifact": HarnessStatus.INVALID_ARTIFACT,
        "task_timeout": HarnessStatus.TIMED_OUT,
        "model_unreachable": HarnessStatus.MODEL_UNREACHABLE,
        "auth_failed": HarnessStatus.AUTH_FAILED,
        "policy_denied": HarnessStatus.POLICY_DENIED,
        "budget_exhausted": HarnessStatus.BUDGET_EXHAUSTED,
        "harness_crash": HarnessStatus.CRASHED,
        "protocol_error": HarnessStatus.PROTOCOL_ERROR,
        "cancelled": HarnessStatus.NOT_RUN,
        "infrastructure_error": HarnessStatus.NOT_RUN,
        "grader_failed": HarnessStatus.COMPLETED,
    }
    score = result["evaluation"]["score"]
    normalized_score = (
        score["earned"] / score["possible"] if score is not None else 0.0
    )
    infrastructure_status = InfrastructureStatus.OK
    if infrastructure:
        infrastructure_status = {
            "cancelled": InfrastructureStatus.CANCELLED,
            "grader_failed": InfrastructureStatus.GRADER_FAILED,
            "infrastructure_error": InfrastructureStatus.RUNNER_FAILED,
        }.get(result["status"], InfrastructureStatus.RUNNER_FAILED)
    return TrialObservation(
        trial_id=result["trial_id"],
        task_id=result["task"]["id"],
        harness_id=result["harness"]["id"],
        model_policy_id=model_policy_analysis_id(model_policy),
        budget_profile_id=budget_analysis_id(
            budget["profile_id"],
            request["budget_limits"],
        ),
        repetition=int(request["order_assignment"]["repetition"]),
        infrastructure_status=infrastructure_status,
        harness_status=status_map[result["status"]],
        score=None if infrastructure else float(normalized_score),
    )


@eval_app.command("analyze")
def eval_analyze(
    results: Path = typer.Argument(..., exists=True, file_okay=False),
    harness_a: str = typer.Option(..., "--harness-a"),
    harness_b: str = typer.Option(..., "--harness-b"),
    out: Path = typer.Option(..., "--out"),
    generated_at: str = typer.Option("2026-07-19T00:00:00Z", "--generated-at"),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        directories = _export_directories(results)
        for directory in directories:
            _reject_raw_nested_observations(directory)
        inputs = tuple(_load_export(path) for path in directories)
        observations = tuple(_observation(item) for item in inputs)
        try:
            analysis = analyze_paired(
                observations,
                harness_a=harness_a,
                harness_b=harness_b,
                equivalence_margin=0.05,
                bootstrap_samples=1_000,
                seed=0,
                publication_policy=PublicationPolicy.official(),
                quality_evidence=None,
            )
            report = generate_report(
                inputs,
                metadata=ReportMetadata(generated_at=generated_at),
            )
        except Exception as exc:
            raise CliProblem(
                "analysis_failed",
                "The paired trial analysis failed.",
                str(exc),
                "Provide paired fresh trials without nested outcomes.",
                str(results),
                ExitCode.ANALYSIS,
            ) from None
        out.mkdir(parents=True, exist_ok=True)
        _write_relaxed(out / "analysis.json", analysis.to_dict())
        report_json, report_html = write_static_report(report, out / "report")
        return {
            "analysis": str(out / "analysis.json"),
            "report_json": str(report_json),
            "report_html": str(report_html),
            "publication_eligible": analysis.publication_eligible,
            "decision": analysis.publication_decision.value,
            "quality_gate_failures": [
                item.to_dict() for item in analysis.quality_gate_failures
            ],
        }

    _execute(
        "eval.analyze",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.ANALYSIS,
    )


@eval_app.command("reproduce")
def eval_reproduce(
    trial: Path = typer.Argument(..., exists=True, file_okay=False),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    def operation() -> Mapping[str, Any]:
        item = _load_export(trial)
        contents = item.bundle["contents"]
        log_descriptors = list(contents["logs"])
        manifest_descriptors = [
            descriptor
            for descriptor in log_descriptors
            if descriptor["schema"] == _REPRODUCTION_SCHEMA
        ]
        if len(manifest_descriptors) != 1:
            raise CliProblem(
                "reproduction_evidence_missing",
                "The canonical bundle lacks one portable reproduction manifest.",
                f"count={len(manifest_descriptors)}",
                "Re-run the trial with bundled grader and output evidence.",
                str(trial),
                ExitCode.REPRODUCTION,
            )
        manifest_descriptor = manifest_descriptors[0]
        try:
            reproduction = strict_json_loads(
                item.documents[manifest_descriptor["path"]].decode("utf-8")
            )
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise CliProblem(
                "reproduction_evidence_invalid",
                "The canonical reproduction manifest is malformed.",
                type(exc).__name__,
                "Restore the exact bundled reproduction evidence.",
                str(trial),
                ExitCode.REPRODUCTION,
            ) from None
        expected_fields = {
            "schema",
            "task_manifest_digest",
            "grader",
            "output_snapshot",
            "grade_result_digest",
            "grader_digest",
            "output_tree_digest",
        }
        if (
            not isinstance(reproduction, dict)
            or set(reproduction) != expected_fields
            or reproduction.get("schema") != _REPRODUCTION_SCHEMA
        ):
            raise CliProblem(
                "reproduction_evidence_invalid",
                "The canonical reproduction manifest has an unsupported shape.",
                repr(sorted(reproduction) if isinstance(reproduction, dict) else type(reproduction).__name__),
                "Restore the exact bundled reproduction evidence.",
                str(trial),
                ExitCode.REPRODUCTION,
            )
        task_descriptor = contents["task_manifest"]
        grader_descriptor = reproduction["grader"]
        snapshot_descriptor = reproduction["output_snapshot"]
        if (
            reproduction["task_manifest_digest"] != task_descriptor["digest"]
            or not isinstance(grader_descriptor, Mapping)
            or not isinstance(snapshot_descriptor, Mapping)
            or grader_descriptor not in log_descriptors
            or snapshot_descriptor not in log_descriptors
            or grader_descriptor.get("schema") != _REPRODUCTION_GRADER_SCHEMA
            or snapshot_descriptor.get("schema")
            not in _REPRODUCTION_SNAPSHOT_SCHEMAS
        ):
            raise CliProblem(
                "reproduction_binding_invalid",
                "Bundled task, grader, and output evidence are not mutually bound.",
                str(manifest_descriptor["path"]),
                "Restore the canonical bundle as a complete unit.",
                str(trial),
                ExitCode.REPRODUCTION,
            )
        grader_result_descriptor = contents["grader_result"]
        if grader_result_descriptor is None:
            raise CliProblem(
                "reproduction_grade_missing",
                "The canonical bundle does not contain a recorded grade.",
                item.bundle["trial_id"],
                "Reproduce only a successfully graded trial.",
                str(trial),
                ExitCode.REPRODUCTION,
            )
        try:
            task_manifest = strict_json_loads(
                item.documents[task_descriptor["path"]].decode("utf-8")
            )
            grader_spec = json.loads(
                item.documents[grader_descriptor["path"]].decode("utf-8")
            )
            original = json.loads(
                item.documents[grader_result_descriptor["path"]].decode("utf-8")
            )
            snapshot = item.documents[snapshot_descriptor["path"]]
        except (KeyError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise CliProblem(
                "reproduction_evidence_invalid",
                "Bundled task, grader, output, or grade evidence is malformed.",
                type(exc).__name__,
                "Restore the exact canonical bundle.",
                str(trial),
                ExitCode.REPRODUCTION,
            ) from None
        try:
            grader = FileAssertionsGrader(grader_spec)
        except Exception as exc:
            raise CliProblem(
                "reproduction_grader_invalid",
                "The bundled grader specification cannot be loaded.",
                type(exc).__name__,
                "Restore the exact canonical grader evidence.",
                str(trial),
                ExitCode.REPRODUCTION,
            ) from None
        if (
            reproduction["grade_result_digest"] != original.get("result_digest")
            or reproduction["grader_digest"] != original.get("grader_digest")
            or reproduction["grader_digest"] != grader.grader_digest
            or reproduction["output_tree_digest"]
            != original.get("output_tree_digest")
        ):
            raise CliProblem(
                "reproduction_binding_invalid",
                "The bundled grader or output identity differs from the recorded grade.",
                str(manifest_descriptor["path"]),
                "Restore the canonical bundle as a complete unit.",
                str(trial),
                ExitCode.REPRODUCTION,
            )
        with tempfile.TemporaryDirectory(prefix="atv-reproduce-") as temporary:
            output = Path(temporary) / "output"
            try:
                decode_output_snapshot(
                    snapshot,
                    output,
                    output_contract=task_manifest["output"],
                )
            except ControllerProblem as exc:
                raise CliProblem(
                    "reproduction_output_invalid",
                    "The bundled output snapshot cannot be reconstructed.",
                    exc.code,
                    "Restore the exact canonical output evidence.",
                    str(trial),
                    ExitCode.REPRODUCTION,
                ) from None
            task_view = SimpleNamespace(manifest=task_manifest)
            try:
                reproduced = grader.grade(
                    task_view,
                    output,
                    lifecycle_receipt=ControllerAssertedLifecycleReceipt.completed(
                        controller_id="local-reproduction"
                    ),
                )
            except Exception as exc:
                raise CliProblem(
                    "reproduction_grading_failed",
                    "The bundled grader could not replay the bundled output.",
                    type(exc).__name__,
                    "Inspect the canonical grader and output evidence.",
                    str(trial),
                    ExitCode.REPRODUCTION,
                ) from None
        comparable_fields = (
            "passed",
            "score",
            "pass_score",
            "assertions",
            "grader_digest",
            "output_tree_digest",
        )
        matches = all(
            reproduced.to_dict()[field] == original[field]
            for field in comparable_fields
        )
        if not matches:
            raise CliProblem(
                "reproduction_mismatch",
                "Trusted grader reproduction differs from the recorded result.",
                reproduced.result_digest,
                "Investigate grader, task, or output drift; do not publish a new score.",
                str(trial),
                ExitCode.REPRODUCTION,
            )
        public_result = verify_public_protocol_export(item.bundle, item.documents)
        return {
            "match": True,
            "trial_id": public_result["trial_id"],
            "recorded_score": original["score"],
            "reproduced_score": reproduced.score,
            "official_score_created": False,
        }

    _execute(
        "eval.reproduce",
        json_output=json_output,
        operation=operation,
        fallback_exit=ExitCode.REPRODUCTION,
    )
