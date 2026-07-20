from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from copy import deepcopy

import pytest

from atv_bench.protocol import ProtocolSession, canonical_digest

pytest_plugins = ["tests.protocol.conftest"]

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "harnesses" / "copilot-oci"
WRAPPER = EXAMPLE / "protocol_wrapper.py"
IMAGE_DIGEST = "1" * 64


def _manifest(harness: str) -> dict:
    template = EXAMPLE / f"{harness}.harness.json.template"
    return json.loads(template.read_text(encoding="utf-8").replace(
        "__IMAGE_DIGEST__", IMAGE_DIGEST
    ))


def _request(protocol_documents: dict, harness: str, output_path: str) -> dict:
    request = deepcopy(protocol_documents["request"])
    manifest = _manifest(harness)
    request["harness"] = {
        "id": manifest["id"],
        "version": manifest["version"],
        "manifest_digest": canonical_digest(manifest),
    }
    request["model_policy"]["allowed_models"] = ["gpt-5.4"]
    request["model_policy"]["gateway"] = "model-gateway.internal:443"
    request["policy"]["network"] = {
        "mode": "model-gateway-only",
        "allowed_destinations": ["model-gateway.internal:443"],
    }
    request["policy"]["credentials"] = [
        {
            "name": "ATV_MODEL_GATEWAY_HANDLE",
            "handle": "atv-credential://trial-0001/model",
        }
    ]
    request["policy"]["writable_paths"] = ["/workspace", "/artifacts"]
    request["required_capabilities"] = deepcopy(manifest["capabilities"])
    request["forbidden_capabilities"] = []
    request["output"] = {
        "mode": "named-artifacts",
        "allow_any_relative_path": False,
        "required_paths": [output_path],
        "allowed_paths": [output_path],
        "allowed_media_types": ["application/json"],
        "max_files": 4,
        "max_total_bytes": 1_048_576,
    }
    request["budget_limits"]["wall_time_ms"] = 20_000
    request["budget_limits"]["stdout_bytes"] = 1_048_576
    request["budget_limits"]["stderr_bytes"] = 1_048_576
    request["budget_limits"]["artifact_bytes"] = 1_048_576
    return request


def _fake_copilot(path: pathlib.Path) -> pathlib.Path:
    fake = path / "fake_copilot.py"
    fake.write_text(
        r'''
import json
import os
import pathlib
import subprocess
import sys
import time

args = sys.argv[1:]
mode_arg = next((item for item in args if item.startswith("fake-mode:")), None)
if mode_arg:
    args.remove(mode_arg)
workspace = pathlib.Path(args[args.index("-C") + 1])
prompt = args[args.index("--prompt") + 1]
mode = mode_arg.split(":", 1)[1] if mode_arg else "success"

def emit(event_type, data=None, **extra):
    row = {
        "type": event_type,
        "data": data or {},
        "id": extra.pop("id", event_type.replace(".", "-")),
        "timestamp": "2026-07-20T12:00:00.000Z",
        **extra,
    }
    print(json.dumps(row, sort_keys=True), flush=True)

if mode == "flood":
    sys.stdout.write("x" * 200000)
    sys.stdout.flush()
    time.sleep(30)
    raise SystemExit(9)

if mode == "cancel":
    marker = workspace / "grandchild-survived.txt"
    code = (
        "import pathlib,time;"
        "time.sleep(3);"
        f"pathlib.Path({str(marker)!r}).write_text('survived', encoding='utf-8')"
    )
    subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    (workspace / "ready.txt").write_text("ready", encoding="utf-8")
    time.sleep(30)
    raise SystemExit(8)

emit("assistant.turn_start", {"turnId": "turn-1", "model": "gpt-5.4"})
emit(
    "tool.execution_start",
    {"toolCallId": "tool-1", "toolName": "write", "arguments": {}},
)
payload = {
    "argv": args,
    "prompt": prompt,
    "env": {
        key: os.environ.get(key)
        for key in (
            "HOME",
            "COPILOT_HOME",
            "COPILOT_OFFLINE",
            "COPILOT_PROVIDER_BASE_URL",
            "COPILOT_PROVIDER_TYPE",
            "COPILOT_PROVIDER_BEARER_TOKEN",
            "COPILOT_PROVIDER_WIRE_API",
            "COPILOT_MODEL",
            "PHOENIX_WORKSPACE",
            "HVE_TELEMETRY",
            "HVE_HOME",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        )
    },
}
(workspace / "result.json").write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
emit(
    "tool.execution_complete",
    {"toolCallId": "tool-1", "success": True},
)
emit(
    "assistant.usage",
    {
        "turnId": "turn-1",
        "model": "gpt-5.4",
        "inputTokens": 11,
        "outputTokens": 7,
        "duration": 9,
        "apiCallId": "api-1",
        "providerCallId": "provider-1",
    },
)
emit(
    "result",
    {},
    sessionId="session-1",
    exitCode=0,
    usage={"premiumRequests": 1, "sessionDurationMs": 10},
)
'''.lstrip(),
        encoding="utf-8",
    )
    return fake


def _phoenix_template(path: pathlib.Path) -> pathlib.Path:
    home = path / "phoenix-home"
    (home / "agents").mkdir(parents=True)
    (home / "skills" / "phoenix").mkdir(parents=True)
    (home / "agents" / "phoenix.agent.md").write_text(
        "---\nname: phoenix\n---\n", encoding="utf-8"
    )
    (home / "skills" / "phoenix" / "SKILL.md").write_text(
        "---\nname: phoenix\n---\n", encoding="utf-8"
    )
    (home / "mcp-config.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "phoenix": {
                        "type": "stdio",
                        "command": "/opt/atv/bin/phoenix-mcp",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return home


def _hve_plugin(path: pathlib.Path) -> pathlib.Path:
    plugin = path / "hve-plugin"
    (plugin / ".github" / "plugin").mkdir(parents=True)
    (plugin / "agents" / "hve-core").mkdir(parents=True)
    (plugin / ".github" / "plugin" / "plugin.json").write_text(
        '{"name":"hve-core","version":"3.3.101"}', encoding="utf-8"
    )
    (plugin / "agents" / "hve-core" / "rpi-agent.md").write_text(
        "---\nname: rpi-agent\n---\n", encoding="utf-8"
    )
    return plugin


def _start_wrapper(
    tmp_path: pathlib.Path,
    harness: str,
    fake: pathlib.Path,
    *,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], pathlib.Path, pathlib.Path, dict, dict]:
    workspace = tmp_path / "workspace"
    artifacts = tmp_path / "artifacts"
    runtime = tmp_path / "runtime"
    workspace.mkdir()
    artifacts.mkdir()
    runtime.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "seed.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=ATV Test",
            "-c",
            "user.email=atv@example.invalid",
            "commit",
            "-qm",
            "seed",
        ],
        check=True,
    )
    phoenix_home = _phoenix_template(tmp_path)
    hve_plugin = _hve_plugin(tmp_path)
    command = [
        sys.executable,
        str(WRAPPER),
        "--harness",
        harness,
        "--copilot-executable",
        sys.executable,
        "--copilot-prefix-arg",
        str(fake),
        "--phoenix-home-template",
        str(phoenix_home),
        "--hve-plugin-dir",
        str(hve_plugin),
        "--workspace-root",
        str(workspace),
        "--artifacts-root",
        str(artifacts),
        "--runtime-root",
        str(runtime),
    ]
    fake_mode = (extra_env or {}).get("FAKE_MODE")
    if fake_mode:
        insertion = command.index("--phoenix-home-template")
        command[insertion:insertion] = [
            f"--copilot-prefix-arg=fake-mode:{fake_mode}",
        ]
    env = os.environ.copy()
    env["ATV_MODEL_GATEWAY_HANDLE"] = "opaque-test-handle"
    env.pop("COPILOT_GITHUB_TOKEN", None)
    env.pop("GH_TOKEN", None)
    env.pop("GITHUB_TOKEN", None)
    if extra_env:
        env.update(extra_env)
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    manifest = _manifest(harness)
    return process, workspace, runtime, manifest, env


def _handshake(
    process: subprocess.Popen[bytes],
    manifest: dict,
    request: dict,
) -> tuple[ProtocolSession, dict]:
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request, sort_keys=True).encode() + b"\n")
    process.stdin.flush()
    hello_raw = process.stdout.readline()
    session = ProtocolSession(manifest, request)
    hello = session.receive_harness_line(
        hello_raw, recorded_at="2026-07-20T12:00:00Z"
    )
    accepted = session.record_controller_accept(
        recorded_at="2026-07-20T12:00:01Z"
    )
    process.stdin.write(json.dumps(accepted, sort_keys=True).encode() + b"\n")
    process.stdin.flush()
    return session, hello


def _finish_transcript(
    process: subprocess.Popen[bytes],
    session: ProtocolSession,
) -> tuple[object, bytes]:
    assert process.stdout is not None
    while True:
        raw = process.stdout.readline()
        if not raw:
            break
        session.receive_harness_line(
            raw, recorded_at=f"2026-07-20T12:00:{len(session.events):02d}Z"
        )
    stderr = process.stderr.read() if process.stderr is not None else b""
    assert process.wait(timeout=10) == 0, stderr.decode(errors="replace")
    return session.finish(), stderr


@pytest.mark.parametrize("harness", ["phoenix", "hve-core"])
def test_wrapper_runs_real_entry_surface_model_free(
    tmp_path, protocol_documents, harness
):
    fake = _fake_copilot(tmp_path)
    process, workspace, runtime, manifest, _ = _start_wrapper(
        tmp_path, harness, fake
    )
    request = _request(protocol_documents, harness, "result.json")
    request["prompt"]["text"] = (
        "Implement exactly this text: $(touch shell-injection) ; "
        "& echo not-a-shell"
    )
    request["prompt"]["digest"] = {
        "algorithm": "sha256",
        "value": hashlib.sha256(request["prompt"]["text"].encode()).hexdigest(),
    }
    session, hello = _handshake(process, manifest, request)
    transcript, stderr = _finish_transcript(process, session)

    assert transcript.authority_verified is True
    assert transcript.status.value == "completed"
    assert hello["capabilities"] == manifest["capabilities"]
    event_types = [event["type"] for event in transcript.events]
    assert event_types[0:2] == ["hello", "accepted"]
    assert "model_call" in event_types
    assert "tool_call" in event_types
    assert "usage" in event_types
    assert event_types[-1] == "result"

    artifact = transcript.result["artifacts"][0]
    result_path = workspace / "result.json"
    assert artifact["path"] == "result.json"
    assert artifact["digest"]["value"] == hashlib.sha256(
        result_path.read_bytes()
    ).hexdigest()
    payload = json.loads(result_path.read_text(encoding="utf-8"))
    argv = payload["argv"]
    assert payload["prompt"] == request["prompt"]["text"]
    assert not (workspace / "shell-injection").exists()
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--stream" in argv
    assert argv[argv.index("--stream") + 1] == "off"
    assert "--disable-builtin-mcps" in argv
    assert payload["env"]["COPILOT_OFFLINE"] == "true"
    assert (
        payload["env"]["COPILOT_PROVIDER_BASE_URL"]
        == "https://model-gateway.internal:443/v1"
    )
    assert payload["env"]["COPILOT_PROVIDER_BEARER_TOKEN"] == "opaque-test-handle"
    assert payload["env"]["COPILOT_MODEL"] == "gpt-5.4"
    assert payload["env"]["COPILOT_GITHUB_TOKEN"] is None
    assert payload["env"]["GH_TOKEN"] is None
    assert payload["env"]["GITHUB_TOKEN"] is None
    assert pathlib.Path(payload["env"]["HOME"]).is_relative_to(runtime)
    assert pathlib.Path(payload["env"]["COPILOT_HOME"]).is_relative_to(runtime)
    if harness == "phoenix":
        assert argv[argv.index("--agent") + 1] == "phoenix"
        assert "--plugin-dir" not in argv
        assert payload["env"]["PHOENIX_WORKSPACE"] == "/workspace"
        assert payload["env"]["HVE_TELEMETRY"] is None
    else:
        assert argv[argv.index("--agent") + 1] == "hve-core:rpi-agent"
        assert "--plugin-dir" in argv
        assert payload["env"]["HVE_TELEMETRY"] == "0"
        assert pathlib.Path(payload["env"]["HVE_HOME"]).is_relative_to(runtime)
    assert not any(runtime.iterdir())
    assert b"opaque-test-handle" not in stderr


def test_wrapper_rejects_forged_accepted_digest_before_launch(
    tmp_path, protocol_documents
):
    fake = _fake_copilot(tmp_path)
    process, workspace, _, manifest, _ = _start_wrapper(
        tmp_path, "phoenix", fake
    )
    request = _request(protocol_documents, "phoenix", "result.json")
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(request).encode() + b"\n")
    process.stdin.flush()
    hello = process.stdout.readline()
    session = ProtocolSession(manifest, request)
    session.receive_harness_line(hello, recorded_at="2026-07-20T12:00:00Z")
    accepted = session.record_controller_accept(
        recorded_at="2026-07-20T12:00:01Z"
    )
    accepted["request_digest"]["value"] = "0" * 64
    process.stdin.write(json.dumps(accepted).encode() + b"\n")
    process.stdin.flush()
    process.stdin.close()
    stderr = process.stderr.read() if process.stderr is not None else b""
    assert process.wait(timeout=10) == 2
    assert b"request_digest" in stderr
    assert not (workspace / "result.json").exists()


def test_controller_cancel_kills_copilot_process_tree(
    tmp_path, protocol_documents
):
    fake = _fake_copilot(tmp_path)
    process, workspace, _, manifest, _ = _start_wrapper(
        tmp_path, "phoenix", fake, extra_env={"FAKE_MODE": "cancel"}
    )
    request = _request(protocol_documents, "phoenix", "result.json")
    session, _ = _handshake(process, manifest, request)
    deadline = time.monotonic() + 8
    while not (workspace / "ready.txt").exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (workspace / "ready.txt").exists()
    cancel = session.record_controller_cancel(
        recorded_at="2026-07-20T12:00:02Z",
        reason_code="operator-cancel",
        grace_period_ms=0,
    )
    assert process.stdin is not None
    process.stdin.write(json.dumps(cancel).encode() + b"\n")
    process.stdin.flush()
    transcript, _ = _finish_transcript(process, session)
    assert transcript.status.value == "cancelled"
    assert transcript.result["exit"]["cancelled"] is True
    time.sleep(3.5)
    assert not (workspace / "grandchild-survived.txt").exists()


def test_child_stdout_limit_fails_closed(tmp_path, protocol_documents):
    fake = _fake_copilot(tmp_path)
    process, _, _, manifest, _ = _start_wrapper(
        tmp_path, "hve-core", fake, extra_env={"FAKE_MODE": "flood"}
    )
    request = _request(protocol_documents, "hve-core", "result.json")
    request["budget_limits"]["stdout_bytes"] = 1024
    session, _ = _handshake(process, manifest, request)
    transcript, _ = _finish_transcript(process, session)
    assert transcript.status.value == "harness_crash"
    assert transcript.result["failure"]["scope"] == "protocol"
    assert transcript.result["failure"]["code"] in {
        "stdout-limit-exceeded",
        "stdout-line-limit",
    }


def test_templates_and_dockerfile_pin_a_common_image():
    phoenix = _manifest("phoenix")
    hve = _manifest("hve-core")
    assert phoenix["runtime"]["image"] == hve["runtime"]["image"]
    assert phoenix["runtime"]["entrypoint"][-1] == "phoenix"
    assert hve["runtime"]["entrypoint"][-1] == "hve-core"
    assert phoenix["security"]["env_allowlist"] == ["ATV_MODEL_GATEWAY_HANDLE"]
    assert hve["security"]["env_allowlist"] == ["ATV_MODEL_GATEWAY_HANDLE"]

    dockerfile = (EXAMPLE / "Dockerfile").read_text(encoding="utf-8")
    for expected in (
        "233e8e1e968bbc0b1dc446d7830efa82489bf118",
        "5c15a03c78da2408527693e0fc3b3e387bf99cb2",
        "copilot-0.0.394.tgz",
        "copilot-linux-x64-0.0.394.tgz",
        "0362769b6fb2aeb7f908a5d9ee1b9e2a5fee6ce6ea7f80c5533d9950252211bd",
        "7d2c7cd9da1d0442f27112807621890b8eefa7b9bbf223cfec6a27d495ee4393",
        "USER 65534:65534",
    ):
        assert expected in dockerfile
    assert "@sha256:" in dockerfile
    assert "cp -aL" in dockerfile
    wrapper = WRAPPER.read_text(encoding="utf-8")
    assert "shell=False" in wrapper
    assert "shell=True" not in wrapper
