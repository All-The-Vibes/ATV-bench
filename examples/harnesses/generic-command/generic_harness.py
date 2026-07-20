"""Generic protocol-v1 harness integrated only through harness.json."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

CAPABILITIES = {
    "workspace_edit": True,
    "subagents": False,
    "resumable": False,
    "browser": False,
    "model_events": True,
    "tool_events": False,
    "usage_events": True,
    "checkpoint_events": False,
    "model_selection": "multiple",
    "token_usage_reporting": "reported",
    "call_usage_reporting": "reported",
    "cost_usage_reporting": "reported",
}


def _timestamp_factory():
    start = datetime.now(timezone.utc)
    tick = 0

    def timestamp() -> str:
        nonlocal tick
        value = start + timedelta(microseconds=tick)
        tick += 1
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    return timestamp


def _canonical_digest(value: dict) -> dict[str, str]:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {"algorithm": "sha256", "value": hashlib.sha256(encoded).hexdigest()}


def _mode(goal: str) -> str:
    marker = "mode="
    if marker not in goal:
        return "single_file"
    return goal.split(marker, 1)[1].split()[0].strip()


def main() -> int:
    trial = json.loads(sys.stdin.readline())
    repo = pathlib.Path.cwd()
    output = trial["output"]
    bot_name = output["required_paths"][0] if output["required_paths"] else "main.py"
    bot = repo / bot_name
    mode = _mode(str(trial["prompt"]["text"]))
    timestamp = _timestamp_factory()
    harness_sequence = 0

    def emit(event_type: str, **payload) -> None:
        nonlocal harness_sequence
        event = {
            "schema": "atv.harness-event/v1",
            "type": event_type,
            "protocol_version": 1,
            "trial_id": trial["trial_id"],
            "attempt_id": trial["attempt_id"],
            "harness_sequence": harness_sequence,
            "emitted_at": timestamp(),
            **payload,
        }
        harness_sequence += 1
        print(json.dumps(event, sort_keys=True), flush=True)

    emit(
        "hello",
        supported_protocol_versions=[1],
        capabilities=CAPABILITIES,
        harness=trial["harness"],
    )
    accepted = json.loads(sys.stdin.readline())
    if (
        accepted.get("type") != "accepted"
        or accepted.get("source") != "controller"
        or accepted.get("request_digest") != _canonical_digest(trial)
    ):
        return 3

    emit("status", status="running", detail_code="editing")
    artifacts: list[dict] = []
    status = "completed"
    failure = None
    exit_code = 0
    model_specs: list[tuple[str, int]] = [("example/model-snapshot", 0)]
    usage_known = mode != "missing_usage"

    def add_artifact(path: pathlib.Path, media_type: str, role: str = "primary") -> None:
        data = path.read_bytes()
        artifacts.append(
            {
                "path": path.relative_to(repo).as_posix(),
                "media_type": media_type,
                "size_bytes": len(data),
                "digest": {
                    "algorithm": "sha256",
                    "value": hashlib.sha256(data).hexdigest(),
                },
                "role": role,
            }
        )

    if mode == "no_edit":
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "single_file":
        bot.write_bytes(b"VALUE = 2\n")
        add_artifact(bot, "text/x-python")
    elif mode == "multi_file":
        bot.write_bytes(b"VALUE = 2\n")
        helper = repo / "helper.py"
        helper.write_bytes(b"HELPER = True\n")
        add_artifact(bot, "text/x-python")
        add_artifact(helper, "text/x-python", "supplemental")
    elif mode == "commit":
        bot.write_bytes(b"VALUE = 3\n")
        subprocess.run(["git", "-C", str(repo), "add", bot.name], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=ATV Example",
                "-c",
                "user.email=atv@example.invalid",
                "commit",
                "-m",
                "example harness edit",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        add_artifact(bot, "text/x-python")
    elif mode == "staged":
        bot.write_bytes(b"VALUE = 5\n")
        subprocess.run(["git", "-C", str(repo), "add", bot.name], check=True)
        add_artifact(bot, "text/x-python")
    elif mode == "untracked":
        path = repo / "untracked.txt"
        path.write_bytes(b"untracked\n")
        add_artifact(path, "text/plain")
    elif mode == "binary":
        (repo / "binary.bin").write_bytes(b"\x00\x01\x02")
        status = "invalid_artifact"
        failure = {
            "code": "binary-artifact",
            "scope": "artifact",
            "retryable": False,
        }
    elif mode == "tracked_binary":
        bot.write_bytes(b"\x00\x01\x02")
        status = "invalid_artifact"
        failure = {
            "code": "tracked-binary-artifact",
            "scope": "artifact",
            "retryable": False,
        }
    elif mode == "oversized":
        (repo / "oversized.txt").write_bytes(b"x" * (600 * 1024))
        status = "invalid_artifact"
        failure = {
            "code": "oversized-artifact",
            "scope": "artifact",
            "retryable": False,
        }
    elif mode == "aggregate_oversized":
        bot.write_bytes(b"x" * (3 * 1024 * 1024))
        status = "invalid_artifact"
        failure = {
            "code": "aggregate-diff-limit",
            "scope": "artifact",
            "retryable": False,
        }
    elif mode == "nonzero":
        status = "harness_crash"
        failure = {"code": "nonzero-exit", "scope": "harness", "retryable": False}
        exit_code = 7
    elif mode == "timeout":
        time.sleep(60)
    elif mode == "child_leak":
        marker = repo / "child-survived.txt"
        code = (
            "import pathlib,time;"
            "time.sleep(10);"
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        )
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "huge_stream":
        sys.stdout.write("x" * (2 * 1024 * 1024))
        sys.stdout.flush()
        return 8
    elif mode == "huge_stderr":
        sys.stderr.write("e" * (2 * 1024 * 1024))
        sys.stderr.flush()
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "unknown_model":
        model_specs = []
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "multiple_models":
        model_specs = [("example/model-a", 0), ("example/model-b", 0)]
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "disallowed_model":
        model_specs = [("disallowed/model", 0)]
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "missing_usage":
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "retries":
        model_specs = [("example/model-snapshot", 2)]
        status = "no_edit"
        failure = {"code": "no-edit", "scope": "harness", "retryable": False}
    elif mode == "secret_probe":
        observed = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("ATV_") or "SECRET" in key or "TOKEN" in key
        }
        path = repo / "observed-env.json"
        path.write_bytes(json.dumps(observed, sort_keys=True).encode("utf-8"))
        add_artifact(path, "application/json")
    elif mode == "windows_newlines":
        bot.write_bytes(b"VALUE = 4\r\n")
        add_artifact(bot, "text/x-python")
    elif mode == "argv_probe":
        path = repo / "argv.json"
        path.write_bytes(json.dumps(sys.argv[1:]).encode("utf-8"))
        add_artifact(path, "application/json")
    else:
        status = "harness_crash"
        failure = {"code": "unknown-mode", "scope": "harness", "retryable": False}
        exit_code = 2

    for model, retry_index in model_specs:
        emit(
            "model_call",
            call_id=f"model-call-{harness_sequence}",
            parent_call_id=None,
            phase="completed",
            requested_model=model,
            resolved_model=model,
            provider="example",
            provider_request_id=f"request-{harness_sequence}",
            usage_delta={
                "input_tokens": 10 if usage_known else None,
                "output_tokens": 7 if usage_known else None,
                "total_tokens": 17 if usage_known else None,
                "calls": 1 if usage_known else None,
                "cost_microusd": 100 if usage_known else None,
            },
            retry_index=retry_index,
            elapsed_ms=1,
            finish_reason="stop",
            failure=None,
        )

    total_calls = len(model_specs) if usage_known else None
    reported_usage = {
        "wall_time_ms": 1,
        "cpu_time_ms": 1,
        "model_input_tokens": 10 * len(model_specs) if usage_known else None,
        "model_output_tokens": 7 * len(model_specs) if usage_known else None,
        "model_total_tokens": 17 * len(model_specs) if usage_known else None,
        "model_calls": total_calls,
        "cost_microusd": 100 * len(model_specs) if usage_known else None,
        "tool_calls": 0,
        "memory_bytes": 1,
        "storage_bytes": sum(item["size_bytes"] for item in artifacts),
        "pids": 1,
        "stdout_bytes": 1,
        "stderr_bytes": 0,
        "artifact_bytes": sum(item["size_bytes"] for item in artifacts),
    }
    if usage_known:
        emit("usage", cumulative_reported=reported_usage)
    for artifact in artifacts:
        emit("artifact", artifact=artifact)

    output_tree_digest = (
        _canonical_digest({"artifacts": artifacts}) if status == "completed" else None
    )
    emit(
        "result",
        harness_result={
            "schema": "atv.harness-result/v1",
            "status": status,
            "exit": {
                "code": exit_code,
                "signal": None,
                "timed_out": False,
                "cancelled": False,
            },
            "output_tree_digest": output_tree_digest,
            "artifacts": artifacts,
            "reported_usage": reported_usage,
            "failure": failure,
        },
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
