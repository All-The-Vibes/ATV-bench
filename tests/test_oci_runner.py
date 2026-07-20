"""Hermetic OCI runner policy, lifecycle, and evidence tests."""
from __future__ import annotations

import json
import shutil
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

import atv_bench.sandbox.oci as oci_module
from atv_bench.eval import (
    Budget,
    BudgetProfile,
    HarnessRef,
    ModelPolicyRef,
    TaskPackage,
    TrialAttempt,
    TrialSpec,
)
from atv_bench.protocol import (
    JsonlProtocolParser,
    ProtocolSession,
    SessionState,
    canonical_json_bytes,
    canonical_jsonl,
)
from atv_bench.sandbox import (
    CleanupStatus,
    CliOciEngine,
    ContainerInspection,
    ContainerPhase,
    EngineIdentity,
    EngineRunResult,
    ImageInspection,
    ImageReferenceError,
    ImageRolePolicyError,
    NetworkEndpoint,
    NetworkInspection,
    NetworkPolicyError,
    OciNetworkPolicy,
    OciResourcePolicy,
    OciRunnerError,
    OciStorageMode,
    OciTrialRequest,
    OciTrialRunner,
    OciTrialStatus,
    OciTrack,
    VolumeInspection,
    build_run_argv,
)
from atv_bench.sandbox.interactive import (
    InteractiveCleanupEvidence,
    InteractiveOciEvidence,
    InteractiveOciResult,
    InteractiveTransportStatus,
)
from atv_bench.security import (
    CredentialBroker,
    TrialBudget,
    TrialPolicy,
)

pytest_plugins = ("tests.protocol.conftest",)

SMOKE_ROOT = Path(__file__).resolve().parents[1] / "tasks" / "smoke" / "repair_config"
PROVIDER_SECRET_CANARY = "ATV_PROVIDER_SECRET_CANARY_DO_NOT_LEAK"


def _package() -> TaskPackage:
    return TaskPackage.load(SMOKE_ROOT)


def _attempt(package: TaskPackage) -> TrialAttempt:
    spec = TrialSpec(
        benchmark_release="ATV-2026.09",
        protocol_version="atv.trial/v1",
        schedule_id="1" * 64,
        task=package.task_ref,
        harness=HarnessRef("fake-harness", "1.0.0", "2" * 64),
        model_policy=ModelPolicyRef("controlled", "1.0.0", "3" * 64),
        budget_profile=BudgetProfile(
            "smoke",
            Budget(
                wall_time_seconds=60,
                max_model_tokens=20_000,
                max_model_calls=20,
                max_cost_microusd=500_000,
            ),
        ),
        repetition=0,
        schedule_seed=7,
    )
    return TrialAttempt(spec=spec, attempt_number=1, fresh_nonce="4" * 64)


def _run_result(
    executable: str,
    spec,
    *,
    exit_code: int = 0,
    timed_out: bool = False,
    cancelled: bool = False,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> EngineRunResult:
    return EngineRunResult(
        argv=build_run_argv(executable, spec),
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=25,
        stdout=stdout,
        stderr=stderr,
        stdout_total_bytes=len(stdout),
        stderr_total_bytes=len(stderr),
        stdout_truncated=False,
        stderr_truncated=False,
    )


def _cli_result(
    *,
    exit_code: int | None,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    cancelled: bool = False,
    stdout_truncated: bool = False,
    stderr_truncated: bool = False,
) -> EngineRunResult:
    return EngineRunResult(
        argv=("docker", "inspect", "atv-test"),
        exit_code=exit_code,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_ms=1,
        stdout=stdout,
        stderr=stderr,
        stdout_total_bytes=len(stdout),
        stderr_total_bytes=len(stderr),
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


def _cli_engine_with_result(
    monkeypatch,
    result: EngineRunResult,
) -> CliOciEngine:
    engine = object.__new__(CliOciEngine)
    engine.executable = "docker"
    engine.kind = "docker"
    monkeypatch.setattr(engine, "_execute", lambda *args, **kwargs: result)
    return engine


class FakeEngine:
    executable = "docker"

    def __init__(
        self,
        *,
        harness_exit: int = 0,
        harness_timeout: bool = False,
        harness_cancelled: bool = False,
        harness_stdout: bytes = b"",
        cleanup_failure_phase: ContainerPhase | None = None,
        inspection_mismatch_phase: ContainerPhase | None = None,
        image_verified: bool = True,
        harness_storage_bytes: int = 0,
        network_endpoints: tuple[str, ...] = (),
    ):
        self.harness_exit = harness_exit
        self.harness_timeout = harness_timeout
        self.harness_cancelled = harness_cancelled
        self.harness_stdout = harness_stdout
        self.cleanup_failure_phase = cleanup_failure_phase
        self.inspection_mismatch_phase = inspection_mismatch_phase
        self.image_verified = image_verified
        self.harness_storage_bytes = harness_storage_bytes
        self.network_endpoints = network_endpoints
        self.specs = []
        self.active = {}
        self.remove_calls = []
        self.env_file_contents = []
        self.harness_initial_snapshot = {}

    def identity(self):
        return EngineIdentity("fake", self.executable, "fake 1.0", "a" * 64)

    def inspect_image(self, image):
        return ImageInspection(
            reference=image.reference,
            requested_digest=image.digest,
            resolved_digest=image.digest if self.image_verified else None,
            image_id="sha256:" + "b" * 64,
            repo_digests=(image.reference,),
            verified=self.image_verified,
        )

    def inspect_network(self, name):
        return NetworkInspection(
            name,
            "network-1",
            "bridge",
            True,
            tuple(
                NetworkEndpoint(
                    container_id=f"id-{index}",
                    identity=identity,
                )
                for index, identity in enumerate(self.network_endpoints)
            ),
        )

    def run_container(self, spec, *, cancel_event=None):
        self.specs.append(spec)
        self.active[spec.name] = spec
        if spec.env_file is not None:
            self.env_file_contents.append(spec.env_file.read_text(encoding="utf-8"))
        mounts = {mount.destination: mount.source for mount in spec.mounts}
        if spec.phase is ContainerPhase.HARNESS:
            self.harness_initial_snapshot = {
                path.relative_to(mounts["/workspace"]).as_posix(): path.read_bytes()
                for path in mounts["/workspace"].rglob("*")
                if path.is_file()
            }
            if self.harness_storage_bytes:
                (mounts["/workspace"] / "storage.bin").write_bytes(
                    b"x" * self.harness_storage_bytes
                )
            return _run_result(
                self.executable,
                spec,
                exit_code=self.harness_exit,
                timed_out=self.harness_timeout,
                cancelled=self.harness_cancelled,
                stdout=self.harness_stdout,
            )
        assert "/trusted" in mounts
        assert (mounts["/trusted"] / "grader.json").is_file()
        assert "/output" in mounts
        return _run_result(
            self.executable,
            spec,
            stdout=b'{"passed":true,"score":1.0}\n',
        )

    def inspect_container(self, name):
        spec = self.active[name]
        inspection = ContainerInspection.expected(spec)
        if spec.phase is self.inspection_mismatch_phase:
            return replace(inspection, read_only_rootfs=False)
        return inspection

    def remove_container(self, name, *, force):
        self.remove_calls.append((name, force))
        spec = self.active.get(name)
        if spec is None:
            return EngineRunResult(
                argv=(self.executable, "rm", "-f", name),
                exit_code=1,
                timed_out=False,
                cancelled=False,
                duration_ms=1,
                stdout=b"",
                stderr=b"not found",
                stdout_total_bytes=0,
                stderr_total_bytes=9,
                stdout_truncated=False,
                stderr_truncated=False,
            )
        if spec.phase is not self.cleanup_failure_phase:
            self.active.pop(name, None)
        return EngineRunResult(
            argv=(self.executable, "rm", "-f", name),
            exit_code=0,
            timed_out=False,
            cancelled=False,
            duration_ms=1,
            stdout=b"",
            stderr=b"",
            stdout_total_bytes=0,
            stderr_total_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
        )

    def container_exists(self, name):
        return name in self.active


class UnverifiableCleanupEngine(FakeEngine):
    def container_exists(self, name):
        raise OciRunnerError(f"permission denied while inspecting {name}")


class ReplayInteractiveTransport:
    def __init__(self, engine: FakeEngine, stdout: bytes):
        self.engine = engine
        self.stdout = stdout

    def run(
        self,
        spec,
        session,
        *,
        controller=None,
        cancel_event=None,
        before_cleanup=None,
    ):
        del controller, cancel_event
        self.engine.specs.append(spec)
        self.engine.active[spec.name] = spec
        tick = 0

        def timestamp():
            nonlocal tick
            value = f"2026-07-20T00:00:{tick:02d}Z"
            tick += 1
            return value

        for line in self.stdout.splitlines(keepends=True):
            session.receive_harness_line(
                line,
                recorded_at=timestamp(),
            )
            if session.state is SessionState.WAIT_ACCEPT:
                session.record_controller_accept(
                    recorded_at=timestamp()
                )
        transcript = session.finish()
        if before_cleanup is not None:
            before_cleanup(spec)
        remove = self.engine.remove_container(spec.name, force=True)
        confirmed_absent = not self.engine.container_exists(spec.name)
        cleanup = InteractiveCleanupEvidence(
            container_name=spec.name,
            remove_attempted=True,
            remove_exit_code=remove.exit_code,
            remove_argv_sha256=oci_module.sha256_bytes(
                canonical_json_bytes(list(remove.argv))
            ),
            confirmed_absent=confirmed_absent,
        )
        transcript_bytes = canonical_jsonl(transcript.events)
        evidence = InteractiveOciEvidence(
            container_name=spec.name,
            engine_kind="fake",
            image_digest=spec.image.digest,
            request_digest=dict(session.request_digest),
            run_argv_sha256=oci_module.sha256_bytes(
                canonical_json_bytes(["docker", "run", "--interactive"])
            ),
            request_written=True,
            accepted_written=True,
            accepted_request_digest_matches=True,
            controller_events=("accepted",),
            termination_actions=("force_remove",),
            cancel_grace_period_ms=None,
            hard_kill_exit_code=None,
            hard_kill_argv_sha256=None,
            harness_event_count=len(
                [
                    event
                    for event in transcript.events
                    if event["source"] == "harness"
                ]
            ),
            stdout_total_bytes=len(self.stdout),
            stderr_total_bytes=0,
            stderr_sha256=oci_module.sha256_bytes(b""),
            stderr_limit_exceeded=False,
            process_exit_code=0,
            eof_observed=True,
            pipe_leak_detected=False,
            cleanup=cleanup,
            transcript_sha256=oci_module.sha256_bytes(transcript_bytes),
            authority_verified=confirmed_absent,
            started_at="2026-07-20T00:00:00Z",
            ended_at="2026-07-20T00:00:02Z",
            duration_ms=2,
            error_code=None,
        )
        return InteractiveOciResult(
            status=InteractiveTransportStatus.COMPLETED,
            transcript=transcript,
            evidence=evidence,
            stdout=self.stdout,
            stderr=b"",
        )


def _apply_tree_transfer(data: bytes, destination: Path) -> tuple[str, int, int]:
    header_bytes, payload = data.split(b"\n", 1)
    header = json.loads(header_bytes.decode("utf-8"))
    assert header["schema"] == "atv.oci-confined-tree-transfer/v1"
    offset = 0
    for row in header["files"]:
        size = int(row["size"])
        chunk = payload[offset : offset + size]
        assert len(chunk) == size
        assert oci_module.sha256_bytes(chunk) == row["sha256"]
        target = destination.joinpath(*row["path"].split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(chunk)
        offset += size
    assert offset == len(payload)
    snapshots = oci_module._snapshot_seed_tree(
        destination,
        max_files=100_000,
        max_total_bytes=max(1, len(payload) + 1),
    )
    digest = oci_module._seed_tree_digest(snapshots)
    assert digest == header["expected_digest"]
    return digest, len(snapshots), sum(item.size for item in snapshots)


class FakeHardQuotaEngine(FakeEngine):
    def __init__(
        self,
        volume_root: Path,
        *,
        hard_enospc: bool = False,
        volume_inspection_mismatch: bool = False,
    ):
        super().__init__()
        self.volume_root = volume_root
        self.volume_root.mkdir(parents=True, exist_ok=True)
        self.hard_enospc = hard_enospc
        self.volume_inspection_mismatch = volume_inspection_mismatch
        self.volume_specs = {}
        self.volume_dirs = {}
        self.volume_remove_calls = []
        self.stdin_payload_digests = []
        self.grader_observed_output = {}

    @staticmethod
    def _result(argv, *, exit_code=0, stdout=b"", stderr=b""):
        return EngineRunResult(
            argv=tuple(str(value) for value in argv),
            exit_code=exit_code,
            timed_out=False,
            cancelled=False,
            duration_ms=2,
            stdout=stdout,
            stderr=stderr,
            stdout_total_bytes=len(stdout),
            stderr_total_bytes=len(stderr),
            stdout_truncated=False,
            stderr_truncated=False,
        )

    def hard_quota_volume_capability(self):
        return True, "fake exact tmpfs named-volume capability"

    def create_volume(self, spec):
        path = self.volume_root / spec.name
        path.mkdir()
        self.volume_specs[spec.name] = spec
        self.volume_dirs[spec.name] = path
        return self._result(spec.create_argv(self.executable), stdout=spec.name.encode())

    def inspect_volume(self, name):
        spec = self.volume_specs[name]
        options = {
            "type": spec.filesystem_type,
            "device": spec.device,
            "o": spec.mount_options,
        }
        if self.volume_inspection_mismatch and spec.purpose == "harness-aggregate":
            options["o"] = "size=1"
        return VolumeInspection(
            name=name,
            driver=spec.driver,
            options=options,
            mountpoint=f"/fake-volumes/{name}",
            scope="local",
        )

    def remove_volume(self, name, *, force):
        self.volume_remove_calls.append((name, force))
        path = self.volume_dirs.pop(name, None)
        self.volume_specs.pop(name, None)
        if path is not None:
            shutil.rmtree(path)
        return self._result((self.executable, "volume", "rm", "-f", name))

    def volume_exists(self, name):
        return name in self.volume_dirs

    def copy_to_container(self, name, source, destination):
        raise AssertionError("hard mode must not use docker/podman cp")

    def exec_container(
        self,
        name,
        command,
        *,
        user,
        working_directory,
        resources,
        cancel_event=None,
        stdin_data=None,
    ):
        raise AssertionError("hard mode must use one foreground trusted container")

    def _mounts(self, spec):
        resolved = {}
        for mount in spec.mounts:
            if mount.mount_type == "volume":
                path = self.volume_dirs[str(mount.source)]
                if mount.volume_subpath is not None:
                    path = path / mount.volume_subpath
                resolved[mount.destination] = path
            else:
                resolved[mount.destination] = Path(mount.source)
        return resolved

    def run_container(self, spec, *, cancel_event=None, stdin_data=None):
        self.specs.append(spec)
        self.active[spec.name] = spec
        mounts = self._mounts(spec)
        if stdin_data is not None:
            self.stdin_payload_digests.append(oci_module.sha256_bytes(stdin_data))
        if spec.phase is ContainerPhase.VOLUME_KEEPER:
            assert spec.detached is True
            return _run_result(self.executable, spec, stdout=b"keeper-id\n")
        if spec.phase is ContainerPhase.SEED:
            assert stdin_data is not None
            harness_root = mounts[oci_module._HARD_QUOTA_HARNESS_ROOT]
            grader_root = mounts[oci_module._HARD_QUOTA_GRADER_ROOT]
            for root, subpaths in (
                (harness_root, oci_module._HARD_QUOTA_HARNESS_SUBPATHS),
                (grader_root, oci_module._HARD_QUOTA_GRADER_SUBPATHS),
            ):
                for subpath in subpaths:
                    (root / subpath).mkdir()
            digest, files, total = _apply_tree_transfer(
                stdin_data,
                harness_root / "workspace",
            )
            assert not any((harness_root / "artifacts").iterdir())
            harness_volume = next(
                item
                for item in spec.mounts
                if item.destination == oci_module._HARD_QUOTA_HARNESS_ROOT
            )
            grader_volume = next(
                item
                for item in spec.mounts
                if item.destination == oci_module._HARD_QUOTA_GRADER_ROOT
            )
            harness_spec = self.volume_specs[str(harness_volume.source)]
            grader_spec = self.volume_specs[str(grader_volume.source)]
            payload = canonical_json_bytes(
                {
                    "schema": "atv.oci-workspace-seed/v1",
                    "expected_digest": digest,
                    "seeded_digest": digest,
                    "file_count": files,
                    "total_bytes": total,
                    "quota_filesystems": {
                        "harness": {
                            "root": oci_module._HARD_QUOTA_HARNESS_ROOT,
                            "filesystem_type": "tmpfs",
                            "capacity_bytes": harness_spec.size_bytes,
                            "device": str(harness_root.stat().st_dev),
                            "subpaths": list(harness_spec.subpaths),
                        },
                        "grader": {
                            "root": oci_module._HARD_QUOTA_GRADER_ROOT,
                            "filesystem_type": "tmpfs",
                            "capacity_bytes": grader_spec.size_bytes,
                            "device": str(grader_root.stat().st_dev),
                            "subpaths": list(grader_spec.subpaths),
                        },
                    },
                }
            ) + b"\n"
            return _run_result(self.executable, spec, stdout=payload)
        if spec.phase is ContainerPhase.HARNESS:
            workspace = mounts["/workspace"]
            self.harness_initial_snapshot = {
                path.relative_to(workspace).as_posix(): path.read_bytes()
                for path in workspace.rglob("*")
                if path.is_file()
            }
            (workspace / "harness.txt").write_bytes(b"fake-output")
            if self.hard_enospc:
                (workspace / "chunk-a.bin").write_bytes(b"a" * 700_000)
                (workspace / "chunk-b.bin").write_bytes(b"b" * 300_000)
                return _run_result(
                    self.executable,
                    spec,
                    exit_code=1,
                    stderr=b"OSError: [Errno 28] No space left on device\n",
                )
            return _run_result(
                self.executable,
                spec,
                stdout=self.harness_stdout,
            )
        if spec.phase is ContainerPhase.OUTPUT_CAPTURE:
            workspace_rows = oci_module._snapshot_seed_tree(
                mounts["/output"],
                max_files=100_000,
                max_total_bytes=2**31,
            )
            artifact_rows = oci_module._snapshot_seed_tree(
                mounts["/harness-artifacts"],
                max_files=100_000,
                max_total_bytes=2**31,
            )
            payload = canonical_json_bytes(
                {
                    "schema": "atv.oci-output-capture/v1",
                    "workspace": {
                        "file_count": len(workspace_rows),
                        "total_bytes": sum(item.size for item in workspace_rows),
                        "tree_digest": oci_module._seed_tree_digest(workspace_rows),
                    },
                    "artifacts": {
                        "file_count": len(artifact_rows),
                        "total_bytes": sum(item.size for item in artifact_rows),
                        "tree_digest": oci_module._seed_tree_digest(artifact_rows),
                    },
                }
            ) + b"\n"
            return _run_result(self.executable, spec, stdout=payload)
        assert spec.phase is ContainerPhase.GRADER
        assert stdin_data is not None
        trusted = self.volume_root / f"trusted-{spec.name}"
        trusted.mkdir()
        _apply_tree_transfer(stdin_data, trusted)
        assert (trusted / "grader.json").is_file()
        output = mounts["/output"]
        assert (output / "config.json").is_file()
        assert (output / "harness.txt").read_bytes() == b"fake-output"
        self.grader_observed_output = {
            path.relative_to(output).as_posix(): path.read_bytes()
            for path in output.rglob("*")
            if path.is_file()
        }
        return _run_result(
            self.executable,
            spec,
            stdout=b'{"passed":true,"score":1.0}\n',
        )

    def remove_container(self, name, *, force):
        result = super().remove_container(name, force=force)
        trusted = self.volume_root / f"trusted-{name}"
        if trusted.exists():
            shutil.rmtree(trusted)
        return result


def _request(package: TaskPackage, **overrides) -> OciTrialRequest:
    values = {
        "attempt": _attempt(package),
        "task": package,
        "harness_image": package.manifest["environment"]["image"],
        "harness_command": ("python", "-c", "print('harness')"),
        "network": OciNetworkPolicy.none(),
    }
    values.update(overrides)
    return OciTrialRequest(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("harness_image", "python:3.12"),
        ("harness_image", "python:3.12@sha256:" + "1" * 64),
        ("task_image", "python:latest"),
        ("grader_image", "python@sha256:not-a-digest"),
    ],
)
def test_mutable_or_malformed_images_are_rejected(field, value):
    package = _package()
    with pytest.raises(ImageReferenceError):
        _request(package, **{field: value})


@pytest.mark.parametrize("name", ["host", "bridge", "default", "none", ""])
def test_broad_or_default_gateway_networks_are_rejected(name):
    with pytest.raises(NetworkPolicyError):
        OciNetworkPolicy.model_gateway_only(
            name,
            allowed_gateway_identities=("gateway",),
        )


def test_gateway_network_rejects_broad_dns_or_missing_identity():
    with pytest.raises(NetworkPolicyError, match="identities"):
        OciNetworkPolicy.model_gateway_only(
            "private-gateway-network",
            allowed_gateway_identities=(),
        )
    with pytest.raises(NetworkPolicyError, match="DNS"):
        OciNetworkPolicy(
            mode="model-gateway-only",
            network_name="private-gateway-network",
            dns_servers=("8.8.8.8",),
            allowed_gateway_identities=("gateway",),
        )


def test_controlled_rejects_unbound_task_and_harness_images():
    package = _package()
    other = "docker.io/library/other@sha256:" + "9" * 64
    with pytest.raises(ImageRolePolicyError, match="Controlled"):
        _request(package, harness_image=other)


def test_systems_allows_different_harness_image_but_labels_confound(tmp_path):
    package = _package()
    other = "docker.io/library/other@sha256:" + "9" * 64
    result = OciTrialRunner(FakeEngine(), work_root=tmp_path).run(
        _request(
            package,
            track=OciTrack.SYSTEMS,
            harness_image=other,
        )
    )
    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.image_roles["systems_confound"]
    assert result.evidence.official_eligible is False
    assert "systems_image_confound" in result.evidence.official_ineligibility_reasons


@pytest.mark.parametrize(
    "peers",
    [
        (),
        ("gateway", "unexpected-peer"),
        ("wrong-gateway",),
    ],
)
def test_gateway_network_requires_exact_declared_peer_set(tmp_path, peers):
    package = _package()
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
    result = OciTrialRunner(
        FakeEngine(network_endpoints=peers), work_root=tmp_path
    ).run(
        _request(
            package,
            attempt=attempt,
            network=OciNetworkPolicy.model_gateway_only(
                "private-gateway-network",
                allowed_gateway_identities=("gateway",),
            ),
            gateway_handle=handle,
            credential_broker=broker,
        )
    )
    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert result.evidence.runtime_verified is False
    assert result.evidence.harness is None


def test_harness_and_grader_are_separate_with_hidden_inputs_late_and_no_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ATV_PROVIDER_API_KEY", PROVIDER_SECRET_CANARY)
    package = _package()
    engine = FakeEngine()
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.runtime_verified is True
    assert result.evidence.official_verified is False
    assert result.evidence.official_eligible is False
    assert "hard_aggregate_storage_quota_unavailable" in (
        result.evidence.official_ineligibility_reasons
    )
    seed = result.evidence.workspace["seed"]
    assert seed["verified"] is True
    assert seed["expected_digest"] == package.manifest["source"]["tree_digest"]["value"]
    assert seed["source_digest"] == seed["seeded_digest"]
    assert engine.harness_initial_snapshot["config.json"] == (
        package.public_workspace / "config.json"
    ).read_bytes()
    assert len(result.evidence.images) == 3
    assert all(image.verified for image in result.evidence.images)
    assert len(engine.specs) == 2
    harness, grader = engine.specs
    assert harness.phase is ContainerPhase.HARNESS
    assert grader.phase is ContainerPhase.GRADER

    trusted = str((package.root / "trusted").resolve())
    harness_argv = list(build_run_argv(engine.executable, harness))
    grader_argv = list(build_run_argv(engine.executable, grader))
    assert trusted not in "\n".join(harness_argv)
    assert trusted in "\n".join(grader_argv)
    assert harness.network.mode.value == "none"
    assert grader.network.mode.value == "none"
    assert grader.env_file is None
    assert grader.environment_names == ()
    assert next(m for m in grader.mounts if m.destination == "/output").read_only
    assert next(m for m in grader.mounts if m.destination == "/trusted").read_only

    for spec in engine.specs:
        argv = list(build_run_argv(engine.executable, spec))
        assert "--read-only" in argv
        assert ["--cap-drop", "ALL"] == argv[
            argv.index("--cap-drop") : argv.index("--cap-drop") + 2
        ]
        assert "no-new-privileges:true" in argv
        for flag in (
            "--pids-limit",
            "--memory",
            "--cpus",
            "--ulimit",
        ):
            assert flag in argv
        assert next(
            mount for mount in spec.mounts if mount.destination == "/tmp"
        ).read_only is False
        assert "--storage-opt" not in argv
        assert "/var/run/docker.sock" not in "\n".join(argv)
        assert str(Path.home()) not in {
            str(mount.source) for mount in spec.mounts
        }

    serialized = json.dumps(result.evidence.to_dict(), sort_keys=True)
    assert PROVIDER_SECRET_CANARY not in serialized
    assert "ATV_PROVIDER_API_KEY" not in serialized
    assert result.evidence.phase_order.index("harness_exited") < (
        result.evidence.phase_order.index("grader_started")
    )
    assert result.evidence.canonical_bytes == canonical_json_bytes(
        result.evidence.to_dict()
    )
    assert result.evidence.digest == result.evidence.digest
    assert result.evidence.harness.storage.monitor_succeeded is True
    assert result.evidence.harness.storage.hard_storage_enforced is False
    assert result.evidence.grader.storage.monitor_succeeded is True
    assert result.evidence.grader.storage.hard_storage_enforced is False


def test_gateway_handle_only_reaches_harness_and_is_finalized_before_grader(
    tmp_path
):
    package = _package()
    attempt = _attempt(package)
    broker = CredentialBroker()
    broker.register_provider("provider", PROVIDER_SECRET_CANARY)
    handle = broker.issue_trial(
        TrialPolicy(
            trial_id=attempt.spec.trial_id,
            attempt_id=attempt.attempt_id,
            allowed_route_ids=("route",),
            budget=TrialBudget(2, 100, 100, 200, 1_000),
        ),
        ttl_seconds=60,
    )
    engine = FakeEngine(network_endpoints=("atv-gateway",))
    request = _request(
        package,
        attempt=attempt,
        network=OciNetworkPolicy.model_gateway_only(
            "atv-model-gateway-internal",
            allowed_gateway_identities=("atv-gateway",),
        ),
        gateway_handle=handle,
        credential_broker=broker,
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.handle_action == "completed"
    assert len(engine.env_file_contents) == 1
    assert handle.value in engine.env_file_contents[0]
    assert PROVIDER_SECRET_CANARY not in engine.env_file_contents[0]
    harness, grader = engine.specs
    assert harness.environment_names == ("ATV_MODEL_GATEWAY_HANDLE",)
    assert grader.environment_names == ()
    assert grader.env_file is None
    harness_argv = build_run_argv(engine.executable, harness)
    assert (
        harness_argv[harness_argv.index("--network") + 1]
        == "atv-model-gateway-internal"
    )
    assert "--dns" not in harness_argv
    grader_argv = build_run_argv(engine.executable, grader)
    assert grader_argv[grader_argv.index("--network") + 1] == "none"
    assert "--dns" not in grader_argv
    assert result.evidence.network["peer_set_verified"] is True
    assert result.evidence.phase_order.index("gateway_handle_completed") < (
        result.evidence.phase_order.index("grader_started")
    )
    visible = json.dumps(result.evidence.to_dict(), sort_keys=True)
    assert handle.value not in visible
    assert PROVIDER_SECRET_CANARY not in visible


def test_workspace_is_fresh_per_attempt_and_removed_after_each_run(tmp_path):
    package = _package()
    first_engine = FakeEngine()
    first = OciTrialRunner(first_engine, work_root=tmp_path).run(_request(package))
    second_engine = FakeEngine()
    second = OciTrialRunner(second_engine, work_root=tmp_path).run(_request(package))

    first_root = Path(first.evidence.workspace["ephemeral_root"])
    second_root = Path(second.evidence.workspace["ephemeral_root"])
    assert first_root != second_root
    assert not first_root.exists()
    assert not second_root.exists()
    first_workspace = next(
        mount.source
        for mount in first_engine.specs[0].mounts
        if mount.destination == "/workspace"
    )
    second_workspace = next(
        mount.source
        for mount in second_engine.specs[0].mounts
        if mount.destination == "/workspace"
    )
    assert first_workspace != second_workspace
    assert first_engine.harness_initial_snapshot == second_engine.harness_initial_snapshot
    assert first_engine.harness_initial_snapshot["config.json"] == (
        package.public_workspace / "config.json"
    ).read_bytes()


def test_seed_digest_mismatch_stops_before_container_start(tmp_path):
    copied = tmp_path / "task"
    shutil.copytree(SMOKE_ROOT, copied)
    package = TaskPackage.load(copied)
    request = _request(package)
    (package.public_workspace / "config.json").write_text(
        '{"tampered": true}\n', encoding="utf-8"
    )
    engine = FakeEngine()
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert engine.specs == []
    assert result.evidence.workspace["seed"] is None
    assert any("snapshot digest changed" in error for error in result.evidence.errors)


def test_seed_copy_detects_source_replacement_race(tmp_path, monkeypatch):
    copied = tmp_path / "task-race"
    shutil.copytree(SMOKE_ROOT, copied)
    package = TaskPackage.load(copied)
    request = _request(package)
    original = oci_module.read_confined_regular_file
    changed = False

    def racing_reader(root, relpath, *, max_bytes):
        nonlocal changed
        data = original(root, relpath, max_bytes=max_bytes)
        if not changed and str(relpath).replace("\\", "/") == "config.json":
            target = Path(root) / "config.json"
            target.write_bytes(target.read_bytes().replace(b"false", b"other"))
            changed = True
        return data

    monkeypatch.setattr(oci_module, "read_confined_regular_file", racing_reader)
    engine = FakeEngine()
    result = OciTrialRunner(engine, work_root=tmp_path).run(request)

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert engine.specs == []
    assert any("changed during snapshot" in error for error in result.evidence.errors)


def test_command_arguments_remain_single_shell_free_tokens(tmp_path):
    package = _package()
    injection = "; touch /tmp/atv-pwned"
    engine = FakeEngine()
    request = _request(
        package,
        harness_command=("python", "-c", "print('safe')", injection),
    )
    OciTrialRunner(engine, work_root=tmp_path).run(request)
    argv = build_run_argv(engine.executable, engine.specs[0])
    assert argv[-1] == injection
    assert argv.count(injection) == 1
    assert not (tmp_path / "atv-pwned").exists()


@pytest.mark.parametrize(
    ("engine_kwargs", "expected"),
    [
        ({"harness_timeout": True}, OciTrialStatus.TIMED_OUT),
        ({"harness_cancelled": True}, OciTrialStatus.CANCELLED),
        ({"harness_exit": 17}, OciTrialStatus.NONZERO_EXIT),
    ],
)
def test_timeout_cancel_and_nonzero_force_remove_exact_container(
    tmp_path, engine_kwargs, expected
):
    package = _package()
    engine = FakeEngine(**engine_kwargs)
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is expected
    assert result.evidence.harness is not None
    cleanup = result.evidence.harness.cleanup
    assert cleanup.attempted is True
    assert cleanup.status is CleanupStatus.SUCCEEDED
    assert cleanup.confirmed_absent is True
    harness_name = engine.specs[0].name
    assert (harness_name, True) in engine.remove_calls
    assert harness_name not in engine.active


@pytest.mark.parametrize(
    "stderr",
    [
        b"Error: No such object: atv-test",
        b"Error response from daemon: No such container: atv-test",
        b'Error: no container with name or ID "atv-test" found',
    ],
)
def test_cli_container_exists_accepts_only_explicit_not_found_as_absent(
    monkeypatch,
    stderr,
):
    engine = _cli_engine_with_result(
        monkeypatch,
        _cli_result(exit_code=1, stdout=b"[]\n", stderr=stderr),
    )

    assert engine.container_exists("atv-test") is False


@pytest.mark.parametrize(
    "result",
    [
        _cli_result(exit_code=1, stderr=b"permission denied"),
        _cli_result(
            exit_code=1,
            stderr=(
                b"error during connect: dial tcp 127.0.0.1:1: "
                b"connect: connection refused"
            ),
        ),
        _cli_result(exit_code=None, timed_out=True),
        _cli_result(
            exit_code=1,
            stderr=b"Error: No such object: atv-test",
            stderr_truncated=True,
        ),
    ],
)
def test_cli_container_exists_fails_closed_on_unverifiable_probe(
    monkeypatch,
    result,
):
    engine = _cli_engine_with_result(monkeypatch, result)

    with pytest.raises(OciRunnerError, match="existence probe"):
        engine.container_exists("atv-test")


def test_unverifiable_cleanup_probe_fails_closed_and_blocks_grader(tmp_path):
    package = _package()
    engine = UnverifiableCleanupEngine()
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.CLEANUP_FAILED
    assert result.evidence.runtime_verified is False
    assert result.evidence.harness is not None
    cleanup = result.evidence.harness.cleanup
    assert cleanup.status is CleanupStatus.FAILED
    assert cleanup.confirmed_absent is False
    assert "absence could not be verified" in cleanup.error
    assert len(engine.specs) == 1


def test_cleanup_failure_is_typed_and_blocks_grader(tmp_path):
    package = _package()
    engine = FakeEngine(cleanup_failure_phase=ContainerPhase.HARNESS)
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.CLEANUP_FAILED
    assert result.evidence.runtime_verified is False
    assert result.evidence.harness is not None
    assert result.evidence.harness.cleanup.status is CleanupStatus.FAILED
    assert result.evidence.harness.cleanup.confirmed_absent is False
    assert len(engine.specs) == 1


def test_controller_storage_monitor_enforces_writable_mount_limit(tmp_path):
    package = _package()
    engine = FakeEngine(harness_storage_bytes=512)
    resources = OciResourcePolicy(
        wall_time_ms=10_000,
        memory_bytes=128 * 1024 * 1024,
        cpu_millis=1_000,
        pids_limit=32,
        storage_bytes=128,
        stdout_bytes=1024,
        stderr_bytes=1024,
        artifact_bytes=1024,
        tmpfs_bytes=1024,
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(package, harness_resources=resources)
    )

    assert result.status is OciTrialStatus.INVALID_OUTPUT
    assert result.evidence.harness.storage.exceeded is True
    assert result.evidence.harness.storage.monitor_succeeded is False
    assert result.evidence.harness.storage.hard_storage_enforced is False
    assert result.evidence.runtime_verified is False


def test_hard_quota_fake_volume_lifecycle_seed_grader_and_evidence(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ATV_PROVIDER_API_KEY", PROVIDER_SECRET_CANARY)
    package = _package()
    engine = FakeHardQuotaEngine(tmp_path / "fake-volumes")
    harness_resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        storage_bytes=2 * 1024 * 1024,
        artifact_bytes=512 * 1024,
        tmpfs_bytes=256 * 1024,
    )
    grader_resources = replace(
        OciResourcePolicy.from_budget_limits(
            package.manifest["grader"]["budget_limits"]
        ),
        storage_bytes=1024 * 1024,
        tmpfs_bytes=256 * 1024,
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(
            package,
            storage_mode=OciStorageMode.HARD_QUOTA,
            harness_resources=harness_resources,
            grader_resources=grader_resources,
        )
    )

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.runtime_verified is True
    assert result.evidence.storage["selected_mode"] == "hard-quota"
    assert result.evidence.storage["hard_storage_enforced"] is True
    assert result.evidence.storage["hard_storage_cleanup_succeeded"] is True
    assert result.evidence.storage["official_eligible"] is True
    assert (
        result.evidence.storage["host_bind_writable_workspace_or_artifacts"]
        is False
    )
    assert "hard_aggregate_storage_quota_unavailable" not in (
        result.evidence.official_ineligibility_reasons
    )
    assert len(result.evidence.storage["volumes"]) == 2
    for volume in result.evidence.storage["volumes"]:
        assert volume["quota_verified"] is True
        assert volume["lifecycle_verified"] is True
        assert volume["inspection"]["driver"] == "local"
        assert volume["inspection"]["mountpoint"].startswith("/fake-volumes/")
        assert volume["inspection"]["options"] == volume["spec"]["options"]
        assert (
            f"size={volume['spec']['size_bytes']}"
            in volume["inspection"]["options"]["o"]
        )
        assert volume["cleanup"]["confirmed_absent"] is True
    assert list(engine.volume_root.iterdir()) == []
    assert all(force is True for _, force in engine.volume_remove_calls)

    seed = result.evidence.workspace["seed"]
    assert seed["verified"] is True
    assert seed["source_digest"] == seed["seeded_digest"]
    assert engine.harness_initial_snapshot["config.json"] == (
        package.public_workspace / "config.json"
    ).read_bytes()
    assert engine.grader_observed_output["harness.txt"] == b"fake-output"
    assert len(engine.stdin_payload_digests) == 2

    phase_order = result.evidence.phase_order
    assert phase_order.index("harness-aggregate_volume_inspected") < phase_order.index(
        "seed_started"
    )
    assert phase_order.index("seed_container_removed") < phase_order.index(
        "harness_started"
    )
    assert phase_order.index("harness_container_removed") < phase_order.index(
        "grader_started"
    )
    assert phase_order.index("grader_container_removed") < phase_order.index(
        "volume_keeper_removed"
    )
    assert phase_order.index("volume_keeper_removed") < phase_order.index(
        "harness-aggregate_volume_removed"
    )

    harness_spec = next(
        spec for spec in engine.specs if spec.phase is ContainerPhase.HARNESS
    )
    grader_spec = next(
        spec for spec in engine.specs if spec.phase is ContainerPhase.GRADER
    )
    assert all(mount.mount_type == "volume" for mount in harness_spec.mounts)
    assert not any(mount.read_only for mount in harness_spec.mounts)
    assert {mount.destination for mount in harness_spec.mounts} == {
        "/workspace",
        "/artifacts",
        "/tmp",
    }
    assert len({mount.source for mount in harness_spec.mounts}) == 1
    assert {mount.volume_subpath for mount in harness_spec.mounts} == {
        "workspace",
        "artifacts",
        "tmp",
    }
    assert all(mount.volume_nocopy for mount in harness_spec.mounts)
    assert all(
        mount.mount_type == "volume"
        for mount in grader_spec.mounts
        if mount.destination != "/grader-output"
    )
    trusted = str((package.root / "trusted").resolve())
    assert trusted not in "\n".join(build_run_argv(engine.executable, harness_spec))
    assert trusted not in "\n".join(build_run_argv(engine.executable, grader_spec))
    serialized = json.dumps(result.evidence.to_dict(), sort_keys=True)
    assert PROVIDER_SECRET_CANARY not in serialized
    assert "ATV_PROVIDER_API_KEY" not in serialized


def test_hard_quota_fake_inspection_mismatch_stops_before_container(tmp_path):
    package = _package()
    engine = FakeHardQuotaEngine(
        tmp_path / "fake-volumes",
        volume_inspection_mismatch=True,
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(package, storage_mode=OciStorageMode.HARD_QUOTA)
    )

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert engine.specs == []
    assert any("volume_options" in error for error in result.evidence.errors)
    assert result.evidence.storage["hard_storage_enforced"] is False
    assert all(
        volume["cleanup"]["confirmed_absent"]
        for volume in result.evidence.storage["volumes"]
    )
    assert list(engine.volume_root.iterdir()) == []


def test_hard_quota_fake_enospc_is_typed_and_still_gradable(tmp_path):
    package = _package()
    engine = FakeHardQuotaEngine(
        tmp_path / "fake-volumes",
        hard_enospc=True,
    )
    resources = replace(
        OciResourcePolicy.from_budget_limits(package.manifest["budget_limits"]),
        storage_bytes=1024 * 1024,
        artifact_bytes=256 * 1024,
        tmpfs_bytes=256 * 1024,
    )
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(
            package,
            storage_mode=OciStorageMode.HARD_QUOTA,
            harness_resources=resources,
        )
    )

    assert result.status is OciTrialStatus.STORAGE_FAILED
    assert result.evidence.harness.storage.hard_storage_enforced is True
    assert result.evidence.harness.storage.exceeded is True
    assert result.evidence.harness.storage.monitor_succeeded is True
    assert result.evidence.grader is not None
    assert result.grader_stdout == b'{"passed":true,"score":1.0}\n'
    assert result.evidence.storage["hard_storage_cleanup_succeeded"] is True
    assert list(engine.volume_root.iterdir()) == []


def test_runtime_inspection_mismatch_prevents_verified_claim_and_grading(tmp_path):
    package = _package()
    engine = FakeEngine(inspection_mismatch_phase=ContainerPhase.HARNESS)
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.POLICY_MISMATCH
    assert result.evidence.runtime_verified is False
    assert "read_only_rootfs" in result.evidence.harness.inspection_mismatches
    assert len(engine.specs) == 1


def test_image_inspection_digest_mismatch_stops_before_container_start(tmp_path):
    package = _package()
    engine = FakeEngine(image_verified=False)
    result = OciTrialRunner(engine, work_root=tmp_path).run(_request(package))

    assert result.status is OciTrialStatus.ENGINE_ERROR
    assert result.evidence.runtime_verified is False
    assert engine.specs == []


def test_trusted_protocol_session_preserves_controller_authority(
    tmp_path, protocol_documents
):
    package = _package()
    attempt = _attempt(package)
    protocol_request = deepcopy(protocol_documents["request"])
    protocol_request["trial_id"] = attempt.spec.trial_id
    protocol_request["attempt_id"] = attempt.attempt_id
    protocol_request["track"] = "controlled"
    harness_events = deepcopy(protocol_documents["harness_events"])
    for event in harness_events:
        event["trial_id"] = attempt.spec.trial_id
        event["attempt_id"] = attempt.attempt_id
    engine = FakeEngine(harness_stdout=canonical_jsonl(harness_events))
    session = ProtocolSession(protocol_documents["harness"], protocol_request)
    request = _request(
        package,
        attempt=attempt,
        protocol_session=session,
    )
    result = OciTrialRunner(
        engine,
        work_root=tmp_path,
        interactive_transport=ReplayInteractiveTransport(
            engine,
            canonical_jsonl(harness_events),
        ),
    ).run(request)

    assert result.status is OciTrialStatus.COMPLETED
    assert result.protocol_transcript is not None
    assert result.protocol_transcript.authority_verified is True
    assert result.evidence.protocol["parsed"] is True
    assert result.evidence.protocol["authority_verified"] is True
    assert result.evidence.protocol["mode"] == "interactive-attached-roundtrip"
    assert result.evidence.protocol["transcript_sha256"]
    assert result.evidence.harness.policy["stdin_open"] is True


def test_legacy_accepted_splice_is_integrity_only_and_unofficial(
    tmp_path, protocol_documents
):
    package = _package()
    harness_events = [
        protocol_documents["events"][0],
        *protocol_documents["events"][2:],
    ]
    engine = FakeEngine(harness_stdout=canonical_jsonl(harness_events))
    result = OciTrialRunner(engine, work_root=tmp_path).run(
        _request(
            package,
            protocol_parser=JsonlProtocolParser(),
            accepted_event=protocol_documents["accepted"],
        )
    )

    assert result.status is OciTrialStatus.COMPLETED
    assert result.evidence.protocol["parsed"] is True
    assert result.evidence.protocol["authority_verified"] is False
    assert result.evidence.protocol["integrity_only"] is True
    assert result.evidence.runtime_verified is False
    assert result.evidence.official_eligible is False
