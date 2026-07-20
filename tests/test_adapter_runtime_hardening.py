"""Security and lifecycle tests for the local harness process runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import atv_bench.adapters.contract as contract_module
from atv_bench.adapters.contract import (
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    Budget,
    CleanupStatus,
    CommandHarnessAdapter,
    EvidenceSource,
    MAX_STDERR_BYTES,
    MAX_STDOUT_BYTES,
    ProcessTreeCleanupResult,
    Usage,
    build_child_environment,
    run_process,
)


def _seed_repo(path: Path) -> None:
    path.mkdir()
    (path / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        [
            "git", "-c", "user.email=a@b.c", "-c", "user.name=atv",
            "commit", "-qm", "seed",
        ],
        cwd=path,
        check=True,
    )


def test_environment_is_default_deny_with_explicit_allowlist(tmp_path):
    source = dict(os.environ)
    source["ATV_ALLOWED_TEST_VALUE"] = "visible"
    source["ATV_SECRET_CANARY"] = "must-not-leak"
    source.update(
        {
            "HOME": "home-canary",
            "USERPROFILE": "profile-canary",
            "APPDATA": "appdata-canary",
            "LOCALAPPDATA": "localappdata-canary",
            "XDG_CONFIG_HOME": "xdg-canary",
        }
    )
    result = run_process(
        [
            sys.executable,
            "-c",
            (
                "import json,os;"
                "print(json.dumps({"
                "'allowed':os.environ.get('ATV_ALLOWED_TEST_VALUE'),"
                "'secret':os.environ.get('ATV_SECRET_CANARY'),"
                "'home':os.environ.get('HOME'),"
                "'profile':os.environ.get('USERPROFILE'),"
                "'appdata':os.environ.get('APPDATA'),"
                "'localappdata':os.environ.get('LOCALAPPDATA'),"
                "'xdg':os.environ.get('XDG_CONFIG_HOME')}))"
            ),
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        env_allowlist=("ATV_ALLOWED_TEST_VALUE",),
        env_source=source,
    )
    payload = json.loads(result.stdout)
    assert payload == {
        "allowed": "visible",
        "secret": None,
        "home": None,
        "profile": None,
        "appdata": None,
        "localappdata": None,
        "xdg": None,
    }
    assert "ATV_ALLOWED_TEST_VALUE" in result.runtime.environment_keys
    assert "ATV_SECRET_CANARY" not in result.runtime.environment_keys
    assert {
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "XDG_CONFIG_HOME"
    }.isdisjoint(result.runtime.environment_keys)


def test_home_and_config_environment_require_explicit_opt_in():
    source = {
        "PATH": "safe",
        "HOME": "explicit-home",
        "APPDATA": "explicit-appdata",
    }
    assert build_child_environment(source=source) == {"PATH": "safe"}
    assert build_child_environment(("HOME",), source=source) == {
        "PATH": "safe",
        "HOME": "explicit-home",
    }


def test_environment_builder_rejects_wildcards_and_never_copies_canary():
    source = {"PATH": "safe", "SECRET_TOKEN": "canary"}
    env = build_child_environment(source=source)
    assert env == {"PATH": "safe"}
    with pytest.raises(ValueError):
        build_child_environment(("*",), source=source)


def test_stdout_and_stderr_are_streamed_into_bounded_tail_buffers(tmp_path):
    result = run_process(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                "sys.stdout.write('A'*10000+'OUT-END');"
                "sys.stderr.write('B'*9000+'ERR-END')"
            ),
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        max_stdout_bytes=128,
        max_stderr_bytes=96,
    )
    assert len(result.stdout.encode("utf-8")) <= 128
    assert len(result.stderr.encode("utf-8")) <= 96
    assert result.stdout.endswith("OUT-END")
    assert result.stderr.endswith("ERR-END")
    assert result.runtime.stdout_bytes == 10007
    assert result.runtime.stderr_bytes == 9007
    assert result.runtime.stdout_truncated is True
    assert result.runtime.stderr_truncated is True


def test_callers_cannot_raise_process_or_adapter_capture_above_global_maxima(tmp_path):
    result = run_process(
        [
            sys.executable,
            "-c",
            (
                "import sys;"
                f"sys.stdout.write('A'*{MAX_STDOUT_BYTES + 4096});"
                f"sys.stderr.write('B'*{MAX_STDERR_BYTES + 4096})"
            ),
        ],
        cwd=tmp_path,
        timeout_seconds=10,
        max_stdout_bytes=10**12,
        max_stderr_bytes=10**12,
    )
    assert len(result.stdout.encode("utf-8")) == MAX_STDOUT_BYTES
    assert len(result.stderr.encode("utf-8")) == MAX_STDERR_BYTES
    assert result.runtime.stdout_truncated is True
    assert result.runtime.stderr_truncated is True

    adapter = CommandHarnessAdapter(
        [sys.executable, "-c", "pass"],
        max_stdout_bytes=10**12,
        max_stderr_bytes=10**12,
    )
    assert adapter.max_stdout_bytes == MAX_STDOUT_BYTES
    assert adapter.max_stderr_bytes == MAX_STDERR_BYTES


def _write_process_tree_fixture(
    tmp_path: Path, *, parent_waits: bool = True
) -> tuple[Path, Path]:
    marker = tmp_path / "descendant-survived.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import pathlib,sys,time\n"
        "time.sleep(0.8)\n"
        "pathlib.Path(sys.argv[1]).write_text('survived')\n",
        encoding="utf-8",
    )
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n"
        + ("time.sleep(60)\n" if parent_waits else ""),
        encoding="utf-8",
    )
    return parent, marker


def test_timeout_kills_the_full_process_tree(tmp_path):
    parent, marker = _write_process_tree_fixture(tmp_path)
    result = run_process(
        [sys.executable, str(parent), str(tmp_path / "child.py"), str(marker)],
        cwd=tmp_path,
        timeout_seconds=0.15,
        termination_grace_seconds=0.1,
    )
    assert result.runtime.timed_out is True
    assert result.runtime.cancelled is False
    assert result.runtime.process_tree_cleanup_attempted is True
    assert result.runtime.process_tree_cleanup_status is CleanupStatus.SUCCEEDED
    assert result.runtime.process_tree_cleanup_succeeded is True
    time.sleep(1.0)
    assert not marker.exists(), "descendant survived its timed-out harness parent"


def test_cancellation_kills_the_full_process_tree(tmp_path):
    parent, marker = _write_process_tree_fixture(tmp_path)
    cancelled = threading.Event()
    timer = threading.Timer(0.15, cancelled.set)
    timer.start()
    try:
        result = run_process(
            [sys.executable, str(parent), str(tmp_path / "child.py"), str(marker)],
            cwd=tmp_path,
            timeout_seconds=30,
            cancel_event=cancelled,
            termination_grace_seconds=0.1,
        )
    finally:
        timer.cancel()
    assert result.runtime.cancelled is True
    assert result.runtime.timed_out is False
    assert result.runtime.process_tree_cleanup_attempted is True
    assert result.runtime.process_tree_cleanup_status is CleanupStatus.SUCCEEDED
    assert result.runtime.process_tree_cleanup_succeeded is True
    time.sleep(1.0)
    assert not marker.exists(), "descendant survived cancellation"


def test_normal_parent_exit_kills_orphan_descendant(tmp_path):
    parent, marker = _write_process_tree_fixture(tmp_path, parent_waits=False)
    result = run_process(
        [sys.executable, str(parent), str(tmp_path / "child.py"), str(marker)],
        cwd=tmp_path,
        timeout_seconds=10,
        termination_grace_seconds=0.1,
    )
    assert result.runtime.exit_code == 0
    assert result.runtime.timed_out is False
    assert result.runtime.cancelled is False
    assert result.runtime.process_tree_cleanup_attempted is True
    assert result.runtime.process_tree_cleanup_status is CleanupStatus.SUCCEEDED
    assert result.runtime.process_tree_cleanup_succeeded is True
    time.sleep(1.0)
    assert not marker.exists(), "orphan survived normal harness-parent exit"


def test_forced_cleanup_verification_failure_is_typed_without_leaking_process(
    tmp_path, monkeypatch
):
    parent, marker = _write_process_tree_fixture(tmp_path)
    real_terminate = contract_module._terminate_process_tree

    def terminate_then_report_failure(*args, **kwargs):
        actual = real_terminate(*args, **kwargs)
        assert actual.status is CleanupStatus.SUCCEEDED
        return ProcessTreeCleanupResult(
            CleanupStatus.FAILED, "forced cleanup verification failure"
        )

    monkeypatch.setattr(
        contract_module, "_terminate_process_tree", terminate_then_report_failure
    )
    result = run_process(
        [sys.executable, str(parent), str(tmp_path / "child.py"), str(marker)],
        cwd=tmp_path,
        timeout_seconds=0.15,
        termination_grace_seconds=0.1,
    )
    assert result.runtime.timed_out is True
    assert result.runtime.process_tree_cleanup_attempted is True
    assert result.runtime.process_tree_cleanup_status is CleanupStatus.FAILED
    assert result.runtime.process_tree_cleanup_succeeded is False
    assert result.runtime.process_tree_cleanup_error == (
        "forced cleanup verification failure"
    )
    assert (
        contract_module._runtime_terminal_status(result.runtime)
        is AdapterStatus.CLEANUP_FAILED
    )
    time.sleep(1.0)
    assert not marker.exists(), "forced failure test leaked a descendant process"

    repo = tmp_path / "repo"
    _seed_repo(repo)
    adapter_result = CommandHarnessAdapter(
        [sys.executable, "-c", "import time; time.sleep(60)"]
    ).run(
        AdapterRequest(
            repo_path=str(repo),
            goal="Timeout safely.",
            budget=Budget(max_seconds=0.15),
        )
    )
    assert adapter_result.status is AdapterStatus.CLEANUP_FAILED
    assert (
        adapter_result.runtime.process_tree_cleanup_status is CleanupStatus.FAILED
    )


def test_command_adapter_captures_all_change_shapes_and_marks_claims_unverified(tmp_path):
    repo = tmp_path / "repo"
    _seed_repo(repo)
    script = tmp_path / "harness.py"
    script.write_text(
        "import json,os,pathlib,subprocess\n"
        "req=json.loads(os.environ['ATV_BENCH_REQUEST_JSON'])\n"
        "root=pathlib.Path(req['repo_path'])\n"
        "(root/'main.py').write_text('VALUE = 2\\n')\n"
        "subprocess.run(['git','add','main.py'],cwd=root,check=True)\n"
        "subprocess.run(['git','-c','user.email=a@b.c','-c','user.name=h',"
        "'commit','-qm','committed'],cwd=root,check=True)\n"
        "(root/'main.py').write_text('VALUE = 3\\n')\n"
        "(root/'staged.py').write_text('STAGED = 1\\n')\n"
        "subprocess.run(['git','add','staged.py'],cwd=root,check=True)\n"
        "(root/'untracked.py').write_text('UNTRACKED = 1\\n')\n"
        "print(json.dumps({'status':'ok','model':'self-reported-model',"
        "'usage':{'tokens':17,'turns':2}}))\n",
        encoding="utf-8",
    )
    result = CommandHarnessAdapter([sys.executable, str(script)]).run(
        AdapterRequest(
            repo_path=str(repo),
            goal="Improve the bot.",
            bot_file="main.py",
            budget=Budget(max_seconds=20),
        )
    )
    assert result.status == AdapterStatus.OK
    assert "VALUE = 3" in result.diff
    assert "staged.py" in result.diff
    assert "untracked.py" in result.diff
    assert result.model == "self-reported-model"
    assert result.model_source is EvidenceSource.HARNESS_REPORTED
    assert result.model_verified is False
    assert result.usage.tokens == 17
    assert result.usage.turns == 2
    assert result.usage.source is EvidenceSource.HARNESS_REPORTED
    assert result.usage.verified is False
    assert result.runtime.exit_code == 0


def test_command_adapter_does_not_inherit_secret_without_opt_in(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _seed_repo(repo)
    monkeypatch.setenv("ATV_SECRET_CANARY", "must-not-leak")
    script = tmp_path / "harness.py"
    script.write_text(
        "import os,pathlib\n"
        "pathlib.Path('observed.py').write_text("
        "'VALUE = '+repr(os.environ.get('ATV_SECRET_CANARY'))+'\\n')\n",
        encoding="utf-8",
    )
    result = CommandHarnessAdapter([sys.executable, str(script)]).run(
        AdapterRequest(repo_path=str(repo), goal="Inspect environment.")
    )
    assert result.status == AdapterStatus.OK
    assert "must-not-leak" not in (repo / "observed.py").read_text(encoding="utf-8")
    assert "ATV_SECRET_CANARY" not in result.runtime.environment_keys


def test_command_adapter_log_is_bounded_even_when_harness_floods_output(tmp_path):
    repo = tmp_path / "repo"
    _seed_repo(repo)
    result = CommandHarnessAdapter(
        [sys.executable, "-c", "print('x'*10000)"],
        log_limit=64,
        max_stdout_bytes=256,
    ).run(AdapterRequest(repo_path=str(repo), goal="Inspect."))
    assert result.status == AdapterStatus.NO_EDIT
    assert len(result.log.encode("utf-8")) <= 64
    assert result.runtime.stdout_truncated is True


def test_self_reported_evidence_cannot_be_constructed_as_verified():
    with pytest.raises(ValueError):
        Usage(
            tokens=1,
            source=EvidenceSource.HARNESS_REPORTED,
            verified=True,
        )
    with pytest.raises(ValueError):
        AdapterResult(
            status=AdapterStatus.OK,
            diff="",
            log="",
            usage=Usage(),
            model="claimed-model",
            model_source=EvidenceSource.HARNESS_REPORTED,
            model_verified=True,
        )
