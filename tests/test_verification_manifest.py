"""Adversarial tests for the local verification evidence-manifest lane."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath

import pytest

import atv_bench.verification_manifest as verification
from atv_bench.launch_audit import GateStatus, audit_launch
from atv_bench.verification_manifest import (
    BoundedSubprocessExecutor,
    LocalVerificationRunner,
    RawExecution,
    VerificationError,
    build_verification_plan,
    canonical_json_bytes,
    gate_rules,
    validate_evidence_manifest,
)


FIXED_NOW = datetime(2026, 7, 19, 20, 0, 0, tzinfo=timezone.utc)
TOOL_VERSIONS = {
    "python": {
        "argv": ["$PYTHON", "--version"],
        "exit_code": 0,
        "version": "Python 3.13.5",
        "output_sha256": "1" * 64,
    },
    "pytest": {
        "argv": ["$PYTHON", "-m", "pytest", "--version"],
        "exit_code": 0,
        "version": "pytest 8.4.1",
        "output_sha256": "2" * 64,
    },
    "uv": {
        "argv": ["uv", "--version"],
        "exit_code": 0,
        "version": "uv 0.8.0",
        "output_sha256": "3" * 64,
    },
    "git": {
        "argv": ["git", "--version"],
        "exit_code": 0,
        "version": "git version 2.50.0",
        "output_sha256": "4" * 64,
    },
    "docker": {
        "argv": ["docker", "version", "--format", "{{json .}}"],
        "exit_code": 0,
        "version": '{"Client":{"Version":"28"},"Server":{"Version":"28"}}',
        "output_sha256": "5" * 64,
    },
}


def _run_git(repo: Path, *argv: str) -> str:
    return subprocess.run(
        ["git", *argv],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        check=True,
    ).stdout.strip()


def _function_name(node_id: str) -> str:
    return node_id.split("::", 1)[1].split("[", 1)[0]


def _required_tests(mode: str) -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {
        spec.id: set() for spec in build_verification_plan(mode) if spec.junit
    }
    for rule in gate_rules():
        if mode not in rule.modes:
            continue
        for requirement in rule.requirements:
            rows.setdefault(requirement.command_id, set()).update(
                requirement.test_ids
            )
    return rows


def _source_file_for_target(target: str) -> str:
    source = target.split("::", 1)[0].replace("\\", "/")
    if source.endswith(".py"):
        return source
    return source.rstrip("/") + "/test_placeholder.py"


def _make_repo(tmp_path: Path, *, slug: str = "example/atv-evidence") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    required_by_command = _required_tests("quick")
    functions_by_file: dict[str, set[str]] = {}
    for tests in required_by_command.values():
        for node_id in tests:
            file_name = node_id.split("::", 1)[0]
            functions_by_file.setdefault(file_name, set()).add(
                _function_name(node_id)
            )
    for spec in build_verification_plan("quick"):
        for target in spec.pytest_targets:
            file_name = _source_file_for_target(target)
            functions_by_file.setdefault(file_name, set()).add("test_placeholder")
    for file_name, functions in sorted(functions_by_file.items()):
        path = repo / Path(*PurePosixPath(file_name).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n\n".join(
                f"def {name}():\n    pass" for name in sorted(functions)
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
    (repo / "README.md").write_text("fixture repository\n", encoding="utf-8")
    _run_git(repo, "init", "-q")
    _run_git(repo, "config", "core.autocrlf", "false")
    _run_git(repo, "config", "user.email", "fixture@example.invalid")
    _run_git(repo, "config", "user.name", "Fixture")
    _run_git(repo, "add", "-A")
    _run_git(repo, "commit", "-qm", "fixture")
    _run_git(repo, "remote", "add", "origin", f"https://github.com/{slug}.git")
    return repo


def _classname(node_id: str) -> str:
    file_name = node_id.split("::", 1)[0]
    return file_name.removesuffix(".py").replace("/", ".")


def _write_junit(
    path: Path,
    node_ids: list[str],
    *,
    timestamp: datetime = FIXED_NOW,
    skipped: set[str] | None = None,
    failed: set[str] | None = None,
) -> None:
    skipped = skipped or set()
    failed = failed or set()
    suites = ET.Element("testsuites", {"name": "pytest tests"})
    suite = ET.SubElement(
        suites,
        "testsuite",
        {
            "name": "pytest",
            "timestamp": timestamp.isoformat(),
            "tests": str(len(node_ids)),
            "failures": str(sum(node in failed for node in node_ids)),
            "errors": "0",
            "skipped": str(sum(node in skipped for node in node_ids)),
            "time": "0.100",
        },
    )
    for node_id in node_ids:
        name = node_id.split("::", 1)[1]
        case = ET.SubElement(
            suite,
            "testcase",
            {
                "classname": _classname(node_id),
                "name": name,
                "time": "0.001",
            },
        )
        if node_id in skipped:
            ET.SubElement(case, "skipped", {"message": "fixture skip"})
        if node_id in failed:
            ET.SubElement(case, "failure", {"message": "fixture failure"})
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)
    epoch = timestamp.timestamp()
    os.utime(path, (epoch, epoch))


class FakeExecutor:
    def __init__(
        self,
        mode: str = "quick",
        *,
        omit: tuple[str, str] | None = None,
        skip: tuple[str, str] | None = None,
    ) -> None:
        self.mode = mode
        self.calls: list[tuple[str, ...]] = []
        self.required = _required_tests(mode)
        self.omit = omit
        self.skip = skip

    def execute(
        self,
        argv,
        *,
        cwd,
        timeout_seconds,
        max_output_bytes,
        env=None,
    ) -> RawExecution:
        tokens = tuple(argv)
        self.calls.append(tokens)
        if "--junitxml" in tokens:
            junit_path = Path(tokens[tokens.index("--junitxml") + 1])
            command_id = junit_path.parent.name
            node_ids = sorted(self.required.get(command_id, set()))
            if not node_ids:
                spec = next(
                    item
                    for item in build_verification_plan(self.mode)
                    if item.id == command_id
                )
                target = spec.pytest_targets[0]
                if "::" in target:
                    node_ids = [target]
                else:
                    node_ids = [
                        f"{_source_file_for_target(target)}::test_placeholder"
                    ]
            if self.omit and self.omit[0] == command_id:
                node_ids = [
                    node for node in node_ids if node != self.omit[1]
                ]
            skipped = (
                {self.skip[1]}
                if self.skip and self.skip[0] == command_id
                else set()
            )
            _write_junit(junit_path, node_ids, skipped=skipped)
        return RawExecution(
            argv=tokens,
            started_at=verification._iso(FIXED_NOW),
            finished_at=verification._iso(FIXED_NOW),
            duration_ms=100,
            exit_code=0,
            timed_out=False,
            stdout=b"fixture-ok\n",
            stderr=b"",
            stdout_total_bytes=11,
            stderr_total_bytes=0,
            stdout_truncated=False,
            stderr_truncated=False,
            error=None,
        )


def _run_quick(
    repo: Path,
    *,
    executor: FakeExecutor | None = None,
    resume: bool = False,
    tool_versions: dict | None = None,
    governance_json: Path | None = None,
    now: datetime = FIXED_NOW,
):
    fake = executor or FakeExecutor()
    outcome = LocalVerificationRunner(
        repo,
        output_root=repo / "reports/local-verification",
        mode="quick",
        executor=fake,
        resume=resume,
        now=lambda: now,
        tool_versions=tool_versions or TOOL_VERSIONS,
        governance_json=governance_json,
    ).run()
    manifest = json.loads(outcome.manifest_path.read_text(encoding="utf-8"))
    return outcome, manifest, fake


def _gate(report, gate_id: str):
    return next(item for item in report.gates if item.id == gate_id)


def test_fixed_plan_is_complete_shell_free_and_mode_scoped(tmp_path):
    quick = build_verification_plan("quick")
    full = build_verification_plan("full")
    quick_ids = {spec.id for spec in quick}
    full_ids = {spec.id for spec in full}

    assert quick_ids == {
        "protocol_focused",
        "eval_focused",
        "security_focused",
        "oci_protocol_focused",
        "signing_focused",
        "adapter_focused",
        "iterative_focused",
        "task_focused",
        "cli_focused",
        "governance_focused",
        "launch_focused",
        "embedded_schema",
        "cp1252_cli",
        "actions_sha",
    }
    assert quick_ids < full_ids
    assert {
        "full_non_live",
        "docker_preflight",
        "docker_image",
        "docker_oci_integration",
        "docker_control_plane_integration",
        "docker_cli_integration",
        "uv_sync_locked",
        "codeclash_assets",
        "package_build",
        "wheel_venv_create",
        "wheel_install",
        "wheel_verify",
        "sdist_venv_create",
        "sdist_install",
        "sdist_verify",
    } <= full_ids
    assert all(isinstance(spec.argv, tuple) for spec in full)
    assert not any(
        token.casefold() in {"cmd", "cmd.exe", "powershell", "pwsh", "sh", "bash"}
        for spec in full
        for token in spec.argv[:1]
    )
    assert all("tests/test_e2e_live.py" not in spec.argv for spec in full)
    assert next(spec for spec in full if spec.id == "wheel_verify").clear_pythonpath
    assert next(spec for spec in full if spec.id == "wheel_verify").cwd == (
        "{ISOLATED_CWD}"
    )

    observed = {}

    def reject_popen(*args, **kwargs):
        observed.update(kwargs)
        raise OSError("fixture stop")

    result = BoundedSubprocessExecutor(
        popen_factory=reject_popen,
        now=lambda: FIXED_NOW,
        monotonic=lambda: 1.0,
    ).execute(
        [sys.executable, "-c", "print('literal')", "; touch injected"],
        cwd=tmp_path,
        timeout_seconds=1,
        max_output_bytes=1024,
    )
    assert result.exit_code is None
    assert observed["shell"] is False
    assert observed["stdin"] is subprocess.DEVNULL


def test_successful_verification_command_cleans_lingering_descendants(tmp_path):
    heartbeat = tmp_path / "child-heartbeat.txt"
    child_path = tmp_path / "child.py"
    child_path.write_text(
        "import os,pathlib,time\n"
        f"path=pathlib.Path({str(heartbeat)!r})\n"
        "path.write_text(str(os.getpid()), encoding='ascii')\n"
        "while True:\n"
        " path.write_text(path.read_text(encoding='ascii') + '.', encoding='ascii')\n"
        " time.sleep(0.02)\n",
        encoding="utf-8",
    )
    parent_path = tmp_path / "parent.py"
    parent_path.write_text(
        "import pathlib,subprocess,sys,time\n"
        f"path=pathlib.Path({str(heartbeat)!r})\n"
        f"subprocess.Popen([sys.executable, {str(child_path)!r}])\n"
        "deadline=time.monotonic()+5\n"
        "while not path.exists() and time.monotonic()<deadline:\n"
        " time.sleep(0.01)\n"
        "raise SystemExit(0 if path.exists() else 2)\n",
        encoding="utf-8",
    )

    result = BoundedSubprocessExecutor().execute(
        [sys.executable, str(parent_path)],
        cwd=tmp_path,
        timeout_seconds=10,
        max_output_bytes=1024,
    )

    assert result.exit_code == 0
    assert result.error is None
    before = heartbeat.read_text(encoding="ascii")
    time.sleep(0.25)
    assert heartbeat.read_text(encoding="ascii") == before


def test_repository_git_commands_are_timed_bounded_and_environment_isolated(
    monkeypatch,
    tmp_path,
):
    observed = {}

    class FakeExecutor:
        def __init__(self, *, maximum_capture_bytes, **_kwargs):
            observed["maximum_capture_bytes"] = maximum_capture_bytes

        def execute(self, argv, **kwargs):
            observed["argv"] = argv
            observed.update(kwargs)
            return RawExecution(
                argv=tuple(argv),
                started_at="2026-07-20T00:00:00Z",
                finished_at="2026-07-20T00:01:00Z",
                duration_ms=60_000,
                exit_code=None,
                timed_out=True,
                stdout=b"",
                stderr=b"",
                stdout_total_bytes=0,
                stderr_total_bytes=0,
                stdout_truncated=False,
                stderr_truncated=False,
                error="command exceeded 60 seconds",
            )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-git")
    monkeypatch.setattr(verification, "BoundedSubprocessExecutor", FakeExecutor)

    with pytest.raises(VerificationError, match="could not complete safely"):
        verification._git(tmp_path, "rev-parse", "HEAD", max_bytes=4096)

    assert observed["timeout_seconds"] == verification.GIT_TIMEOUT_SECONDS
    assert observed["maximum_capture_bytes"] == 4096
    assert observed["max_output_bytes"] == 4096
    assert observed["argv"][:5] == [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
    ]
    assert observed["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
    assert observed["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "OPENAI_API_KEY" not in observed["env"]


def test_verification_environment_is_allowlisted_without_secret_value_hashes():
    source = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": "/home/example",
        "OPENAI_API_KEY": "sk-secret-value",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
        "USERNAME": "private-user",
        "PYTHONPATH": "/untrusted/import/path",
    }
    environment, policy = verification._environment_bundle(source, spec=None)

    assert environment["HOME"] == "/home/example"
    assert "OPENAI_API_KEY" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "USERNAME" not in environment
    assert "PYTHONPATH" not in environment
    assert policy["inheritance_mode"] == "allowlist"
    assert "OPENAI_API_KEY" not in policy["inherited_names"]
    assert "OPENAI_API_KEY" not in policy["forced_value_sha256"]
    assert set(policy["forced_value_sha256"]) == set(policy["forced"])
    verification._validate_environment_policy(policy, spec=None)
    redacted = verification._redact_host_path(Path.home() / "bin" / "tool")
    assert redacted == "$HOME/bin/tool"
    executable = verification._hash_tool_executable(Path(sys.executable))
    assert executable["path"] == verification._redact_host_path(Path(sys.executable))
    assert "private-user" not in executable["path"]


def test_junit_hostname_is_removed_before_public_evidence(tmp_path):
    path = tmp_path / "junit.xml"
    node_id = (
        "tests/test_eval_stats.py::"
        "test_tasks_not_trials_are_the_bootstrap_and_macro_average_unit"
    )
    _write_junit(path, [node_id])
    tree = ET.parse(path)
    suite = next(tree.getroot().iter("testsuite"))
    suite.set("hostname", "PRIVATE-WORKSTATION")
    system_out = ET.SubElement(suite, "system-out")
    escaped_home = str(Path.home()).replace("\\", "\\\\")
    system_out.text = f"home={Path.home()} escaped={escaped_home} repo={tmp_path}"
    tree.write(path, encoding="utf-8", xml_declaration=True)

    removed = verification._sanitize_junit_metadata(
        path,
        repo_root=tmp_path,
        output_root=tmp_path / "reports",
    )
    sanitized = path.read_text(encoding="utf-8")

    assert removed == ("testsuite.hostname", "xml.host-identifiers")
    assert "PRIVATE-WORKSTATION" not in sanitized
    assert str(Path.home()) not in sanitized
    assert escaped_home not in sanitized
    assert str(tmp_path) not in sanitized
    assert "$HOME" in sanitized
    assert "$REPO" in sanitized
    assert "hostname=" not in sanitized


def test_public_stream_sanitizer_removes_paths_hosts_and_secret_values(
    tmp_path,
    monkeypatch,
):
    secret = "ATV_TEST_SECRET_VALUE_123456"
    monkeypatch.setenv("ATV_TEST_SECRET_TOKEN", secret)
    output_root = tmp_path / "reports" / "local-verification"
    raw_text = (
        f"repo={tmp_path} home={Path.home()} "
        f"user={os.environ.get('USERNAME', '')} secret={secret}"
    ).encode("utf-8")
    raw = RawExecution(
        argv=("fixture",),
        started_at="2026-07-20T00:00:00Z",
        finished_at="2026-07-20T00:00:01Z",
        duration_ms=1_000,
        exit_code=0,
        timed_out=False,
        stdout=raw_text,
        stderr=raw_text,
        stdout_total_bytes=len(raw_text),
        stderr_total_bytes=len(raw_text),
        stdout_truncated=False,
        stderr_truncated=False,
    )

    sanitized = verification._sanitize_raw_execution(
        raw,
        repo_root=tmp_path,
        output_root=output_root,
    )
    combined = sanitized.stdout + sanitized.stderr

    assert str(tmp_path).encode() not in combined
    assert str(Path.home()).encode() not in combined
    assert secret.encode() not in combined
    assert b"$REPO" in combined
    assert b"$HOME" in combined
    assert b"$SECRET" in combined
    assert "$SECRET" in sanitized.stdout_redactions
    assert sanitized.stdout_total_bytes == len(sanitized.stdout)
    assert sanitized.stdout_sha256 == verification.sha256_bytes(sanitized.stdout)


def test_verification_run_lock_rejects_concurrent_owner(tmp_path):
    output_root = tmp_path / "reports" / "local-verification" / "locked"
    with verification._verification_run_lock(output_root):
        with pytest.raises(VerificationError, match="another local verification"):
            with verification._verification_run_lock(output_root):
                pass
    assert (output_root / ".run.lock").is_file()


def test_windows_safe_pytest_temp_path_is_short_external_and_canonical(tmp_path):
    repo = _make_repo(tmp_path)
    output_root = (
        repo
        / "reports"
        / "local-verification"
        / ("very-long-evidence-directory-" * 4)
    )
    repository = verification.repository_snapshot(
        repo,
        excluded_paths=("reports/local-verification",),
    )
    spec = next(
        item
        for item in build_verification_plan("full")
        if item.id == "full_non_live"
    )
    resolved = verification._resolve_tokens(
        spec,
        repo_root=repo,
        output_root=output_root,
        repository=repository,
        python_executable=sys.executable,
    )
    basetemp = Path(resolved[resolved.index("--basetemp") + 1])
    normalized = verification._normalize_argv(
        resolved,
        repo_root=repo,
        output_root=output_root,
        python_executable=sys.executable,
    )

    assert basetemp.is_absolute()
    assert repo.resolve() not in basetemp.parents
    assert len(os.fspath(basetemp)) < 180
    assert normalized[normalized.index("--basetemp") + 1].startswith(
        "$VERIFY_TMP/"
    )
    for command_id in ("docker_preflight", "docker_image"):
        docker_spec = next(
            item
            for item in build_verification_plan("full")
            if item.id == command_id
        )
        docker_argv = verification._resolve_tokens(
            docker_spec,
            repo_root=repo,
            output_root=output_root,
            repository=repository,
            python_executable=sys.executable,
        )
        assert "{{json .}}" in docker_argv
    verification._prepare_command_paths(
        spec,
        output_root=output_root,
        repository=repository,
        resume=False,
    )
    assert basetemp.parent.is_dir()
    readonly = basetemp.parent / "read-only-git-object"
    readonly.write_bytes(b"object")
    readonly.chmod(0o444)
    verification._safe_remove_verification_temp(
        verification._verification_temp_root(output_root, repository)
    )
    assert not readonly.exists()
    outside_temp = Path(__file__).resolve().parent.parent
    with pytest.raises(VerificationError, match="outside verification temp"):
        verification._safe_remove_verification_temp(outside_temp)


def test_content_addressed_quick_run_has_bounded_bound_evidence(tmp_path):
    repo = _make_repo(tmp_path)
    outcome, manifest, fake = _run_quick(repo)

    assert outcome.plan_succeeded is True
    assert outcome.launch_ready is False
    assert outcome.manifest_path.stem == outcome.manifest_sha256
    assert outcome.proof_path.stem == outcome.proof_sha256
    assert hashlib.sha256(outcome.manifest_path.read_bytes()).hexdigest() == (
        outcome.manifest_sha256
    )
    assert manifest["canonical_digest"]["value"] == (
        outcome.manifest_canonical_sha256
    )
    assert manifest["schema"] == "atv.launch-evidence-manifest/v1"
    assert manifest["repository"]["schema"] == verification.REPOSITORY_SCHEMA
    assert manifest["repository"]["dirty"] is False
    assert set(manifest["repository"]["excluded_paths"]) == {
        "docs/CREDIBILITY_STATUS.md",
        "reports/local-verification",
    }
    assert manifest["environment_policy"]["schema"] == verification.ENVIRONMENT_SCHEMA
    assert manifest["tool_versions"]["schema"] == verification.TOOLCHAIN_SCHEMA
    assert {
        item["path"] for item in manifest["tool_versions"]["dependency_files"]
    } == {"pyproject.toml", "uv.lock"}
    assert manifest["summary"]["local_verification_only"] is True
    assert manifest["summary"]["official_run_claimed"] is False
    assert len(fake.calls) == len(build_verification_plan("quick"))
    validated = validate_evidence_manifest(
        repo, outcome.manifest_path, now=FIXED_NOW
    )
    assert validated == manifest

    generated_status = repo / "docs" / "CREDIBILITY_STATUS.md"
    generated_status.parent.mkdir(parents=True, exist_ok=True)
    generated_status.write_text("generated after verification\n", encoding="utf-8")
    assert validate_evidence_manifest(
        repo,
        outcome.manifest_path,
        now=FIXED_NOW,
    ) == manifest

    proof = json.loads(outcome.proof_path.read_text(encoding="utf-8"))
    assert proof["schema"] == "atv.launch-proof/v1"
    assert proof["canonical_digest"]["value"] == outcome.proof_canonical_sha256
    for assessment in proof["gates"].values():
        assert {"Problem", "Cause", "Fix", "Evidence"} <= assessment.keys()
        assert assessment["mapped_evidence"]
    for reference in manifest["commands"].values():
        command = json.loads((repo / reference["artifact"]).read_text())
        assert {"Problem", "Cause", "Fix", "Evidence"} <= command[
            "diagnostic"
        ].keys()
        assert command["stdout"]["sha256"]
        assert command["stdout"]["stream_sha256"]
        assert command["stdout"]["capture_sha256"] == command["stdout"]["sha256"]
        assert command["stderr"]["sha256"]
        assert command["stderr"]["stream_sha256"]
        assert command["environment"]["schema"] == verification.ENVIRONMENT_SCHEMA
        assert command["invocation"]["schema"] == verification.INVOCATION_SCHEMA
        assert command["termination"]["schema"] == verification.TERMINATION_SCHEMA
        assert command["canonical_digest"]["value"] == reference["canonical_sha256"]
        if command["junit"]:
            assert command["junit"]["sha256"]
            assert command["junit"]["tests"] > 0


def test_manifest_rejects_unallowlisted_argv_and_boolean_proofs(tmp_path):
    repo = _make_repo(tmp_path)
    outcome, manifest, _ = _run_quick(repo)

    argv_forgery = copy.deepcopy(manifest)
    argv_forgery["plan"]["commands"][0]["argv"].append("; touch owned")
    with pytest.raises(VerificationError, match="fixed allowlisted argv plan"):
        validate_evidence_manifest(repo, argv_forgery, now=FIXED_NOW)

    boolean_forgery = copy.deepcopy(manifest)
    gate_id = next(iter(boolean_forgery["proofs"]))
    boolean_forgery["proofs"][gate_id] = True
    with pytest.raises(VerificationError, match="boolean|object|artifact"):
        validate_evidence_manifest(repo, boolean_forgery, now=FIXED_NOW)

    assert outcome.manifest_path.is_file()


def test_forged_or_tampered_junit_and_nested_digest_are_rejected(tmp_path):
    repo = _make_repo(tmp_path)
    outcome, manifest, _ = _run_quick(repo)
    command_ref = manifest["commands"]["eval_focused"]
    command_path = repo / command_ref["artifact"]
    command = json.loads(command_path.read_text(encoding="utf-8"))
    junit_path = repo / command["junit"]["artifact"]
    tree = ET.parse(junit_path)
    suite = next(tree.getroot().iter("testsuite"))
    first = next(suite.iter("testcase"))
    suite.remove(first)
    tree.write(junit_path, encoding="utf-8", xml_declaration=True)
    os.utime(junit_path, (FIXED_NOW.timestamp(), FIXED_NOW.timestamp()))

    with pytest.raises(VerificationError, match="digest mismatch"):
        validate_evidence_manifest(repo, outcome.manifest_path, now=FIXED_NOW)

    # Restore by rerunning, then tamper with the proof object itself.
    outcome, manifest, _ = _run_quick(repo)
    proof_path = repo / manifest["proof"]["artifact"]
    proof_path.write_bytes(proof_path.read_bytes() + b" ")
    with pytest.raises(VerificationError, match="digest mismatch"):
        validate_evidence_manifest(repo, outcome.manifest_path, now=FIXED_NOW)


def test_stale_and_cross_repository_evidence_are_rejected(tmp_path):
    repo = _make_repo(tmp_path / "first")
    outcome, manifest, _ = _run_quick(repo)

    stale = copy.deepcopy(manifest)
    stale["generated_at"] = verification._iso(
        FIXED_NOW - timedelta(days=31)
    )
    with pytest.raises(VerificationError, match="stale"):
        validate_evidence_manifest(repo, stale, now=FIXED_NOW)

    copied = tmp_path / "copied"
    shutil.copytree(repo, copied)
    copied_manifest = copied / outcome.manifest_path.relative_to(repo)
    with pytest.raises(VerificationError, match="another repository|workspace_id"):
        validate_evidence_manifest(copied, copied_manifest, now=FIXED_NOW)


def test_gate_pass_is_recomputed_from_exact_required_junit_nodes(tmp_path):
    repo = _make_repo(tmp_path)
    missing = (
        "tests/test_eval_stats.py::"
        "test_tasks_not_trials_are_the_bootstrap_and_macro_average_unit"
    )
    outcome, manifest, _ = _run_quick(
        repo, executor=FakeExecutor(omit=("eval_focused", missing))
    )
    proof = json.loads(outcome.proof_path.read_text(encoding="utf-8"))
    assert proof["gates"]["launch.independent_trial"]["result"] == "failed"
    report = audit_launch(
        repo,
        audit_date="2026-07-19",
        evidence_manifest=manifest,
    )
    assert _gate(report, "launch.independent_trial").status is GateStatus.FAILED
    assert "exact required test" in proof["gates"][
        "launch.independent_trial"
    ]["Cause"]


def test_safe_resume_reuses_only_fresh_same_context_untampered_evidence(tmp_path):
    repo = _make_repo(tmp_path)
    first_executor = FakeExecutor()
    first, manifest, _ = _run_quick(repo, executor=first_executor)
    assert len(first_executor.calls) == len(build_verification_plan("quick"))

    resumed_executor = FakeExecutor()
    second, _, _ = _run_quick(
        repo, executor=resumed_executor, resume=True
    )
    assert resumed_executor.calls == []
    assert second.manifest_sha256 != "" and first.manifest_sha256 != ""

    command = json.loads(
        (repo / manifest["commands"]["protocol_focused"]["artifact"]).read_text()
    )
    stdout_path = repo / command["stdout"]["artifact"]
    stdout_path.write_bytes(b"tampered")
    tamper_executor = FakeExecutor()
    _run_quick(repo, executor=tamper_executor, resume=True)
    assert len(tamper_executor.calls) == 1

    changed_tools = copy.deepcopy(TOOL_VERSIONS)
    changed_tools["python"]["version"] = "Python 9.9"
    drift_executor = FakeExecutor()
    _run_quick(
        repo,
        executor=drift_executor,
        resume=True,
        tool_versions=changed_tools,
    )
    assert len(drift_executor.calls) == len(build_verification_plan("quick"))


def test_deterministic_normalization_produces_identical_content_addresses(tmp_path):
    repo = _make_repo(tmp_path)
    first, _, _ = _run_quick(repo, executor=FakeExecutor())
    second, _, _ = _run_quick(
        repo,
        executor=FakeExecutor(),
        now=FIXED_NOW + timedelta(hours=1),
    )

    assert first.proof_sha256 != second.proof_sha256
    assert first.manifest_sha256 != second.manifest_sha256
    assert first.proof_canonical_sha256 == second.proof_canonical_sha256
    assert first.manifest_canonical_sha256 == second.manifest_canonical_sha256
    assert first.audit_sha256 != second.audit_sha256
    assert first.proof_path.read_bytes() != second.proof_path.read_bytes()
    assert first.manifest_path.read_bytes() != second.manifest_path.read_bytes()


def test_optional_governance_json_is_read_only_repo_bound_and_hashed(tmp_path):
    repo = _make_repo(tmp_path)
    injected = tmp_path / "governance;touch-NOT-EXECUTED.json"
    document = {
        "schema_version": 1,
        "source": "github-rest-via-gh",
        "repository": "example/atv-evidence",
        "default_branch": "main",
        "generated_at": verification._iso(FIXED_NOW),
        "passed": False,
        "failure_count": 1,
        "failures": ["default_branch.protected"],
        "findings": [],
    }
    injected.write_bytes(canonical_json_bytes(document))
    marker = tmp_path / "NOT-EXECUTED"

    outcome, manifest, _ = _run_quick(repo, governance_json=injected)
    governance = manifest["governance_evidence"]
    copied = repo / governance["artifact"]
    assert copied.read_bytes() == injected.read_bytes()
    assert hashlib.sha256(copied.read_bytes()).hexdigest() == governance["sha256"]
    assert not marker.exists()
    validate_evidence_manifest(repo, outcome.manifest_path, now=FIXED_NOW)

    wrong = tmp_path / "wrong.json"
    wrong_document = dict(document, repository="other/repository")
    wrong.write_bytes(canonical_json_bytes(wrong_document))
    with pytest.raises(VerificationError, match="different repository"):
        _run_quick(repo, governance_json=wrong)


def test_realistic_partial_evidence_reduces_only_justified_audit_blockers(
    tmp_path,
):
    repo = _make_repo(tmp_path)
    before = audit_launch(repo, audit_date="2026-07-19")
    outcome, manifest, _ = _run_quick(repo)
    after = audit_launch(
        repo,
        audit_date="2026-07-19",
        evidence_manifest=manifest,
    )

    justified = {
        "launch.independent_trial",
        "release.experiment.trial_unit",
        "launch.versioned_protocol_task",
        "release.protocol.schemas",
        "launch.paired_schedule",
        "release.experiment.paired",
        "launch.winner_rule",
        "release.analysis.winner",
        "launch.secret_isolation",
        "release.security.no_credentials",
        "release.protocol.unknown_versions",
        "release.tasks.deterministic",
        "release.experiment.model_budget",
        "release.experiment.infrastructure",
        "release.repository.actions_pinned",
        "release.tasks.gates",
    }
    assert all(_gate(after, gate_id).status is GateStatus.ACHIEVED for gate_id in justified)
    assert after.blocker_count == before.blocker_count - len(justified)
    assert _gate(after, "launch.contamination_retraction").status is GateStatus.BLOCKED
    assert outcome.launch_ready is False

    excluded = {
        "launch.process_oci_conformance",
        "launch.ephemeral_runner",
        "launch.model_attestation",
        "launch.signed_bundle",
        "launch.task_portfolio",
        "launch.five_trials",
        "launch.clustered_uncertainty",
        "launch.external_reproduction",
        "launch.live_governance",
        "launch.immutable_release",
        "release.repository.signed_tag",
        "release.protocol.process_oci",
        "release.security.signatures",
        "release.security.independent_review",
        "release.tasks.human_review",
        "release.tasks.split",
        "release.analysis.independent_review",
        "release.publication.external_reproduction",
    }
    assert all(
        _gate(after, gate_id).status is not GateStatus.ACHIEVED
        for gate_id in excluded
    )


def test_full_plan_rules_require_zero_skip_real_docker_and_package_artifacts():
    rules = {rule.gate_id: rule for rule in gate_rules()}
    for gate_id in (
        "launch.hidden_grader",
        "launch.process_oci_conformance",
        "launch.ephemeral_runner",
        "release.security.hidden_tests",
        "release.protocol.process_oci",
        "release.protocol.cancellation",
        "release.security.gateway_egress",
        "release.security.bombs",
        "release.publication.reproduction_command",
    ):
        rule = rules[gate_id]
        assert rule.modes == frozenset({"full"})
        integration = [
            requirement
            for requirement in rule.requirements
            if requirement.command_id.startswith("docker_")
            and requirement.test_ids
        ]
        assert integration
        assert all(requirement.require_zero_skips for requirement in integration)
    packaging = rules["launch.packaging_cp1252"]
    command_ids = {item.command_id for item in packaging.requirements}
    assert {
        "uv_sync_locked",
        "codeclash_assets",
        "package_build",
        "wheel_install",
        "wheel_verify",
        "sdist_install",
        "sdist_verify",
        "cp1252_cli",
    } <= command_ids
    build = next(
        item
        for item in packaging.requirements
        if item.command_id == "package_build"
    )
    assert set(build.artifact_labels) == {"wheel", "sdist"}
