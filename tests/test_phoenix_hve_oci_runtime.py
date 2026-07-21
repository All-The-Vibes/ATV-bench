"""Security and content-binding tests for the Phoenix/hve OCI backend."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from scripts import phoenix_hve_oci_runtime as oci


def _command(
    *argv: str,
    cwd: Path | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    result = subprocess.run(
        list(argv),
        cwd=cwd,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def _init_repo(path: Path, *, remote: str) -> str:
    path.mkdir(parents=True)
    _command("git", "init", "-q", cwd=path)
    _command("git", "config", "user.email", "atv@example.invalid", cwd=path)
    _command("git", "config", "user.name", "ATV Test", cwd=path)
    _command("git", "config", "core.autocrlf", "false", cwd=path)
    _command("git", "remote", "add", "origin", remote, cwd=path)
    return path.as_posix()


def _commit_all(path: Path, message: str = "seed") -> str:
    _command("git", "add", "-A", cwd=path)
    _command("git", "commit", "-qm", message, cwd=path)
    return _command("git", "rev-parse", "HEAD", cwd=path).decode().strip()


def _fake_copilot_identity(root: Path) -> oci.CopilotPackageIdentity:
    root.mkdir(parents=True, exist_ok=True)
    return oci.CopilotPackageIdentity(
        root=root.resolve(),
        version="1.2.3",
        build_commit="abc1234",
        tree_sha256="b" * 64,
        loader_sha256="c" * 64,
        host_node_version="v24.0.0",
        host_version_output="copilot 1.2.3",
    )


def _fake_proxy_image() -> oci.OciProxyImage:
    script_sha256 = oci._sha256_bytes(oci._CONNECT_PROXY_PY.encode("utf-8"))
    labels = {
        "org.atvbench.schema": oci.OCI_PROXY_IMAGE_SCHEMA,
        "org.atvbench.role": "connect-proxy",
        "org.atvbench.proxy.script-sha256": script_sha256,
    }
    return oci.OciProxyImage(
        tag=f"atv-bench/test-connect-proxy:{'9' * 24}",
        image_id=f"sha256:{'8' * 64}",
        platform="linux/amd64",
        runtime_base_image=f"runtime@sha256:{'7' * 64}",
        build_spec_sha256="9" * 64,
        inspect_sha256="a" * 64,
        script_sha256=script_sha256,
        labels=labels,
        reused=True,
    )


def _fake_image(tmp_path: Path, harness: str) -> oci.OciImage:
    source_root = tmp_path / f"{harness}-source"
    source_root.mkdir()
    source = oci.GitSourceIdentity(
        harness=harness,
        repository=(
            "all-the-vibes/atv-phoenix"
            if harness == "phoenix"
            else "microsoft/hve-core"
        ),
        checkout=source_root.resolve(),
        commit="1" * 40,
        git_tree="2" * 40,
        tracked_listing_sha256="3" * 64,
        remote="https://github.com/example/repo.git",
    )
    copilot = _fake_copilot_identity(tmp_path / f"{harness}-copilot")
    return oci.OciImage(
        harness=harness,
        tag=f"atv-bench/test-{harness}:{'4' * 24}",
        image_id=f"sha256:{'5' * 64}",
        platform="linux/amd64",
        build_spec_sha256="4" * 64,
        inspect_sha256="6" * 64,
        source=source,
        copilot=copilot,
        labels={"org.atvbench.harness": harness},
        parity={"verified": True},
        proxy=_fake_proxy_image(),
        reused=True,
    )


def _fresh_workspace(path: Path) -> Path:
    _init_repo(path, remote="https://github.com/example/task.git")
    (path / "main.py").write_text("print('seed')\n", encoding="utf-8")
    _commit_all(path)
    return path.resolve()


def _run_config(tmp_path: Path, harness: str = "phoenix") -> oci.OciRunConfig:
    return oci.OciRunConfig(
        docker="docker",
        image=_fake_image(tmp_path, harness),
        harness=harness,
        workspace=_fresh_workspace(tmp_path / f"{harness}-workspace"),
        evidence_dir=tmp_path / f"{harness}-evidence",
        run_id=f"task-01-{harness}",
        model="gpt-explicit",
        max_ai_credits=41,
        timeout_seconds=123,
        limits=oci.ResourceLimits(
            cpus=1.5,
            memory_bytes=768 * 1024 * 1024,
            pids=96,
            tmpfs_bytes=64 * 1024 * 1024,
            home_tmpfs_bytes=32 * 1024 * 1024,
            shm_bytes=32 * 1024 * 1024,
        ),
    )


def _container_inspect(
    config: oci.OciRunConfig,
    *,
    name: str,
    internal_network: str,
    running: bool = False,
) -> dict:
    proxy_url = f"http://{oci.PROXY_ALIAS}:{oci.PROXY_PORT}"
    return {
        "Id": "7" * 64,
        "Image": config.image.image_id,
        "Name": f"/{name}",
        "Config": {
            "User": oci.CONTAINER_USER,
            "Image": config.image.tag,
            "WorkingDir": oci.CONTAINER_WORKSPACE,
            "Cmd": [
                "-p",
                "task",
                "--secret-env-vars=GITHUB_ASKPASS",
            ],
            "Labels": {
                "org.atvbench.run-schema": oci.OCI_RUN_SCHEMA,
                "org.atvbench.run-id": config.run_id,
                "org.atvbench.harness": config.harness,
            },
            "Env": [
                f"COPILOT_HOME={oci.COPILOT_HOME}",
                "HOME=/home/runner",
                f"HTTP_PROXY={proxy_url}",
                f"HTTPS_PROXY={proxy_url}",
                f"http_proxy={proxy_url}",
                f"https_proxy={proxy_url}",
            ],
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Init": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "AutoRemove": False,
            "NetworkMode": internal_network,
            "IpcMode": "none",
            "PidsLimit": config.limits.pids,
            "Memory": config.limits.memory_bytes,
            "MemorySwap": config.limits.memory_bytes,
            "ShmSize": config.limits.shm_bytes,
            "NanoCpus": int(config.limits.cpus * 1_000_000_000),
            "Devices": [],
            "Binds": None,
            "Tmpfs": {
                "/tmp": (f"rw,noexec,nosuid,nodev,size={config.limits.tmpfs_bytes}"),
                oci.COPILOT_HOME: (
                    f"rw,noexec,nosuid,nodev,size={config.limits.tmpfs_bytes}"
                ),
                "/home/runner": (
                    f"rw,exec,nosuid,nodev,size={config.limits.home_tmpfs_bytes}"
                ),
            },
            "Ulimits": [{"Name": "nofile", "Soft": 1024, "Hard": 1024}],
            "ExtraHosts": [
                "host.docker.internal:127.0.0.1",
                "gateway.docker.internal:127.0.0.1",
            ],
        },
        "Mounts": [
            {
                "Type": "bind",
                "Source": str(config.workspace),
                "Destination": oci.CONTAINER_WORKSPACE,
                "RW": True,
            }
        ],
        "NetworkSettings": {
            "Networks": {
                internal_network: {
                    "Aliases": [name],
                }
            }
        },
        "State": {"Running": running, "ExitCode": 0},
    }


def _network_inspect(
    config: oci.OciRunConfig,
    *,
    name: str,
    role: str,
    internal: bool,
    members: tuple[str, ...] = (),
) -> dict:
    return {
        "Name": name,
        "Id": "d" * 64,
        "Driver": "bridge",
        "Internal": internal,
        "Attachable": False,
        "Ingress": False,
        "Labels": oci._network_labels(config, role=role),
        "Containers": {
            str(index): {"Name": member}
            for index, member in enumerate(members, start=1)
        },
    }


def _proxy_container_inspect(
    config: oci.OciRunConfig,
    *,
    name: str,
    internal_network: str,
    egress_network: str,
    running: bool = True,
) -> dict:
    return {
        "Id": "e" * 64,
        "Image": config.image.proxy.image_id,
        "Name": f"/{name}",
        "Config": {
            "User": oci.CONTAINER_USER,
            "WorkingDir": "/",
            "Env": [
                "PYTHONDONTWRITEBYTECODE=1",
                "PYTHONUNBUFFERED=1",
            ],
            "Labels": {
                "org.atvbench.run-schema": oci.OCI_RUN_SCHEMA,
                "org.atvbench.run-id": config.run_id,
                "org.atvbench.role": "connect-proxy",
            },
        },
        "HostConfig": {
            "ReadonlyRootfs": True,
            "Init": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": False,
            "AutoRemove": False,
            "NetworkMode": egress_network,
            "IpcMode": "none",
            "PidMode": "",
            "UTSMode": "",
            "PidsLimit": 64,
            "Memory": 256 * 1024 * 1024,
            "MemorySwap": 256 * 1024 * 1024,
            "ShmSize": 32 * 1024 * 1024,
            "NanoCpus": 500_000_000,
            "Devices": [],
            "Binds": None,
            "PublishAllPorts": False,
            "PortBindings": None,
            "Tmpfs": {
                "/tmp": "rw,noexec,nosuid,nodev,size=33554432",
            },
            "Ulimits": [{"Name": "nofile", "Soft": 256, "Hard": 256}],
            "ExtraHosts": [
                "host.docker.internal:127.0.0.1",
                "gateway.docker.internal:127.0.0.1",
            ],
        },
        "Mounts": [],
        "NetworkSettings": {
            "Networks": {
                egress_network: {"Aliases": [name]},
                internal_network: {"Aliases": [name, oci.PROXY_ALIAS]},
            }
        },
        "State": {"Running": running, "ExitCode": 0},
    }


def test_create_argv_enforces_isolation_and_one_workspace_mount(tmp_path):
    config = _run_config(tmp_path, "phoenix")
    names = oci._run_resource_names(config)

    argv = oci._container_create_argv(
        config,
        goal="Do the public task; do not inspect the hidden grader.",
        container_name=names["harness_container"],
        internal_network=names["internal_network"],
    )

    assert "--read-only" in argv
    assert oci._option_values(argv, "--user") == [oci.CONTAINER_USER]
    assert oci._option_values(argv, "--cap-drop") == ["ALL"]
    assert oci._option_values(argv, "--security-opt") == ["no-new-privileges:true"]
    assert oci._option_values(argv, "--network") == [names["internal_network"]]
    tmpfs_values = oci._option_values(argv, "--tmpfs")
    assert "exec" in next(
        value for value in tmpfs_values if value.startswith("/home/runner:")
    )
    assert all(
        "noexec" in value
        for value in tmpfs_values
        if not value.startswith("/home/runner:")
    )
    assert oci._option_values(argv, "--mount") == [
        oci._workspace_mount_value(config.workspace)
    ]
    assert oci._option_values(argv, "--env-file") == []
    environment = oci._option_values(argv, "--env")
    assert f"HTTPS_PROXY=http://{oci.PROXY_ALIAS}:{oci.PROXY_PORT}" in environment
    assert f"HTTP_PROXY=http://{oci.PROXY_ALIAS}:{oci.PROXY_PORT}" in environment
    assert "--secret-env-vars=GITHUB_ASKPASS" in argv
    assert oci.AUTH_DIRECTORY.startswith("/home/runner/")
    assert "COPILOT_GITHUB_TOKEN=" not in "\n".join(argv)
    assert "GITHUB_TOKEN=" not in "\n".join(argv)
    assert "GH_TOKEN=" not in "\n".join(argv)
    assert "docker.sock" not in "\n".join(argv)
    assert str(config.image.source.checkout) not in "\n".join(argv)
    assert str(config.image.copilot.root) not in "\n".join(argv)


def test_create_argv_rejects_second_mount_and_docker_socket(tmp_path):
    config = _run_config(tmp_path, "hve")
    names = oci._run_resource_names(config)
    argv = oci._container_create_argv(
        config,
        goal="task",
        container_name=names["harness_container"],
        internal_network=names["internal_network"],
    )
    argv[2:2] = [
        "--mount",
        "type=bind,source=/var/run/docker.sock,target=/var/run/docker.sock",
    ]

    with pytest.raises(oci.OciRuntimeError):
        oci._assert_secure_create_argv(
            argv,
            workspace=config.workspace,
            network=names["internal_network"],
        )


def test_container_inspect_validation_and_redaction(tmp_path):
    config = _run_config(tmp_path)
    names = oci._run_resource_names(config)
    token = "super-secret-token"
    inspect = _container_inspect(
        config,
        name=names["harness_container"],
        internal_network=names["internal_network"],
    )

    oci._validate_container_inspect(
        inspect,
        config=config,
        container_name=names["harness_container"],
        internal_network=names["internal_network"],
        token=token,
        expect_running=False,
    )
    redacted = oci._redact_inspect(inspect, token)
    encoded = json.dumps(redacted, sort_keys=True)
    assert token not in encoded
    assert "COPILOT_GITHUB_TOKEN" not in encoded
    assert "GITHUB_ASKPASS" not in "\n".join(inspect["Config"]["Env"])

    inspect_with_token = json.loads(json.dumps(inspect))
    inspect_with_token["Config"]["Env"].append(f"GITHUB_TOKEN={token}")
    with pytest.raises(oci.OciRuntimeError, match="persists"):
        oci._validate_container_inspect(
            inspect_with_token,
            config=config,
            container_name=names["harness_container"],
            internal_network=names["internal_network"],
            token=token,
            expect_running=False,
        )

    inspect["Mounts"].append(
        {
            "Type": "bind",
            "Source": str(tmp_path / "hidden-grader"),
            "Destination": "/grader",
            "RW": False,
        }
    )
    with pytest.raises(oci.OciRuntimeError, match="exactly one"):
        oci._validate_container_inspect(
            inspect,
            config=config,
            container_name=names["harness_container"],
            internal_network=names["internal_network"],
            token=token,
            expect_running=False,
        )


def test_source_identity_fails_closed_on_dirty_or_wrong_commit(tmp_path):
    repo = tmp_path / "phoenix"
    _init_repo(repo, remote="https://github.com/All-The-Vibes/ATV-Phoenix.git")
    (repo / "Cargo.toml").write_text("[package]\nname='phoenix'\n", encoding="utf-8")
    (repo / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    commit = _commit_all(repo)

    identity = oci.inspect_source(
        repo,
        harness="phoenix",
        expected_commit=commit,
    )
    assert identity.commit == commit
    assert identity.repository == "all-the-vibes/atv-phoenix"

    (repo / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(oci.OciRuntimeError, match="dirty"):
        oci.inspect_source(repo, harness="phoenix", expected_commit=commit)
    (repo / "untracked.txt").unlink()

    with pytest.raises(oci.OciRuntimeError, match="expected"):
        oci.inspect_source(
            repo,
            harness="phoenix",
            expected_commit="f" * 40,
        )


def test_local_copilot_package_is_content_bound_and_exactly_copied(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is unavailable")
    package = tmp_path / "copilot"
    (package / "nested").mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@github/copilot",
                "version": "1.2.3",
                "type": "module",
                "buildMetadata": {"gitCommit": "abc1234"},
            }
        ),
        encoding="utf-8",
    )
    (package / "npm-loader.js").write_text(
        'console.log("copilot 1.2.3");\n',
        encoding="utf-8",
    )
    (package / "nested" / "payload.bin").write_bytes(b"\x00payload\xff")

    identity = oci.inspect_copilot_package(package, host_node=node)
    copied = tmp_path / "copied"
    oci._copy_tree_exact(package, copied)

    assert identity.version == "1.2.3"
    assert identity.build_commit == "abc1234"
    assert identity.host_version_output == "copilot 1.2.3"
    assert identity.tree_sha256 == oci.tree_sha256(copied)
    assert identity.loader_sha256 == oci._sha256_file(copied / "npm-loader.js")


def test_hve_git_symlink_assets_are_materialized_and_shimmed(tmp_path):
    repo = tmp_path / "hve"
    _init_repo(repo, remote="https://github.com/microsoft/hve-core.git")
    _command("git", "config", "core.symlinks", "false", cwd=repo)
    target = repo / "shared" / "rpi-agent.md"
    target.parent.mkdir()
    target.write_text(
        "---\nname: RPI Agent\ntools: ['read']\n---\n\nDo the task.\n",
        encoding="utf-8",
    )
    plugin_json = repo / "plugins" / "hve-core" / ".github" / "plugin" / "plugin.json"
    plugin_json.parent.mkdir(parents=True)
    plugin_json.write_text('{"name":"hve-core"}\n', encoding="utf-8")
    pointer = repo / "plugins" / "hve-core" / "agents" / "rpi-agent.md"
    pointer.parent.mkdir(parents=True)
    pointer_text = "../../../shared/rpi-agent.md"
    pointer.write_text(pointer_text, encoding="utf-8")
    _command("git", "add", "shared/rpi-agent.md", cwd=repo)
    _command(
        "git",
        "add",
        "plugins/hve-core/.github/plugin/plugin.json",
        cwd=repo,
    )
    object_id = (
        _command(
            "git",
            "hash-object",
            "-w",
            "--stdin",
            cwd=repo,
            input_bytes=pointer_text.encode("utf-8"),
        )
        .decode()
        .strip()
    )
    _command(
        "git",
        "update-index",
        "--add",
        "--cacheinfo",
        "120000",
        object_id,
        "plugins/hve-core/agents/rpi-agent.md",
        cwd=repo,
    )
    _command("git", "commit", "-qm", "seed", cwd=repo)
    commit = _command("git", "rev-parse", "HEAD", cwd=repo).decode().strip()
    source = oci.inspect_source(repo, harness="hve", expected_commit=commit)

    destination = tmp_path / "assets"
    metadata = oci._prepare_hve_assets(
        source,
        destination,
        tool_compat_shim=True,
    )
    materialized = destination / "plugin" / "agents" / "rpi-agent.md"

    assert materialized.is_file()
    assert "name: RPI Agent" in materialized.read_text(encoding="utf-8")
    assert "tools: ['*']" in materialized.read_text(encoding="utf-8")
    assert metadata["rpi_agent"] == "agents/rpi-agent.md"
    assert metadata["tool_compatibility_shim"]["path"] == ("plugin/agents/rpi-agent.md")
    assert str(tmp_path) not in json.dumps(metadata, sort_keys=True)
    assert metadata["asset_tree_sha256"] == oci.tree_sha256(destination / "plugin")
    assert metadata["staging_tree_sha256"] == oci.tree_sha256(destination)


def test_dockerfiles_keep_harness_assets_separate_and_lock_cargo():
    phoenix = oci._dockerfile("phoenix")
    hve = oci._dockerfile("hve")
    proxy = oci._proxy_dockerfile()

    assert "cargo build --locked --release --bin phoenix-mcp" in phoenix
    assert "Cargo.lock" not in hve
    assert "phoenix-src/" in phoenix
    assert "hve-assets" not in phoenix
    assert "hve-assets/plugin/" in hve
    assert "phoenix-src" not in hve
    assert "USER 10001:10001" in phoenix
    assert "USER 10001:10001" in hve
    assert "feed-token.sh" in phoenix
    assert "start-agent.sh" in phoenix
    assert "feed-token.sh" in hve
    assert "start-agent.sh" in hve
    assert "ARG RUNTIME_BASE_IMAGE" in proxy
    assert "ATV_PROXY_SCRIPT_SHA256" in proxy
    assert "connect-proxy.py" in proxy
    assert "USER 10001:10001" in proxy


def test_stale_image_labels_fail_closed():
    labels = {
        "org.atvbench.schema": oci.OCI_IMAGE_SCHEMA,
        "org.atvbench.harness": "phoenix",
        "org.atvbench.build-spec-sha256": "a" * 64,
    }
    inspect = {
        "Id": f"sha256:{'b' * 64}",
        "Os": "linux",
        "Architecture": "amd64",
        "RepoTags": ["atv-bench/test:tag"],
        "Config": {
            "Labels": {**labels, "org.atvbench.source.commit": "wrong"},
            "User": oci.CONTAINER_USER,
            "WorkingDir": oci.CONTAINER_WORKSPACE,
            "Entrypoint": ["/opt/atv/entrypoint.sh"],
            "Env": ["ATV_HARNESS=phoenix"],
        },
    }
    expected = {**labels, "org.atvbench.source.commit": "c" * 40}

    with pytest.raises(oci.OciRuntimeError, match="stale or substituted"):
        oci._validate_image_inspect(
            inspect,
            harness="phoenix",
            tag="atv-bench/test:tag",
            labels=expected,
            platform="linux/amd64",
        )


def test_linux_copilot_parity_mismatch_fails_closed(tmp_path, monkeypatch):
    identity = _fake_copilot_identity(tmp_path / "copilot")
    valid = {
        "platform": "linux",
        "arch": "x64",
        "harness": "phoenix",
        "node_version": identity.host_node_version,
        "package_version": identity.version,
        "build_commit": identity.build_commit,
        "tree_sha256": identity.tree_sha256,
        "asset_tree_sha256": "d" * 64,
        "loader_sha256": identity.loader_sha256,
        "entrypoint_sha256": oci._sha256_bytes(oci._ENTRYPOINT_SH.encode("utf-8")),
        "feed_token_sha256": oci._sha256_bytes(oci._FEED_TOKEN_SH.encode("utf-8")),
        "start_agent_sha256": oci._sha256_bytes(oci._START_AGENT_SH.encode("utf-8")),
        "node_use_env_proxy_supported": True,
        "version_output": (f"{identity.version}\nCommit: {identity.build_commit}"),
        "runtime_tools": [
            "/usr/local/bin/node",
            "/usr/bin/git",
            "/usr/bin/python3",
            "/usr/bin/cp",
        ],
        "phoenix_mcp_sha256": "e" * 64,
        "other_harness_assets_present": False,
    }

    def result(payload):
        return subprocess.CompletedProcess(
            args=["docker"],
            returncode=0,
            stdout=json.dumps(payload).encode("utf-8") + b"\n",
            stderr=b"",
        )

    monkeypatch.setattr(oci, "_run_bytes", lambda *args, **kwargs: result(valid))
    parity = oci._verify_image_parity(
        "docker",
        "image",
        identity,
        harness="phoenix",
        asset_metadata={"asset_tree_sha256": "d" * 64},
        platform="linux/amd64",
    )
    assert parity["verified"] is True
    assert parity["arch"] == "x64"
    assert parity["host_version_output"] == identity.host_version_output
    assert identity.version in parity["linux_version_output"]
    assert identity.build_commit in parity["linux_version_output"]

    invalid = {**valid, "tree_sha256": "0" * 64}
    monkeypatch.setattr(oci, "_run_bytes", lambda *args, **kwargs: result(invalid))
    with pytest.raises(oci.OciRuntimeError, match="parity"):
        oci._verify_image_parity(
            "docker",
            "image",
            identity,
            harness="phoenix",
            asset_metadata={"asset_tree_sha256": "d" * 64},
            platform="linux/amd64",
        )


def test_token_is_sent_only_over_docker_exec_stdin(tmp_path, monkeypatch):
    config = _run_config(tmp_path)
    token = "gho_super_secret"

    class CaptureStdin:
        def __init__(self):
            self.payload = bytearray()
            self.closed = False

        def write(self, payload):
            self.payload.extend(payload)

        def flush(self):
            return None

        def close(self):
            self.closed = True

    class FakeProcess:
        def __init__(self, argv):
            self.argv = list(argv)
            self.stdin = CaptureStdin()
            self.captured_stdin = self.stdin
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    captured = {}

    def fake_popen(argv, **kwargs):
        process = FakeProcess(argv)
        captured["process"] = process
        captured["kwargs"] = kwargs
        return process

    monkeypatch.setattr(oci.subprocess, "Popen", fake_popen)
    process, argv = oci._start_token_feeder(
        config,
        container_id="a" * 64,
        token=token,
    )
    evidence = oci._finish_token_feeder(
        process,
        argv=argv,
        token=token,
    )

    fake = captured["process"]
    assert token not in "\n".join(argv)
    assert bytes(fake.captured_stdin.payload) == f"{token}\n".encode()
    assert fake.captured_stdin.closed is True
    assert evidence["stdin_only"] is True
    assert evidence["fifo_removed"] is True
    assert evidence["askpass_removed"] is True
    assert evidence["secret_env_vars"] == ["GITHUB_ASKPASS"]


def test_proxy_networks_are_exact_and_token_free(tmp_path):
    config = _run_config(tmp_path)
    names = oci._run_resource_names(config)
    token = "gho_not_in_metadata"
    internal = _network_inspect(
        config,
        name=names["internal_network"],
        role="internal",
        internal=True,
        members=(names["proxy_container"], names["harness_container"]),
    )
    egress = _network_inspect(
        config,
        name=names["egress_network"],
        role="egress",
        internal=False,
        members=(names["proxy_container"],),
    )
    proxy = _proxy_container_inspect(
        config,
        name=names["proxy_container"],
        internal_network=names["internal_network"],
        egress_network=names["egress_network"],
    )

    oci._validate_network_inspect(
        internal,
        config=config,
        name=names["internal_network"],
        role="internal",
        internal=True,
    )
    oci._validate_network_inspect(
        egress,
        config=config,
        name=names["egress_network"],
        role="egress",
        internal=False,
    )
    oci._validate_network_membership(
        internal,
        egress,
        internal_expected={names["proxy_container"], names["harness_container"]},
        egress_expected={names["proxy_container"]},
    )
    oci._validate_proxy_container_inspect(
        proxy,
        config=config,
        container_name=names["proxy_container"],
        internal_network=names["internal_network"],
        egress_network=names["egress_network"],
        token=token,
        expect_running=True,
    )

    proxy["Config"]["Env"].append(f"GITHUB_TOKEN={token}")
    with pytest.raises(oci.OciRuntimeError, match="persists"):
        oci._validate_proxy_container_inspect(
            proxy,
            config=config,
            container_name=names["proxy_container"],
            internal_network=names["internal_network"],
            egress_network=names["egress_network"],
            token=token,
            expect_running=True,
        )


def test_proxy_log_allowlist_is_fail_closed():
    rows = [
        {
            "event": "ready",
            "allowlist": sorted(oci.COPILOT_MODEL_HOSTS),
            "explicit_denylist": sorted(oci.EXPLICIT_PROXY_DENY_HOSTS),
        },
        {
            "event": "connect",
            "allowed": False,
            "host": "raw.githubusercontent.com",
            "port": 443,
            "reason": "explicit_deny",
        },
        {
            "event": "connect",
            "allowed": True,
            "host": "api.githubcopilot.com",
            "port": 443,
        },
    ]
    payload = b"\n".join(
        json.dumps(row, sort_keys=True).encode("utf-8") for row in rows
    )
    assert len(oci._validate_proxy_logs(payload, b"")) == 3

    rows[-1]["host"] = "api.github.com"
    bad = b"\n".join(json.dumps(row).encode("utf-8") for row in rows)
    with pytest.raises(oci.OciRuntimeError, match="non-Copilot"):
        oci._validate_proxy_logs(bad, b"")


def test_cleanup_unconfirmed_is_detected(tmp_path, monkeypatch):
    config = _run_config(tmp_path)
    names = oci._run_resource_names(config)
    harness = _container_inspect(
        config,
        name=names["harness_container"],
        internal_network=names["internal_network"],
    )

    def inspect_container(docker, reference):
        return harness if reference == names["harness_container"] else None

    monkeypatch.setattr(oci, "_inspect_container_optional", inspect_container)
    monkeypatch.setattr(oci, "_inspect_network_optional", lambda *args: None)
    monkeypatch.setattr(
        oci,
        "_run_bytes",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
    )

    cleanup = oci._cleanup_run_resources(config, names=names)

    assert cleanup["all_confirmed_absent"] is False
    harness_row = cleanup["resources"][0]
    assert harness_row["ownership_confirmed"] is True
    assert harness_row["confirmed_absent"] is False


def test_build_config_requires_digest_pinned_base_images(tmp_path):
    config = oci.OciBuildConfig(
        phoenix_repo=tmp_path / "phoenix",
        phoenix_commit="a" * 40,
        hve_repo=tmp_path / "hve",
        hve_commit="b" * 40,
        copilot_package=tmp_path / "copilot",
        runtime_base_image="node:latest",
        rust_builder_image=f"rust@sha256:{'c' * 64}",
        evidence_dir=tmp_path / "evidence",
    )

    with pytest.raises(oci.OciRuntimeError, match="pinned"):
        oci._validate_build_config(config)


@pytest.mark.integration
def test_docker_non_root_read_only_smoke_when_available():
    docker = shutil.which("docker")
    image = "node:24-bookworm-slim"
    if not docker:
        pytest.skip("Docker CLI is unavailable")
    version = subprocess.run(
        [docker, "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    local_image = subprocess.run(
        [docker, "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if version.returncode != 0 or local_image.returncode != 0:
        pytest.skip("Docker daemon or local smoke image is unavailable")
    name = f"atv-oci-smoke-{uuid.uuid4().hex[:12]}"
    try:
        result = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--name",
                name,
                "--read-only",
                "--user",
                oci.CONTAINER_USER,
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                "16",
                "--cpus",
                "0.25",
                "--memory",
                str(128 * 1024 * 1024),
                "--memory-swap",
                str(128 * 1024 * 1024),
                "--network",
                "none",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16777216",
                "--entrypoint",
                "node",
                image,
                "-e",
                (
                    "const fs=require('fs');"
                    "console.log(JSON.stringify({uid:process.getuid(),"
                    "rootWritable:(()=>{try{fs.writeFileSync('/atv-test','x');"
                    "return true}catch{return false}})()}))"
                ),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
        )
    finally:
        subprocess.run(
            [docker, "container", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    payload = json.loads(result.stdout)
    assert payload == {"uid": 10001, "rootWritable": False}


@pytest.mark.integration
def test_docker_connect_proxy_denies_raw_and_allows_copilot_when_available(
    tmp_path,
):
    docker = shutil.which("docker")
    if not docker:
        pytest.skip("Docker CLI is unavailable")
    version = subprocess.run(
        [docker, "version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if version.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    base_image = None
    for candidate in (
        "python:3.13-slim",
        "python:3.12-slim",
        "node:24-bookworm",
        "node:25.9-bookworm",
        "rust:1.88-bookworm",
        "node:24-bookworm-slim",
    ):
        inspect = subprocess.run(
            [docker, "image", "inspect", candidate],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        if inspect.returncode != 0:
            continue
        tools = subprocess.run(
            [
                docker,
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "sh",
                candidate,
                "-c",
                "command -v python3 && command -v groupadd && command -v useradd",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=60,
        )
        if tools.returncode == 0:
            base_image = candidate
            break
    if base_image is None:
        pytest.skip("no local Docker image has Python and user-management tools")

    suffix = uuid.uuid4().hex[:12]
    tag = f"atv-connect-proxy-smoke:{suffix}"
    proxy_name = f"atv-proxy-smoke-{suffix}"
    internal_network = f"atv-int-smoke-{suffix}"
    egress_network = f"atv-eg-smoke-{suffix}"
    dockerfile = tmp_path / "Dockerfile"
    proxy_script = tmp_path / "connect-proxy.py"
    dockerfile.write_text(oci._proxy_dockerfile(), encoding="utf-8", newline="\n")
    proxy_script.write_text(
        oci._CONNECT_PROXY_PY,
        encoding="utf-8",
        newline="\n",
    )
    script_sha256 = oci._sha256_bytes(oci._CONNECT_PROXY_PY.encode("utf-8"))
    client = (
        "import socket,sys;"
        "s=socket.create_connection((sys.argv[1],18080),10);"
        "target=sys.argv[2];"
        "s.sendall(('CONNECT '+target+':443 HTTP/1.1\\r\\nHost: '+target+"
        "':443\\r\\n\\r\\n').encode());"
        "data=s.recv(4096);"
        "print(data.split(b'\\r\\n',1)[0].decode());"
        "s.close()"
    )

    def run(*argv, timeout=120):
        return subprocess.run(
            [docker, *argv],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )

    try:
        build = run(
            "build",
            "--pull=false",
            "--file",
            str(dockerfile),
            "--tag",
            tag,
            "--build-arg",
            f"RUNTIME_BASE_IMAGE={base_image}",
            "--build-arg",
            f"ATV_PROXY_SCRIPT_SHA256={script_sha256}",
            str(tmp_path),
            timeout=600,
        )
        assert build.returncode == 0, build.stderr.decode(errors="replace")
        assert (
            run(
                "network",
                "create",
                "--driver",
                "bridge",
                "--internal",
                internal_network,
            ).returncode
            == 0
        )
        assert (
            run(
                "network",
                "create",
                "--driver",
                "bridge",
                egress_network,
            ).returncode
            == 0
        )
        create = run(
            "create",
            "--name",
            proxy_name,
            "--read-only",
            "--user",
            oci.CONTAINER_USER,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            egress_network,
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=33554432",
            tag,
        )
        assert create.returncode == 0, create.stderr.decode(errors="replace")
        connect = run(
            "network",
            "connect",
            "--alias",
            oci.PROXY_ALIAS,
            internal_network,
            proxy_name,
        )
        assert connect.returncode == 0, connect.stderr.decode(errors="replace")
        start = run("start", proxy_name)
        assert start.returncode == 0, start.stderr.decode(errors="replace")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            logs = run("logs", proxy_name, timeout=30)
            if b'"event":"ready"' in logs.stdout:
                break
            time.sleep(0.1)
        else:
            pytest.fail("CONNECT proxy did not become ready")

        direct = run(
            "run",
            "--rm",
            "--network",
            internal_network,
            base_image,
            "python3",
            "-c",
            ("import socket;socket.create_connection(('1.1.1.1',443),3)"),
        )
        assert direct.returncode != 0

        denied = run(
            "run",
            "--rm",
            "--network",
            internal_network,
            base_image,
            "python3",
            "-c",
            client,
            oci.PROXY_ALIAS,
            "raw.githubusercontent.com",
        )
        assert denied.returncode == 0, denied.stderr.decode(errors="replace")
        assert denied.stdout.strip() == b"HTTP/1.1 403 Forbidden"

        allowed = run(
            "run",
            "--rm",
            "--network",
            internal_network,
            base_image,
            "python3",
            "-c",
            client,
            oci.PROXY_ALIAS,
            "api.githubcopilot.com",
        )
        assert allowed.returncode == 0, allowed.stderr.decode(errors="replace")
        if allowed.stdout.strip() == b"HTTP/1.1 502 Bad Gateway":
            pytest.skip("Docker proxy smoke has no DNS or external network access")
        assert allowed.stdout.strip() == b"HTTP/1.1 200 Connection Established"

        logs = run("logs", proxy_name)
        assert b'"host":"raw.githubusercontent.com"' in logs.stdout
        assert b'"allowed":false' in logs.stdout
        assert b'"host":"api.githubcopilot.com"' in logs.stdout
        assert b'"allowed":true' in logs.stdout
    finally:
        run("container", "rm", "--force", proxy_name, timeout=60)
        run("network", "rm", internal_network, timeout=60)
        run("network", "rm", egress_network, timeout=60)
        run("image", "rm", "--force", tag, timeout=120)
        assert run("container", "inspect", proxy_name).returncode != 0
        assert run("network", "inspect", internal_network).returncode != 0
        assert run("network", "inspect", egress_network).returncode != 0
