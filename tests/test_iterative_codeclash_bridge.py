"""Focused integration seams for persistent CodeClash players and log feedback."""
from __future__ import annotations

import io
import secrets
import shutil
import subprocess
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    EvidenceSource,
    HarnessAdapter,
    Usage,
)
from atv_bench.capture import MAX_FILE_BYTES, CaptureRejected
from atv_bench.codeclash_env import codeclash_available


class _Tree:
    def __init__(self):
        self.tree = {"main.py": "ROUND = 0\n"}
        self.feedback = {
            0: {"results.json": '{"winner":"Tie"}'},
            1: {"results.json": '{"winner":"player"}'},
        }

    def read_tree(self):
        return dict(self.tree)

    def write_tree(self, files):
        self.tree = dict(files)

    def read_feedback(self, round_number):
        return dict(self.feedback.get(round_number, {}))


class _Adapter(HarnessAdapter):
    name = "fake-integration"
    calls = 0
    goals: list[str] = []

    def run(self, request: AdapterRequest):
        type(self).calls += 1
        type(self).goals.append(request.goal)
        path = Path(request.repo_path) / request.bot_file
        current = int(path.read_bytes().decode("utf-8").split("=")[1].strip())
        path.write_bytes(f"ROUND = {current + 1}\n".encode("utf-8"))
        return AdapterResult(
            status=AdapterStatus.OK,
            diff="",
            log="",
            usage=Usage(),
            model=request.model,
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=False,
        )


def test_harness_player_keeps_one_core_but_invokes_fresh_round_processes(monkeypatch):
    from atv_bench import integration

    _Adapter.calls = 0
    _Adapter.goals = []
    tree = _Tree()

    class FakePlayer:
        def __init__(self, config, environment, game_context):
            self.config = config
            self.name = config["name"]
            self.environment = environment
            self.game_context = game_context
            self._metadata = {"name": self.name}

    monkeypatch.setitem(integration.ADAPTERS, "fake-integration", _Adapter)
    monkeypatch.setattr(
        integration,
        "import_codeclash",
        lambda: SimpleNamespace(Player=FakePlayer),
    )
    monkeypatch.setattr(
        integration,
        "_DockerTreeContainer",
        lambda *args, **kwargs: tree,
    )
    player_class = integration._make_harness_player("fake-integration")
    context = SimpleNamespace(
        prompts={"edit": "Improve.", "_version": "edit@1"},
        round=1,
        name="LightCycles",
        working_dir="/workspace",
    )
    player = player_class(
        {
            "name": "player",
            "config": {
                "model": "requested-model",
                "adaptation": "iterative",
                "manifest_capabilities": {"resumable": False},
            },
        },
        object(),
        context,
    )
    core_id = id(player._atv_core)
    player.run()
    context.round = 2
    player.run()

    assert id(player._atv_core) == core_id
    assert _Adapter.calls == 2
    assert tree.tree["main.py"] == "ROUND = 2\n"
    assert '"winner":"player"' in _Adapter.goals[1]
    assert player._metadata["atv"]["trial_unit"] == "tournament"
    assert set(player._metadata["atv"]["rounds"]) == {1, 2}


@pytest.mark.skipif(
    not codeclash_available(),
    reason="pinned CodeClash dependency is not installed",
)
def test_docker_feedback_bridge_reads_exact_codeclash_round_log_path(
    monkeypatch,
):
    from atv_bench import integration
    from atv_bench.integration import _DockerTreeContainer

    seen = {}

    def fake_copy(_env, source, destination):
        seen["source"] = source
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "results.json").write_text('{"winner":"player"}')
        return ("results.json",)

    monkeypatch.setattr(integration, "_bounded_copy_from_container", fake_copy)
    bridge = _DockerTreeContainer(object(), "/workspace", logs_root="/logs")
    feedback = bridge.read_feedback(1)

    assert seen["source"] == "/logs/rounds/1"
    assert feedback == {"results.json": '{"winner":"player"}'}


@pytest.mark.skipif(
    not codeclash_available(),
    reason="pinned CodeClash dependency is not installed",
)
def test_docker_tree_write_uses_pinned_codeclash_execute_contract(monkeypatch):
    from atv_bench import integration
    from atv_bench.integration import _DockerTreeContainer

    class FakeEnvironment:
        def __init__(self):
            self.calls = []

        def execute(self, action, cwd="", *, timeout=None):
            assert isinstance(action, dict)
            self.calls.append((action, cwd, timeout))
            return {"returncode": 0, "output": "", "exception_info": ""}

    copied = []
    monkeypatch.setattr(
        integration,
        "_write_bytes_to_container",
        lambda env, destination, data: copied.append(
            (env, destination, data)
        ),
    )
    environment = FakeEnvironment()
    bridge = _DockerTreeContainer(environment, "/workspace")
    monkeypatch.setattr(
        bridge,
        "read_tree",
        lambda: {"old.py": "old\n", "main.py": "before\n"},
    )

    bridge.write_tree({"main.py": "after\n", "pkg/helper.py": "HELPER = True\n"})

    commands = [item[0]["command"] for item in environment.calls]
    assert commands == [
        "rm -f -- /workspace/old.py",
        "mkdir -p -- /workspace",
        "mkdir -p -- /workspace/pkg",
    ]
    assert all(item[1] == "/workspace" for item in environment.calls)
    assert [item[1] for item in copied] == [
        "/workspace/main.py",
        "/workspace/pkg/helper.py",
    ]
    assert [item[2] for item in copied] == [
        b"after\n",
        b"HELPER = True\n",
    ]


def _tar_stream(*entries: tuple[str, bytes | None, str | None]) -> io.BytesIO:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data, link_target in entries:
            info = tarfile.TarInfo(name)
            if link_target is not None:
                info.type = tarfile.SYMTYPE
                info.linkname = link_target
                archive.addfile(info)
            elif data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    stream.seek(0)
    return stream


def test_container_tree_tar_transfer_is_bounded_and_link_safe(tmp_path):
    from atv_bench.integration import _materialize_bounded_tar

    destination = tmp_path / "valid"
    captured = _materialize_bounded_tar(
        _tar_stream(
            ("./pkg", None, None),
            ("./pkg/helper.py", b"VALUE = 1\n", None),
            ("./main.py", b"print('ok')\n", None),
        ),
        destination,
    )
    assert captured == ("pkg/helper.py", "main.py")
    assert (destination / "main.py").read_text(encoding="utf-8") == "print('ok')\n"

    with pytest.raises(CaptureRejected, match="link is not allowed"):
        _materialize_bounded_tar(
            _tar_stream(("./escape", None, "../../outside")),
            tmp_path / "link",
        )


def test_container_tar_command_excludes_codeclash_git_and_cache_trees():
    from atv_bench.integration import (
        IGNORED_CONTAINER_TREE_DIRS,
        _container_tar_command,
    )

    command = _container_tar_command("docker", "container-id", "/work")
    assert command[:4] == ["docker", "exec", "container-id", "tar"]
    for directory in IGNORED_CONTAINER_TREE_DIRS:
        assert f"--exclude=./{directory}" in command
        assert f"--exclude=*/{directory}" in command
    assert command[-5:] == ["-C", "/work", "-cf", "-", "."]


@pytest.mark.integration
def test_real_docker_codeclash_copy_excludes_git_and_cache_trees(tmp_path):
    from atv_bench.integration import _DockerTreeContainer

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    image = (
        "docker.io/library/python@sha256:"
        "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
    )
    daemon = subprocess.run(
        [docker, "info"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if daemon.returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    cached = subprocess.run(
        [docker, "image", "inspect", image],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if cached.returncode != 0:
        pytest.skip("digest-pinned Python image is not cached")

    name = f"atv-codeclash-copy-{secrets.token_hex(4)}"
    try:
        subprocess.run(
            [docker, "run", "-d", "--name", name, image, "sleep", "120"],
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
        )
        setup = (
            "from pathlib import Path;"
            "r=Path('/work');"
            "(r/'.git/objects').mkdir(parents=True);"
            "(r/'__pycache__').mkdir();"
            "[(r/'.git/objects'/f'{i:03d}').write_bytes(b'x') for i in range(100)];"
            "[(r/'__pycache__'/f'{i:03d}.pyc').write_bytes(b'x') for i in range(100)];"
            "(r/'main.py').write_bytes(b'VALUE = 1\\n')"
        )
        subprocess.run(
            [docker, "exec", name, "python", "-c", setup],
            check=True,
            timeout=60,
        )
        environment = SimpleNamespace(
            container_id=name,
            config=SimpleNamespace(executable=docker),
        )
        tree = _DockerTreeContainer(environment, "/work").read_tree()
        assert tree == {"main.py": "VALUE = 1\n"}
    finally:
        subprocess.run(
            [docker, "rm", "-f", name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )


@pytest.mark.integration
def test_real_codeclash_lightcycles_runs_persistent_model_free_round(
    tmp_path,
    monkeypatch,
):
    from atv_bench import integration
    from atv_bench.codeclash_env import (
        CODECLASH_LIGHTCYCLES_PIN,
        CODECLASH_PIN,
        import_codeclash,
    )
    from atv_bench.config import build_pvp_config

    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    daemon = subprocess.run(
        [docker, "info"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=False,
    )
    if daemon.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

    class NorthAdapter(HarnessAdapter):
        name = "model-free-north"

        def run(self, request: AdapterRequest):
            Path(request.repo_path, request.bot_file).write_bytes(
                b"def get_move(obs):\n    return 'N'\n"
            )
            return AdapterResult(
                status=AdapterStatus.OK,
                diff="",
                log="model-free CodeClash fixture",
                usage=Usage(source=EvidenceSource.UNAVAILABLE),
                model=request.model,
                model_source=EvidenceSource.HARNESS_REPORTED,
                model_verified=False,
            )

    class EastAdapter(HarnessAdapter):
        name = "model-free-east"

        def run(self, request: AdapterRequest):
            Path(request.repo_path, request.bot_file).write_bytes(
                b"def get_move(obs):\n    return 'E'\n"
            )
            return AdapterResult(
                status=AdapterStatus.OK,
                diff="",
                log="model-free CodeClash fixture",
                usage=Usage(source=EvidenceSource.UNAVAILABLE),
                model=request.model,
                model_source=EvidenceSource.HARNESS_REPORTED,
                model_verified=False,
            )

    import_codeclash()
    from codeclash.tournaments.pvp import PvpTournament

    def active_names() -> set[str]:
        return set(
            subprocess.run(
                [
                    docker,
                    "ps",
                    "-a",
                    "--filter",
                    "name=minisweagent-",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
                check=True,
            ).stdout.split()
        )

    before = active_names()
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setitem(integration.ADAPTERS, "claude-code", NorthAdapter)
    monkeypatch.setitem(integration.ADAPTERS, "copilot-cli", EastAdapter)
    integration._player_class_cache.clear()
    config = build_pvp_config(
        game="lightcycles",
        a="claude-code",
        b="copilot-cli",
        model="model-free-fixture",
        rounds=1,
        budget={"max_turns": 1, "max_seconds": 30, "max_tokens": 1},
    )
    integration.register()
    try:
        tournament = PvpTournament(config, output_dir=tmp_path, cleanup=True)
        tournament.run()
    finally:
        integration.unregister()
        integration._player_class_cache.clear()

    assert (tmp_path / "metadata.json").is_file()
    assert set(tournament._metadata["round_stats"]) == {0, 1}
    for agent in tournament.agents:
        rounds = agent._metadata["atv"]["rounds"]
        assert set(rounds) == {1}
        assert rounds[1]["fresh_harness_process"] is True
        assert rounds[1]["fresh_model_context"] is True
        assert rounds[1]["input_tree_sha256"] != rounds[1]["output_tree_sha256"]

    game_commit = subprocess.run(
        [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "codeclash/lightcycles",
            "git",
            "rev-parse",
            "HEAD",
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()
    assert game_commit == CODECLASH_LIGHTCYCLES_PIN
    assert active_names() == before
    assert CODECLASH_PIN == "f0694c64ecf6abfca2bc867bad2de9333fef5be8"


def test_container_tree_tar_transfer_rejects_file_and_archive_bombs(tmp_path):
    from atv_bench.integration import _materialize_bounded_tar

    with pytest.raises(CaptureRejected, match="file is too large"):
        _materialize_bounded_tar(
            _tar_stream(("./huge.py", b"x" * (MAX_FILE_BYTES + 1), None)),
            tmp_path / "file-bomb",
        )

    with pytest.raises(CaptureRejected, match="transferred bytes"):
        _materialize_bounded_tar(
            _tar_stream(("./main.py", b"print('ok')\n", None)),
            tmp_path / "archive-bomb",
            archive_limit=512,
        )


def test_codeclash_source_profile_keeps_large_utf8_tree_and_skips_binary(tmp_path):
    from atv_bench.integration import (
        MAX_CODECLASH_ARCHIVE_BYTES,
        MAX_CODECLASH_ARCHIVE_DIRECTORIES,
        MAX_CODECLASH_ARCHIVE_ENTRIES,
        MAX_CODECLASH_ARCHIVE_FILE_BYTES,
        MAX_CODECLASH_ARCHIVE_FILES,
        MAX_CODECLASH_ARCHIVE_TOTAL_BYTES,
        _materialize_bounded_tar,
        _read_codeclash_text_tree,
    )

    entries = [
        (f"./game/file_{index:03d}.go", b"package game\n", None)
        for index in range(100)
    ]
    entries.append(("./game/battlesnake", b"\x00\x01\x02", None))
    destination = tmp_path / "codeclash-tree"
    captured = _materialize_bounded_tar(
        _tar_stream(("./game", None, None), *entries),
        destination,
        archive_limit=MAX_CODECLASH_ARCHIVE_BYTES,
        max_entries=MAX_CODECLASH_ARCHIVE_ENTRIES,
        max_directories=MAX_CODECLASH_ARCHIVE_DIRECTORIES,
        max_files=MAX_CODECLASH_ARCHIVE_FILES,
        max_total_bytes=MAX_CODECLASH_ARCHIVE_TOTAL_BYTES,
        max_file_bytes=MAX_CODECLASH_ARCHIVE_FILE_BYTES,
    )

    tree = _read_codeclash_text_tree(destination, captured)
    assert len(tree) == 100
    assert tree["game/file_000.go"] == "package game\n"
    assert "game/battlesnake" not in tree
