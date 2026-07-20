"""Real OCI engine smoke test.

This test never pulls an image. It skips with the exact unavailable prerequisite
when no daemon/rootless engine or cached digest-pinned smoke image exists.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import secrets
import subprocess

import pytest

from atv_bench.eval import (
    Budget,
    BudgetProfile,
    HarnessRef,
    ModelPolicyRef,
    TaskPackage,
    TrialAttempt,
    TrialSpec,
)
from atv_bench.sandbox import (
    CliOciEngine,
    DigestPinnedImage,
    EngineUnavailableError,
    OciNetworkPolicy,
    OciResourcePolicy,
    OciRunnerError,
    OciStorageMode,
    OciTrialRequest,
    OciTrialRunner,
    OciTrialStatus,
)
from atv_bench.security import CredentialBroker, TrialBudget, TrialPolicy

pytestmark = pytest.mark.integration

SMOKE_ROOT = Path(__file__).resolve().parents[1] / "tasks" / "smoke" / "repair_config"


def _attempt(package: TaskPackage) -> TrialAttempt:
    spec = TrialSpec(
        benchmark_release="ATV-2026.09",
        protocol_version="atv.trial/v1",
        schedule_id="1" * 64,
        task=package.task_ref,
        harness=HarnessRef("oci-smoke", "1.0.0", "2" * 64),
        model_policy=ModelPolicyRef("none", "1.0.0", "3" * 64),
        budget_profile=BudgetProfile(
            "smoke",
            Budget(60, 1, 1, 1),
        ),
        repetition=0,
        schedule_seed=1,
    )
    return TrialAttempt(spec, 1, "4" * 64)


def _engine_package_image_or_skip():
    try:
        engine = CliOciEngine.auto()
    except EngineUnavailableError as exc:
        pytest.skip(f"OCI integration unavailable: {exc}")
    reachable, detail = engine.daemon_status()
    if not reachable:
        pytest.skip(
            f"OCI integration unavailable: {engine.kind} daemon/rootless service "
            f"is not reachable: {detail}"
        )

    package = TaskPackage.load(SMOKE_ROOT)
    image = DigestPinnedImage.parse(package.manifest["environment"]["image"])
    try:
        inspection = engine.inspect_image(image)
    except Exception as exc:
        pytest.skip(
            "OCI integration unavailable: required digest-pinned smoke image is "
            f"not locally cached ({image.reference}): {exc}"
        )
    if not inspection.verified:
        pytest.skip(
            "OCI integration unavailable: locally cached smoke image does not "
            f"verify requested digest {image.digest}"
        )
    return engine, package, image


def _assert_hard_quota_volumes_removed(engine, result):
    storage = result.evidence.storage
    assert storage["selected_mode"] == "hard-quota"
    assert storage["hard_storage_enforced"] is True
    assert storage["hard_storage_cleanup_succeeded"] is True
    assert storage["host_bind_writable_workspace_or_artifacts"] is False
    assert len(storage["volumes"]) == 2
    for volume in storage["volumes"]:
        assert volume["quota_verified"] is True
        assert volume["lifecycle_verified"] is True
        assert volume["inspection"]["driver"] == "local"
        assert volume["inspection"]["mountpoint"].startswith("/")
        assert volume["inspection"]["options"] == volume["spec"]["options"]
        assert (
            f"size={volume['spec']['size_bytes']}"
            in volume["inspection"]["options"]["o"]
        )
        assert volume["cleanup"]["attempted"] is True
        assert volume["cleanup"]["confirmed_absent"] is True
        assert engine.volume_exists(volume["spec"]["name"]) is False
    assert {
        volume["spec"]["purpose"]: tuple(volume["spec"]["subpaths"])
        for volume in storage["volumes"]
    } == {
        "harness-aggregate": ("workspace", "artifacts", "tmp"),
        "grader-aggregate": ("output", "tmp"),
    }


def test_real_docker_container_existence_distinguishes_not_found_from_daemon_failure(
    monkeypatch,
):
    try:
        engine = CliOciEngine.auto()
    except EngineUnavailableError as exc:
        pytest.skip(f"OCI integration unavailable: {exc}")
    if engine.kind != "docker":
        pytest.skip("real existence classification regression requires Docker")
    reachable, detail = engine.daemon_status()
    if not reachable:
        pytest.skip(f"Docker daemon is not reachable: {detail}")

    missing = f"atv-definitely-missing-{secrets.token_hex(8)}"
    assert engine.container_exists(missing) is False

    monkeypatch.setenv("DOCKER_HOST", "tcp://127.0.0.1:1")
    with pytest.raises(OciRunnerError, match="existence probe"):
        engine.container_exists(missing)


def test_real_engine_networkless_harness_then_hidden_grader(tmp_path):
    engine, package, image = _engine_package_image_or_skip()
    expected_config = (package.public_workspace / "config.json").read_bytes()

    request = OciTrialRequest(
        attempt=_attempt(package),
        task=package,
        task_image=image,
        harness_image=image,
        grader_image=image,
        storage_mode=OciStorageMode.HARD_QUOTA,
        harness_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "assert pathlib.Path('/workspace/config.json').read_bytes()"
                f"=={expected_config!r};"
                "pathlib.Path('/workspace/harness-output.txt').write_text("
                "'workspace-persisted',encoding='utf-8');"
                "pathlib.Path('/artifacts/harness-artifact.txt').write_text("
                "'artifact-persisted',encoding='utf-8')"
            ),
        ),
        grader_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "assert pathlib.Path('/trusted/grader.json').is_file();"
                "assert pathlib.Path('/output/config.json').is_file();"
                "assert pathlib.Path('/output/harness-output.txt').read_text()"
                "=='workspace-persisted';"
                "assert pathlib.Path('/harness-artifacts/harness-artifact.txt')"
                ".read_text()=='artifact-persisted';"
                "print('grader-ok')"
            ),
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.runtime_verified is True
    assert result.evidence.official_verified is False
    assert b"grader-ok" in result.grader_stdout
    assert result.evidence.harness.cleanup.confirmed_absent is True
    assert result.evidence.grader.cleanup.confirmed_absent is True
    assert result.evidence.seed is not None
    assert result.evidence.output_capture is not None
    seed = result.evidence.workspace["seed"]
    assert seed["verified"] is True
    assert seed["source_digest"] == seed["seeded_digest"]
    assert result.evidence.official_eligible is False
    assert "hard_aggregate_storage_quota_unavailable" not in (
        result.evidence.official_ineligibility_reasons
    )
    assert result.evidence.workspace["output_capture"]["workspace"][
        "file_count"
    ] >= 2
    assert result.evidence.workspace["output_capture"]["artifacts"][
        "file_count"
    ] == 1
    assert result.evidence.phase_order.index("seed_container_removed") < (
        result.evidence.phase_order.index("harness_started")
    )
    assert result.evidence.phase_order.index("harness_container_removed") < (
        result.evidence.phase_order.index("grader_started")
    )
    assert result.evidence.phase_order.index("grader_container_removed") < (
        result.evidence.phase_order.index("volume_keeper_removed")
    )
    _assert_hard_quota_volumes_removed(engine, result)


def test_real_engine_timeout_force_removes_exact_container_before_grader(tmp_path):
    engine, package, image = _engine_package_image_or_skip()
    resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        wall_time_ms=500,
    )
    request = OciTrialRequest(
        attempt=_attempt(package),
        task=package,
        task_image=image,
        harness_image=image,
        grader_image=image,
        harness_resources=resources,
        storage_mode=OciStorageMode.HARD_QUOTA,
        harness_command=("python", "-c", "import time; time.sleep(60)"),
        grader_command=(
            "python",
            "-c",
                (
                    "import pathlib;"
                    "assert pathlib.Path('/trusted/grader.json').is_file();"
                    "assert pathlib.Path('/output/config.json').is_file();"
                    "print('grader-after-timeout-ok')"
                ),
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.TIMED_OUT
    assert result.evidence.harness.run.timed_out is True
    assert result.evidence.harness.cleanup.confirmed_absent is True
    assert result.evidence.harness.cleanup.succeeded is True
    assert result.evidence.grader.cleanup.confirmed_absent is True
    assert b"grader-after-timeout-ok" in result.grader_stdout
    assert not engine.container_exists(
        result.evidence.harness.cleanup.container_name
    )
    _assert_hard_quota_volumes_removed(engine, result)


def test_real_engine_hard_quota_reports_aggregate_enospc_and_grades_output(
    tmp_path,
):
    engine, package, image = _engine_package_image_or_skip()
    resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        storage_bytes=1024 * 1024,
        artifact_bytes=256 * 1024,
        tmpfs_bytes=256 * 1024,
    )
    request = OciTrialRequest(
        attempt=_attempt(package),
        task=package,
        task_image=image,
        harness_image=image,
        grader_image=image,
        storage_mode=OciStorageMode.HARD_QUOTA,
        harness_resources=resources,
        harness_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "pathlib.Path('/workspace/chunk-a.bin').write_bytes(b'a'*700000);"
                "pathlib.Path('/workspace/chunk-b.bin').write_bytes(b'b'*700000)"
            ),
        ),
        grader_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "assert pathlib.Path('/trusted/grader.json').is_file();"
                "assert pathlib.Path('/output/config.json').is_file();"
                "assert pathlib.Path('/output/chunk-a.bin').stat().st_size==700000;"
                "print('grader-read-quota-output')"
            ),
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.STORAGE_FAILED
    assert result.evidence.harness.run.exit_code not in (0, None)
    assert result.evidence.harness.storage.hard_storage_enforced is True
    assert result.evidence.harness.storage.exceeded is True
    assert result.evidence.harness.storage.monitor_succeeded is True
    assert b"No space left on device" in result.harness_stderr
    assert b"grader-read-quota-output" in result.grader_stdout
    assert result.evidence.grader is not None
    _assert_hard_quota_volumes_removed(engine, result)


def test_real_engine_pid_bomb_is_contained_and_cleaned(tmp_path):
    engine, package, image = _engine_package_image_or_skip()
    resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        pids_limit=24,
        wall_time_ms=10_000,
    )
    request = OciTrialRequest(
        attempt=_attempt(package),
        task=package,
        task_image=image,
        harness_image=image,
        grader_image=image,
        storage_mode=OciStorageMode.HARD_QUOTA,
        harness_resources=resources,
        harness_command=(
            "python",
            "-c",
            (
                "import pathlib,subprocess,sys;"
                "children=[];limited=False;"
                "\ntry:\n"
                "  for _ in range(200):\n"
                "    children.append(subprocess.Popen([sys.executable,'-c',"
                "'import time;time.sleep(30)']))\n"
                "except OSError:\n"
                "  limited=True\n"
                "pathlib.Path('/workspace/pids-contained.txt').write_text("
                "str(limited),encoding='utf-8');"
                "assert limited"
            ),
        ),
        grader_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "assert pathlib.Path('/output/pids-contained.txt').read_text()"
                "=='True';"
                "print('pid-bomb-contained')"
            ),
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.harness.inspection.pids_limit == 24
    assert result.evidence.harness.cleanup.confirmed_absent is True
    assert b"pid-bomb-contained" in result.grader_stdout
    _assert_hard_quota_volumes_removed(engine, result)


def test_real_engine_memory_bomb_is_oom_contained_and_cleaned(tmp_path):
    engine, package, image = _engine_package_image_or_skip()
    resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        memory_bytes=64 * 1024 * 1024,
        wall_time_ms=10_000,
    )
    request = OciTrialRequest(
        attempt=_attempt(package),
        task=package,
        task_image=image,
        harness_image=image,
        grader_image=image,
        storage_mode=OciStorageMode.HARD_QUOTA,
        harness_resources=resources,
        harness_command=(
            "python",
            "-c",
            "x=bytearray(512*1024*1024);print(len(x))",
        ),
        grader_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "assert pathlib.Path('/trusted/grader.json').is_file();"
                "assert pathlib.Path('/output/config.json').is_file();"
                "print('memory-bomb-contained')"
            ),
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.NONZERO_EXIT
    assert result.evidence.harness.run.exit_code not in (0, None)
    assert result.evidence.harness.inspection.memory_bytes == 64 * 1024 * 1024
    assert result.evidence.harness.cleanup.confirmed_absent is True
    assert b"memory-bomb-contained" in result.grader_stdout
    _assert_hard_quota_volumes_removed(engine, result)


def test_real_engine_output_bomb_is_streamed_bounded_and_cleaned(tmp_path):
    engine, package, image = _engine_package_image_or_skip()
    resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        stdout_bytes=64 * 1024,
        wall_time_ms=10_000,
    )
    request = OciTrialRequest(
        attempt=_attempt(package),
        task=package,
        task_image=image,
        harness_image=image,
        grader_image=image,
        storage_mode=OciStorageMode.HARD_QUOTA,
        harness_resources=resources,
        harness_command=(
            "python",
            "-c",
            "import os;os.write(1,b'x'*(8*1024*1024))",
        ),
        grader_command=(
            "python",
            "-c",
            "print('output-bomb-contained')",
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.harness.run.stdout_total_bytes >= 8 * 1024 * 1024
    assert len(result.harness_stdout) <= resources.stdout_bytes
    assert result.evidence.harness.run.stdout_truncated is True
    assert result.evidence.harness.cleanup.confirmed_absent is True
    assert b"output-bomb-contained" in result.grader_stdout
    _assert_hard_quota_volumes_removed(engine, result)


def test_real_internal_network_requires_exact_named_gateway_sidecar(tmp_path):
    engine, package, image = _engine_package_image_or_skip()
    suffix = secrets.token_hex(4)
    network = f"atv-gateway-net-{suffix}"
    gateway = f"atv-gateway-{suffix}"
    create = subprocess.run(
        [engine.executable, "network", "create", "--internal", network],
        capture_output=True,
        text=True,
        check=False,
    )
    assert create.returncode == 0, create.stderr
    sidecar = subprocess.run(
        [
            engine.executable,
            "run",
            "-d",
            "--name",
            gateway,
            "--network",
            network,
            "--user",
            "65534:65534",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            image.reference,
            "python",
            "-c",
            "import time; time.sleep(60)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        assert sidecar.returncode == 0, sidecar.stderr
        attempt = _attempt(package)
        broker = CredentialBroker()
        handle = broker.issue_trial(
            TrialPolicy(
                trial_id=attempt.spec.trial_id,
                attempt_id=attempt.attempt_id,
                allowed_route_ids=("route",),
                budget=TrialBudget(1, 1, 1, 2, 1),
            ),
            ttl_seconds=60,
        )
        result = OciTrialRunner(engine, work_root=tmp_path).run(
            OciTrialRequest(
                attempt=attempt,
                task=package,
                task_image=image,
                harness_image=image,
                grader_image=image,
                storage_mode=OciStorageMode.HARD_QUOTA,
                harness_command=(
                    "python",
                    "-c",
                    (
                        "import pathlib;"
                        "assert pathlib.Path('/workspace/config.json').is_file()"
                    ),
                ),
                grader_command=(
                    "python",
                    "-c",
                    (
                        "import pathlib;"
                        "assert pathlib.Path('/trusted/grader.json').is_file();"
                        "assert pathlib.Path('/output/config.json').is_file();"
                        "print('gateway-grader-ok')"
                    ),
                ),
                network=OciNetworkPolicy.model_gateway_only(
                    network,
                    allowed_gateway_identities=(gateway,),
                ),
                gateway_handle=handle,
                credential_broker=broker,
            )
        )
        assert result.status is OciTrialStatus.COMPLETED
        assert result.evidence.runtime_verified is True
        assert result.evidence.network["peer_set_verified"] is True
        assert result.evidence.network["before"]["endpoints"][0]["identity"] == gateway
        assert result.evidence.network["after"]["endpoints"][0]["identity"] == gateway
        assert result.evidence.handle_action == "completed"
        assert b"gateway-grader-ok" in result.grader_stdout
        _assert_hard_quota_volumes_removed(engine, result)
    finally:
        subprocess.run(
            [engine.executable, "rm", "-f", gateway],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            [engine.executable, "network", "rm", network],
            capture_output=True,
            check=False,
        )
