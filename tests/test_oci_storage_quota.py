"""Focused aggregate-storage quota security and Docker integration tests."""
from __future__ import annotations

from dataclasses import replace

import pytest

import atv_bench.sandbox.oci as oci_module
from atv_bench.sandbox import (
    ContainerInspection,
    ContainerPhase,
    ContainerSpec,
    DigestPinnedImage,
    InspectedMount,
    MountSpec,
    OciNetworkPolicy,
    OciResourcePolicy,
    OciStorageMode,
    OciTrialRequest,
    OciTrialRunner,
    OciTrialStatus,
    build_run_argv,
)
from tests.test_oci_runner import (
    FakeEngine,
    FakeHardQuotaEngine,
    _package,
    _request,
)
from tests.test_oci_runner_integration import (
    _assert_hard_quota_volumes_removed,
    _attempt,
    _engine_package_image_or_skip,
)


def _resources(storage_bytes: int = 2 * 1024 * 1024) -> OciResourcePolicy:
    return OciResourcePolicy(
        wall_time_ms=10_000,
        memory_bytes=128 * 1024 * 1024,
        cpu_millis=1_000,
        pids_limit=32,
        storage_bytes=storage_bytes,
        stdout_bytes=1024 * 1024,
        stderr_bytes=1024 * 1024,
        artifact_bytes=storage_bytes,
        tmpfs_bytes=min(storage_bytes, 256 * 1024),
    )


def _aggregate_spec() -> ContainerSpec:
    image = DigestPinnedImage.parse("python@sha256:" + "1" * 64)
    volume = "atv-test-aggregate"
    return ContainerSpec(
        phase=ContainerPhase.HARNESS,
        name="atv-test-harness",
        image=image,
        command=("python", "-c", "print('ok')"),
        mounts=(
            MountSpec.volume(
                volume,
                "/workspace",
                False,
                subpath="workspace",
                no_copy=True,
            ),
            MountSpec.volume(
                volume,
                "/artifacts",
                False,
                subpath="artifacts",
                no_copy=True,
            ),
            MountSpec.volume(
                volume,
                "/tmp",
                False,
                subpath="tmp",
                no_copy=True,
            ),
        ),
        resources=_resources(),
        network=OciNetworkPolicy.none(),
        working_directory="/workspace",
    )


def test_quota_argv_uses_one_volume_subpaths_and_disables_default_shm():
    spec = _aggregate_spec()
    argv = build_run_argv("docker", spec)

    assert spec.policy_dict()["storage_enforcement"] == {
        "aggregate": oci_module._HARD_QUOTA_ENFORCEMENT,
        "hard_aggregate_quota_requested": True,
        "hard_aggregate_quota": True,
        "per_file_rlimit_fsize": True,
        "official_eligible_if_runtime_verified_and_cleaned": True,
    }
    assert argv[argv.index("--ipc") + 1] == "none"
    tmpfs_values = [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "--tmpfs"
    ]
    assert not any(value.startswith("/tmp:") for value in tmpfs_values)
    mount_values = [
        argv[index + 1]
        for index, value in enumerate(argv[:-1])
        if value == "--mount"
    ]
    assert len(mount_values) == 3
    assert all("src=atv-test-aggregate" in value for value in mount_values)
    assert {
        next(
            token.split("=", 1)[1]
            for token in value.split(",")
            if token.startswith("volume-subpath=")
        )
        for value in mount_values
    } == {"workspace", "artifacts", "tmp"}
    assert all("volume-nocopy" in value for value in mount_values)


def test_runtime_inspection_rejects_ipc_subpath_and_alternate_mount_drift():
    spec = _aggregate_spec()
    inspection = ContainerInspection.expected(spec)

    assert "ipc_mode" in replace(
        inspection,
        ipc_mode="private",
    ).mismatches(spec)

    wrong_subpath = replace(
        inspection.mounts[0],
        volume_subpath="other",
    )
    assert "mount_policy" in replace(
        inspection,
        mounts=(wrong_subpath, *inspection.mounts[1:]),
    ).mismatches(spec)

    alternate = InspectedMount(
        source="escape-volume",
        destination="/escape",
        read_only=False,
        mount_type="volume",
        volume_subpath="escape",
        volume_nocopy=True,
    )
    assert "mount_policy" in replace(
        inspection,
        mounts=(*inspection.mounts, alternate),
    ).mismatches(spec)


def test_declared_image_volume_fails_before_untrusted_container_start(tmp_path):
    class DeclaredVolumeEngine(FakeEngine):
        def inspect_image(self, image):
            return replace(
                super().inspect_image(image),
                declared_volumes=("/escape",),
            )

    package = _package()
    engine = DeclaredVolumeEngine()
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert engine.specs == []
    assert any("implicit writable volumes" in error for error in result.evidence.errors)


def test_explicit_hard_quota_capability_failure_closes_before_start(tmp_path):
    package = _package()
    engine = FakeEngine()
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(package, storage_mode=OciStorageMode.HARD_QUOTA)
    )

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert engine.specs == []
    assert result.evidence.storage["selected_mode"] == "bind-monitor"
    assert result.evidence.storage["hard_storage_enforced"] is False


def test_auto_mode_does_not_fallback_after_hard_quota_verification_failure(
    tmp_path,
):
    package = _package()
    engine = FakeHardQuotaEngine(
        tmp_path / "fake-volumes",
        volume_inspection_mismatch=True,
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert engine.specs == []
    assert any("volume_options" in error for error in result.evidence.errors)
    assert all(
        volume["cleanup"]["confirmed_absent"]
        for volume in result.evidence.storage["volumes"]
    )
    assert list(engine.volume_root.iterdir()) == []


def test_unverifiable_named_volume_cleanup_fails_closed(tmp_path):
    class CleanupProbeFailureEngine(FakeHardQuotaEngine):
        def volume_exists(self, name):
            raise RuntimeError("daemon unavailable during cleanup verification")

    package = _package()
    engine = CleanupProbeFailureEngine(tmp_path / "fake-volumes")
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(package, storage_mode=OciStorageMode.HARD_QUOTA)
    )

    assert result.status is OciTrialStatus.CLEANUP_FAILED
    assert result.evidence.runtime_verified is False
    assert result.evidence.storage["hard_storage_cleanup_succeeded"] is False
    assert all(
        volume["cleanup"]["status"] == "failed"
        for volume in result.evidence.storage["volumes"]
    )


@pytest.mark.integration
def test_real_docker_quota_is_aggregate_across_workspace_artifacts_and_temp(
    tmp_path,
):
    engine, package, image = _engine_package_image_or_skip()
    resources = _resources(1024 * 1024)
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
                "import os,pathlib;"
                "assert not pathlib.Path('/dev/shm').exists();"
                "pathlib.Path('/workspace/a.bin').write_bytes(b'a'*300000);"
                "pathlib.Path('/artifacts/b.bin').write_bytes(b'b'*300000);"
                "os.symlink('/tmp','/tmp/alias');"
                "pathlib.Path('/tmp/alias/c.bin').write_bytes(b'c'*600000)"
            ),
        ),
        grader_command=(
            "python",
            "-c",
            (
                "import pathlib;"
                "assert pathlib.Path('/output/a.bin').stat().st_size==300000;"
                "assert pathlib.Path('/harness-artifacts/b.bin').stat().st_size"
                "==300000;"
                "print('aggregate-quota-grader-ok')"
            ),
        ),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.STORAGE_FAILED
    assert result.evidence.harness.run.exit_code not in (0, None)
    assert b"No space left on device" in result.harness_stderr
    assert result.evidence.harness.storage.hard_storage_enforced is True
    assert result.evidence.harness.storage.peak_bytes == 0
    assert result.evidence.harness.storage.enforcement == (
        oci_module._HARD_QUOTA_ENFORCEMENT
    )
    assert result.evidence.harness.inspection.ipc_mode == "none"
    assert result.evidence.harness.inspection.tmpfs == {}
    assert b"aggregate-quota-grader-ok" in result.grader_stdout
    mounts = result.evidence.harness.inspection.mounts
    assert len({mount.source for mount in mounts if not mount.read_only}) == 1
    assert {
        mount.volume_subpath for mount in mounts if not mount.read_only
    } == {"workspace", "artifacts", "tmp"}
    _assert_hard_quota_volumes_removed(engine, result)


@pytest.mark.integration
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_real_docker_output_links_cannot_bypass_verified_capture(
    tmp_path,
    link_kind,
):
    engine, package, image = _engine_package_image_or_skip()
    if link_kind == "symlink":
        link_command = "os.symlink('/tmp','/workspace/link')"
    else:
        link_command = (
            "pathlib.Path('/workspace/base.bin').write_bytes(b'x'*4096);"
            "os.link('/workspace/base.bin','/workspace/link.bin')"
        )
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
            f"import os,pathlib;{link_command}",
        ),
        grader_command=("python", "-c", "raise AssertionError('must not grade')"),
        network=OciNetworkPolicy.none(),
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.INVALID_OUTPUT
    assert result.evidence.harness.storage.hard_storage_enforced is True
    assert result.evidence.grader is None
    assert any(
        marker in error
        for error in result.evidence.errors
        for marker in ("unsafe directory", "link, hardlink, or special file")
    )
    _assert_hard_quota_volumes_removed(engine, result)
