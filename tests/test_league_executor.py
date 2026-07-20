"""Hermetic and gated end-to-end tests for the local League executor."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import site
import shutil
import subprocess
import sys
import textwrap
import threading
import venv
import zipfile
from pathlib import Path, PurePosixPath
from typing import Sequence

import pytest
from typer.testing import CliRunner

from atv_bench.cli import app
from atv_bench.league_executor import (
    ARENA_BASE_IMAGE,
    RUN_OUTPUT_LIMIT_BYTES,
    CommandResult,
    DockerCliEngine,
    LeagueExecutorError,
    LeagueScoreReceipt,
    _exclusive_store_lock,
    execute_league_score,
    materialize_arena_context,
)
from atv_bench.store import LeagueStore

ROOT = Path(__file__).parent.parent


@pytest.fixture(autouse=True)
def _clear_ambient_engine_selection(monkeypatch):
    """Unit/integration calls must opt into CI or non-default Docker routing."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)


def _command_result(
    argv: Sequence[str],
    *,
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timed_out: bool = False,
    output_limit_exceeded: bool = False,
) -> CommandResult:
    return CommandResult(
        argv=tuple(str(part) for part in argv),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        stdout_total_bytes=len(stdout),
        stderr_total_bytes=len(stderr),
        duration_ms=7,
        stdout_sha256=hashlib.sha256(stdout).hexdigest(),
        stderr_sha256=hashlib.sha256(stderr).hexdigest(),
        timed_out=timed_out,
        output_limit_exceeded=output_limit_exceeded,
    )


class FakeEngine:
    executable = "docker"

    def __init__(
        self,
        *,
        run_stdout: bytes | None = None,
        run_exit_code: int = 0,
        run_timed_out: bool = False,
        run_output_limit_exceeded: bool = False,
        cleanup_probe_error: bool = False,
        context_host: str = "npipe:////./pipe/dockerDesktopLinuxEngine",
    ) -> None:
        self.run_stdout = run_stdout
        self.run_exit_code = run_exit_code
        self.run_timed_out = run_timed_out
        self.run_output_limit_exceeded = run_output_limit_exceeded
        self.cleanup_probe_error = cleanup_probe_error
        self.context_host = context_host
        self.commands: list[tuple[str, ...]] = []
        self.context_files: dict[str, bytes] = {}
        self.staged_bot_bytes: bytes | None = None

    def execute(
        self,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        output_limit_bytes: int,
    ) -> CommandResult:
        del timeout_seconds, output_limit_bytes
        command = tuple(str(part) for part in argv)
        self.commands.append(command)

        if command[1:] == ("--version",):
            return _command_result(command, stdout=b"Docker version 28.0.0, build fake\n")

        if command[1:] == ("context", "show"):
            return _command_result(command, stdout=b"fake-local\n")

        if command[1:4] == ("context", "inspect", "--format"):
            return _command_result(
                command,
                stdout=(json.dumps(self.context_host) + "\n").encode(),
            )

        if command[1:4] == ("info", "--format", "{{json .}}"):
            payload = {
                "ID": "fake-daemon-id",
                "Name": "fake-daemon",
                "ServerVersion": "28.0.0",
                "OperatingSystem": "Fake Linux",
                "OSType": "linux",
                "Architecture": "x86_64",
                "KernelVersion": "6.6.0-fake",
                "Driver": "overlay2",
                "CgroupDriver": "cgroupfs",
                "SecurityOptions": [
                    "name=cgroupns",
                    "name=seccomp,profile=builtin",
                ],
            }
            return _command_result(command, stdout=(json.dumps(payload) + "\n").encode())

        if len(command) > 1 and command[1] == "build":
            context = Path(command[-1])
            self.context_files = {
                path.relative_to(context).as_posix(): path.read_bytes()
                for path in context.rglob("*")
                if path.is_file()
            }
            return _command_result(
                command,
                stdout=b"fake build complete\n",
                stderr=b"fake build diagnostic\n",
            )

        if (
            len(command) > 5
            and command[1:4] == ("image", "inspect", "--format")
        ):
            return _command_result(
                command,
                stdout=("sha256:" + "a" * 64 + "\n").encode(),
            )

        if len(command) > 1 and command[1] == "run":
            mount = command[command.index("--mount") + 1]
            source = mount.split("source=", 1)[1].split(",target=", 1)[0]
            self.staged_bot_bytes = (Path(source) / "main.py").read_bytes()
            if self.run_stdout is None:
                env_values = {
                    command[index + 1].split("=", 1)[0]: command[index + 1].split("=", 1)[1]
                    for index, part in enumerate(command)
                    if part == "--env"
                }
                payload = {
                    "status": "ok",
                    "player_a": env_values["ATV_OPPONENT"],
                    "player_b": env_values["ATV_SUBMITTER"],
                    "outcome": "a_wins",
                    "match_id": env_values["ATV_MATCH_ID"],
                    "game": env_values["ATV_GAME"],
                    "seed": int(env_values["ATV_SEED"]),
                }
                stdout = (json.dumps(payload) + "\n").encode()
            else:
                stdout = self.run_stdout
            return _command_result(
                command,
                exit_code=self.run_exit_code,
                stdout=stdout,
                stderr=b"bounded run diagnostic\n",
                timed_out=self.run_timed_out,
                output_limit_exceeded=self.run_output_limit_exceeded,
            )

        if command[1:3] == ("container", "ls"):
            return _command_result(
                command,
                exit_code=2 if self.cleanup_probe_error else 0,
                stderr=b"daemon unavailable\n" if self.cleanup_probe_error else b"",
            )
        if command[1:3] == ("image", "ls"):
            return _command_result(
                command,
                exit_code=2 if self.cleanup_probe_error else 0,
                stderr=b"daemon unavailable\n" if self.cleanup_probe_error else b"",
            )
        if command[1] == "rm" or command[1:3] == ("image", "rm"):
            return _command_result(command)
        raise AssertionError(f"unexpected fake-engine command: {command!r}")


def _bot(tmp_path: Path, source: str = "print('up', flush=True)\n") -> Path:
    path = tmp_path / "submitted.py"
    path.write_text(source, encoding="utf-8")
    return path


def _execute(tmp_path: Path, engine: FakeEngine, *, store: Path | None = None):
    return execute_league_score(
        submitter="alice",
        bot_path=_bot(tmp_path),
        match_id="local-20260720-001",
        game="lightcycles",
        seed=17,
        output_dir=tmp_path / "evidence",
        local_store=store,
        engine=engine,
    )


def _run_command(engine: FakeEngine) -> tuple[str, ...]:
    return next(command for command in engine.commands if command[1] == "run")


def test_fake_engine_end_to_end_stages_exact_bytes_binds_and_ingests(tmp_path):
    engine = FakeEngine()
    bot = _bot(tmp_path)
    expected_bytes = bot.read_bytes()
    expected_sha = hashlib.sha256(expected_bytes).hexdigest()
    store_dir = tmp_path / "league"

    receipt = execute_league_score(
        submitter="alice",
        bot_path=bot,
        match_id="local-20260720-001",
        game="lightcycles",
        seed=17,
        output_dir=tmp_path / "evidence",
        local_store=store_dir,
        engine=engine,
    )

    assert engine.staged_bot_bytes == expected_bytes
    assert receipt.bot_sha256 == expected_sha
    assert receipt.ingested is True
    assert receipt.result == {
        "status": "ok",
        "player_a": "byok-anchor",
        "player_b": "alice",
        "outcome": "a_wins",
        "match_id": "local-20260720-001",
        "game": "lightcycles",
        "seed": 17,
        "bot_sha256": expected_sha,
    }
    stored = LeagueStore(str(store_dir)).load_matches()
    assert len(stored) == 1
    assert stored[0]["match_id"] == "local-20260720-001"
    assert stored[0]["bot_sha256"] == expected_sha
    assert not (store_dir / ".league-score.lock").exists()

    bundle = receipt.bundle_dir
    checksums_bytes = (bundle / "checksums.json").read_bytes()
    assert bundle.parent.name == "sha256"
    assert bundle.name == hashlib.sha256(checksums_bytes).hexdigest()
    checksums = json.loads(checksums_bytes)
    for name, expected in checksums["files"].items():
        data = (bundle / name).read_bytes()
        assert len(data) == expected["size_bytes"]
        assert hashlib.sha256(data).hexdigest() == expected["sha256"]

    meta = json.loads((bundle / "meta.json").read_text())
    assert meta["binding_verified"] is True
    assert meta["cleanup"]["container_absent"] is True
    assert meta["cleanup"]["image_tag_absent"] is True
    assert meta["cleanup"]["image_removal_scope"] == "unique-run-tag-only"
    assert meta["cleanup"]["image_id_removal_attempted"] is False
    assert meta["cleanup"]["image_id_absence_claimed"] is False
    assert "image_id_absent" not in meta["cleanup"]
    assert meta["engine"]["endpoint"] == {
        "source": "docker-context",
        "context": "fake-local",
        "uri": "npipe:////./pipe/dockerDesktopLinuxEngine",
        "transport": "npipe",
        "local_socket_verified": True,
        "context_inspect_stdout_sha256": hashlib.sha256(
            b'"npipe:////./pipe/dockerDesktopLinuxEngine"\n'
        ).hexdigest(),
    }
    assert meta["engine"]["daemon"]["id"] == "fake-daemon-id"
    assert meta["engine"]["daemon"]["name"] == "fake-daemon"
    assert meta["engine"]["daemon"]["security_options"] == [
        "name=cgroupns",
        "name=seccomp,profile=builtin",
    ]
    assert meta["engine"]["daemon"]["rootless"] is False
    assert meta["resource_policy"]["network"] == "none"
    assert meta["resource_policy"]["read_only_root"] is True
    assert meta["match_spec"]["seed_semantics"].startswith("label-only")
    assert meta["local_store"]["requested"] is True
    assert "evidence before local-store" in meta["local_store"]["mutation_order"]
    assert (bundle / "materials" / "bot" / "main.py").read_bytes() == expected_bytes
    assert (
        bundle
        / "materials"
        / "arena-context"
        / "pkg"
        / "atv_bench"
        / "arena"
        / "__main__.py"
    ).is_file()


def test_fake_engine_builds_from_packaged_modules_and_pinned_base(tmp_path):
    engine = FakeEngine()
    _execute(tmp_path, engine)

    dockerfile = engine.context_files["Dockerfile"].decode()
    assert dockerfile.splitlines()[0] == f"FROM {ARENA_BASE_IMAGE}"
    assert "@sha256:" in dockerfile.splitlines()[0]
    for required in (
        "pkg/atv_bench/__init__.py",
        "pkg/atv_bench/arena/__init__.py",
        "pkg/atv_bench/arena/__main__.py",
        "pkg/atv_bench/arena/engine.py",
        "pkg/atv_bench/arena/referee.py",
        "league_entrypoint.py",
    ):
        assert required in engine.context_files
    assert (
        engine.context_files["pkg/atv_bench/arena/engine.py"]
        == (ROOT / "src" / "atv_bench" / "arena" / "engine.py").read_bytes()
    )
    assert "pkg/atv_bench/arena/live_server.py" not in engine.context_files
    assert "pkg/atv_bench/arena/sample_bots/greedy_survivor.py" not in engine.context_files


def test_run_argv_enforces_every_required_confinement_and_uses_no_shell_string(tmp_path):
    engine = FakeEngine()
    _execute(tmp_path, engine)
    command = _run_command(engine)

    assert isinstance(command, tuple)
    for flag, value in (
        ("--network", "none"),
        ("--ipc", "none"),
        ("--user", "65534:65534"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges"),
        ("--memory", "512m"),
        ("--memory-swap", "512m"),
        ("--cpus", "1.0"),
        ("--pids-limit", "128"),
        ("--ulimit", "nofile=64:64"),
    ):
        index = command.index(flag)
        assert command[index + 1] == value
    assert "--read-only" in command
    assert "--rm" in command
    assert "--init" in command
    assert "--tmpfs" in command
    assert "--mount" in command
    assert command[-2:] != ("sh", "-c")
    assert "sh" not in command
    assert "-c" not in command
    assert "sha256:" + "a" * 64 in command
    assert not any(part.startswith("atv-bench/league-score:") for part in command)


def test_reproduction_metadata_redacts_ephemeral_host_paths(tmp_path):
    engine = FakeEngine()
    receipt = _execute(tmp_path, engine)
    reproduction = (receipt.bundle_dir / "reproduction.json").read_text()
    doc = json.loads(reproduction)

    assert "${CONTEXT}" in doc["argv"]["build"]
    assert any("${STAGED_BOT_DIR}" in part for part in doc["argv"]["run"])
    assert "${IMAGE_TAG}" in doc["argv"]["build"]
    assert "${CONTAINER_NAME}" in doc["argv"]["run"]
    assert "atv-league-score-" not in reproduction
    assert str(tmp_path) not in reproduction


def test_forged_arena_identity_is_rejected_and_cleanup_still_runs(tmp_path):
    raw = {
        "status": "ok",
        "player_a": "famous-dev",
        "player_b": "alice",
        "outcome": "b_wins",
        "match_id": "local-20260720-001",
        "game": "lightcycles",
        "seed": 17,
    }
    engine = FakeEngine(run_stdout=(json.dumps(raw) + "\n").encode())

    with pytest.raises(LeagueExecutorError, match="MatchSpec"):
        _execute(tmp_path, engine)

    assert any(command[1] == "rm" for command in engine.commands)
    assert any(command[1:3] == ("image", "rm") for command in engine.commands)
    assert not (tmp_path / "evidence" / "sha256").exists()


def test_cleanup_removes_only_unique_run_tag_never_shared_image_id(tmp_path):
    engine = FakeEngine()
    _execute(tmp_path, engine)

    run_command = _run_command(engine)
    immutable_image_id = next(
        part for part in run_command if part == "sha256:" + "a" * 64
    )
    image_remove = next(
        command for command in engine.commands if command[1:3] == ("image", "rm")
    )
    assert image_remove[:3] == ("docker", "image", "rm")
    assert len(image_remove) == 4
    assert image_remove[3].startswith("atv-bench/league-score:")
    assert image_remove[3] != immutable_image_id
    assert "--force" not in image_remove
    assert not any(
        command[1:4] == ("image", "ls", "--all") for command in engine.commands
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"", "exactly one JSON result line"),
        (b"{}\n{}\n", "exactly one JSON result line"),
        (b"not-json\n", "League schema"),
    ],
)
def test_invalid_arena_output_fails_closed(payload, message, tmp_path):
    engine = FakeEngine(run_stdout=payload)
    with pytest.raises(LeagueExecutorError, match=message):
        _execute(tmp_path, engine)
    assert any(command[1] == "rm" for command in engine.commands)


@pytest.mark.parametrize(
    "engine",
    [
        FakeEngine(run_timed_out=True),
        FakeEngine(run_output_limit_exceeded=True),
        FakeEngine(run_exit_code=125),
    ],
)
def test_runtime_failure_is_infrastructure_error_not_a_scored_forfeit(engine, tmp_path):
    with pytest.raises(LeagueExecutorError):
        _execute(tmp_path, engine)
    assert not (tmp_path / "evidence" / "sha256").exists()
    assert any(command[1:3] == ("container", "ls") for command in engine.commands)


def test_cleanup_probe_error_cannot_be_reported_as_absence(tmp_path):
    engine = FakeEngine(cleanup_probe_error=True)
    with pytest.raises(LeagueExecutorError, match="cleanup could not be verified"):
        _execute(tmp_path, engine)
    assert not (tmp_path / "evidence" / "sha256").exists()


def test_cleanup_failure_is_surfaced_alongside_primary_execution_error(tmp_path):
    engine = FakeEngine(run_exit_code=125, cleanup_probe_error=True)
    with pytest.raises(LeagueExecutorError) as raised:
        _execute(tmp_path, engine)

    message = str(raised.value)
    assert "sandboxed League match failed with exit code 125" in message
    assert "additionally" in message
    assert "cleanup could not be verified" in message
    assert "container absence was not verified" in message
    assert "unique image-tag absence was not verified" in message


def test_refuses_to_execute_inside_github_actions(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    engine = FakeEngine()
    with pytest.raises(LeagueExecutorError, match="refuses to run inside GitHub Actions"):
        execute_league_score(
            submitter="alice",
            bot_path=_bot(tmp_path),
            match_id="local-20260720-actions",
            game="lightcycles",
            seed=17,
            output_dir=tmp_path / "evidence",
            engine=engine,
        )
    assert engine.commands == []


@pytest.mark.parametrize(
    "remote_host",
    [
        "tcp://127.0.0.1:2375",
        "tcp://docker.example.test:2376",
        "ssh://operator@docker.example.test",
    ],
)
def test_rejects_remote_docker_host_before_any_engine_command(
    monkeypatch, remote_host, tmp_path
):
    monkeypatch.setenv("DOCKER_HOST", remote_host)
    engine = FakeEngine()
    with pytest.raises(LeagueExecutorError, match="verified local unix:// or npipe://"):
        _execute(tmp_path, engine)
    assert engine.commands == []


def test_rejects_remote_docker_context_before_build_or_run(tmp_path):
    engine = FakeEngine(context_host="ssh://operator@docker.example.test")
    with pytest.raises(LeagueExecutorError, match="verified local unix:// or npipe://"):
        _execute(tmp_path, engine)
    assert any(command[1:] == ("context", "show") for command in engine.commands)
    assert any(command[1:3] == ("context", "inspect") for command in engine.commands)
    assert not any(command[1] in {"info", "build", "run"} for command in engine.commands)


def test_local_docker_host_is_bound_and_attested(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    engine = FakeEngine()
    receipt = _execute(tmp_path, engine)
    meta = json.loads((receipt.bundle_dir / "meta.json").read_text())

    assert not any(command[1] == "context" for command in engine.commands)
    assert meta["engine"]["endpoint"] == {
        "source": "DOCKER_HOST",
        "context": None,
        "uri": "unix:///var/run/docker.sock",
        "transport": "unix",
        "local_socket_verified": True,
        "context_inspect_stdout_sha256": None,
    }
    assert meta["engine"]["daemon"]["server_version"] == "28.0.0"


def test_bot_validation_rejects_non_utf8_and_oversize_before_docker(tmp_path):
    for name, payload in (
        ("binary.py", b"\xff\xfe"),
        ("huge.py", b"x" * (256 * 1024 + 1)),
    ):
        path = tmp_path / name
        path.write_bytes(payload)
        engine = FakeEngine()
        with pytest.raises(LeagueExecutorError):
            execute_league_score(
                submitter="alice",
                bot_path=path,
                match_id="safe-1",
                game="lightcycles",
                seed=0,
                output_dir=tmp_path / f"out-{name}",
                engine=engine,
            )
        assert engine.commands == []


def test_bot_symlink_is_rejected_before_docker(tmp_path):
    target = _bot(tmp_path)
    link = tmp_path / "linked.py"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("symlink creation is unavailable on this host")
    engine = FakeEngine()
    with pytest.raises(LeagueExecutorError, match="symlink"):
        execute_league_score(
            submitter="alice",
            bot_path=link,
            match_id="safe-1",
            game="lightcycles",
            seed=0,
            output_dir=tmp_path / "out",
            engine=engine,
        )
    assert engine.commands == []


def test_docker_cli_adapter_uses_popen_argv_shell_false(monkeypatch):
    real_popen = subprocess.Popen
    observed: dict[str, object] = {}

    def guarded_popen(*args, **kwargs):
        observed["argv"] = args[0]
        observed["shell"] = kwargs.get("shell")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr("atv_bench.league_executor.subprocess.Popen", guarded_popen)
    engine = DockerCliEngine(sys.executable)
    result = engine.execute(
        [engine.executable, "-c", "print('argv-only')"],
        timeout_seconds=10,
        output_limit_bytes=4096,
    )

    assert result.ok
    assert result.stdout.strip() == b"argv-only"
    assert isinstance(observed["argv"], tuple)
    assert observed["shell"] is False


def test_docker_cli_adapter_stops_storing_after_output_bound():
    engine = DockerCliEngine(sys.executable)
    result = engine.execute(
        [engine.executable, "-c", "import sys; sys.stdout.write('x' * 200000)"],
        timeout_seconds=10,
        output_limit_bytes=1024,
    )
    assert result.output_limit_exceeded is True
    assert len(result.stdout) + len(result.stderr) <= 1024
    assert result.stdout_total_bytes > 1024


def test_materialized_context_does_not_require_repository_arena_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = materialize_arena_context(tmp_path / "context")
    assert (context.root / "Dockerfile").is_file()
    assert (context.root / "pkg" / "atv_bench" / "arena" / "__main__.py").is_file()
    assert context.context_sha256
    assert context.arena_source_sha256


def test_evidence_is_committed_before_optional_store_mutation(monkeypatch, tmp_path):
    order: list[str] = []
    from atv_bench import league_executor as executor
    from atv_bench import publish

    real_write_bundle = executor._write_bundle

    def ordered_write_bundle(*args, **kwargs):
        order.append("bundle")
        return real_write_bundle(*args, **kwargs)

    def ordered_ingest(*args, **kwargs):
        del args, kwargs
        order.append("ingest")
        return True

    monkeypatch.setattr(executor, "_write_bundle", ordered_write_bundle)
    monkeypatch.setattr(publish, "ingest_result", ordered_ingest)
    receipt = _execute(tmp_path, FakeEngine(), store=tmp_path / "league")
    assert receipt.ingested is True
    assert order == ["bundle", "ingest"]


def test_local_store_lock_serializes_cross_handle_ingestion(tmp_path):
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with _exclusive_store_lock(tmp_path / "league"):
            acquired.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert acquired.wait(timeout=5)
    try:
        with pytest.raises(LeagueExecutorError, match="store lock"):
            with _exclusive_store_lock(tmp_path / "league", timeout_seconds=0.1):
                pass
    finally:
        release.set()
        holder.join(timeout=5)
    assert not holder.is_alive()


def _build_wheel(tmp_path: Path) -> Path:
    out = tmp_path / "dist"
    out.mkdir()
    uv = shutil.which("uv")
    if uv:
        command = [uv, "build", "--wheel", "--out-dir", str(out)]
    elif importlib.util.find_spec("build") is not None:
        command = [sys.executable, "-m", "build", "--wheel", "--outdir", str(out)]
    else:
        pytest.skip("uv or python-build is required for the installed-wheel regression")
    subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, "UV_LINK_MODE": "copy"},
    )
    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def test_built_wheel_can_generate_arena_context_without_checkout(tmp_path):
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as package:
        names = set(package.namelist())
    for required in (
        "atv_bench/league_executor.py",
        "atv_bench/arena/__init__.py",
        "atv_bench/arena/__main__.py",
        "atv_bench/arena/engine.py",
        "atv_bench/arena/referee.py",
    ):
        assert required in names
        assert PurePosixPath(required).parts[0] == "atv_bench"

    context_out = tmp_path / "installed-context"
    script = (
        "import json,sys; "
        f"sys.path.insert(0, {str(wheel)!r}); "
        "from pathlib import Path; "
        "from atv_bench.league_executor import materialize_arena_context; "
        f"c=materialize_arena_context(Path({str(context_out)!r})); "
        "print(json.dumps({'context':c.context_sha256,'arena':c.arena_source_sha256}))"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", script],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    assert json.loads(proc.stdout)["context"]
    assert (context_out / "Dockerfile").is_file()
    assert (context_out / "pkg" / "atv_bench" / "arena" / "__main__.py").is_file()

    installed = tmp_path / "installed-venv"
    venv.EnvBuilder(with_pip=True).create(installed)
    scripts = installed / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    cli = scripts / ("atv-bench.exe" if os.name == "nt" else "atv-bench")
    installed_site = subprocess.run(
        [str(python), "-c", "import site; print(site.getsitepackages()[-1])"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    current_site = Path(site.getsitepackages()[-1])
    (Path(installed_site) / "atv-test-dependencies.pth").write_text(
        str(current_site),
        encoding="utf-8",
    )
    subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    help_result = subprocess.run(
        [str(cli), "league-score", "--help"],
        cwd=tmp_path,
        check=True,
        text=True,
        capture_output=True,
    )
    assert "--submitter" in help_result.stdout
    assert "--match-id" in help_result.stdout


def test_github_workflows_cannot_invoke_local_league_executor():
    workflow_root = ROOT / ".github" / "workflows"
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(workflow_root.glob("*.yml"))
    ).lower()
    assert "league-score" not in combined
    assert "atv_bench.league_executor" not in combined


def test_symlinked_content_address_root_is_rejected(tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    output = tmp_path / "evidence"
    output.mkdir()
    try:
        os.symlink(target, output / "sha256", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable on this host")
    with pytest.raises(LeagueExecutorError, match="sha256 directory"):
        _execute(tmp_path, FakeEngine())


def test_cli_league_score_maps_all_inputs_and_prints_receipt(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    result = {
        "status": "ok",
        "player_a": "byok-anchor",
        "player_b": "alice",
        "outcome": "a_wins",
        "match_id": "m-1",
        "game": "lightcycles",
        "seed": 3,
        "bot_sha256": "b" * 64,
    }
    receipt = LeagueScoreReceipt(
        bundle_dir=tmp_path / "out" / "sha256" / ("c" * 64),
        bundle_sha256="c" * 64,
        bot_sha256="b" * 64,
        result=result,
        ingested=True,
    )

    def fake_execute(**kwargs):
        captured.update(kwargs)
        return receipt

    monkeypatch.setattr(
        "atv_bench.league_executor.execute_league_score",
        fake_execute,
    )
    bot = _bot(tmp_path)
    runner = CliRunner()
    invocation = runner.invoke(
        app,
        [
            "league-score",
            "--submitter",
            "alice",
            "--bot",
            str(bot),
            "--match-id",
            "m-1",
            "--game",
            "lightcycles",
            "--seed",
            "3",
            "--out",
            str(tmp_path / "out"),
            "--store",
            str(tmp_path / "league"),
            "--json",
        ],
    )

    assert invocation.exit_code == 0, invocation.stdout
    assert json.loads(invocation.stdout)["bundle_sha256"] == "c" * 64
    assert captured == {
        "submitter": "alice",
        "bot_path": bot,
        "match_id": "m-1",
        "game": "lightcycles",
        "seed": 3,
        "output_dir": tmp_path / "out",
        "local_store": tmp_path / "league",
    }


def _docker_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        return subprocess.run(
            [docker, "info"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            shell=False,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.integration
@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_real_docker_league_score_from_packaged_context(tmp_path):
    bot = _bot(
        tmp_path,
        textwrap.dedent(
            """\
            import sys
            for _line in sys.stdin:
                print("up", flush=True)
            """
        ),
    )
    receipt = execute_league_score(
        submitter="docker-probe",
        bot_path=bot,
        match_id="docker-integration-1",
        game="lightcycles",
        seed=5,
        output_dir=tmp_path / "evidence",
    )
    assert receipt.result["status"] == "ok"
    assert receipt.result["player_b"] == "docker-probe"
    assert receipt.result["match_id"] == "docker-integration-1"
    assert receipt.result["seed"] == 5
    assert receipt.bundle_dir.is_dir()
    assert (receipt.bundle_dir / "checksums.json").is_file()
    meta = json.loads((receipt.bundle_dir / "meta.json").read_text())
    assert meta["engine"]["endpoint"]["transport"] in {"npipe", "unix"}
    assert meta["engine"]["endpoint"]["local_socket_verified"] is True
    assert meta["engine"]["daemon"]["id"]
    assert meta["engine"]["daemon"]["name"]
    assert meta["engine"]["daemon"]["server_version"]
    assert isinstance(meta["engine"]["daemon"]["security_options"], list)
    assert meta["cleanup"]["image_removal_scope"] == "unique-run-tag-only"
    assert meta["cleanup"]["image_id_removal_attempted"] is False
    assert meta["cleanup"]["image_id_absence_claimed"] is False
    assert (
        len((receipt.bundle_dir / "run.stdout.log").read_bytes())
        <= RUN_OUTPUT_LIMIT_BYTES
    )
