"""Controller-owned process bridge from AdapterRequest to protocol v1."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from atv_bench.adapters import build_child_environment
from atv_bench.harness_manifest import (
    _BRIDGE_CONFIG_ENV,
    render_argv_template,
)
from atv_bench.protocol import (
    ProtocolSession,
    SessionState,
    canonical_json_bytes,
    canonical_sha256,
    decode_json_object_line,
    strict_json_loads,
)


def _timestamp_factory():
    started = datetime.now(timezone.utc)
    tick = 0

    def timestamp() -> str:
        nonlocal tick
        value = started + timedelta(microseconds=tick)
        tick += 1
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    return timestamp


def _drain_stderr(stream, sink: bytearray, limit: int = 256 * 1024) -> None:
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                return
            sink.extend(chunk)
            if len(sink) > limit:
                del sink[:-limit]
    finally:
        stream.close()


def _status_for_bridge(harness_status: str) -> str:
    return {
        "completed": "ok",
        "no_edit": "no_edit",
        "policy_denied": "policy_denied",
        "budget_exhausted": "budget_exhausted",
        "task_timeout": "timeout",
        "cancelled": "cancelled",
    }.get(harness_status, "error")


def _model_summary(events: list[dict[str, Any]]) -> tuple[str, list[str], int]:
    models: list[str] = []
    retries = 0
    for event in events:
        if event["type"] != "model_call":
            continue
        model = event.get("resolved_model") or event.get("requested_model")
        if isinstance(model, str) and model not in models:
            models.append(model)
        retry_index = event.get("retry_index")
        if isinstance(retry_index, int):
            retries = max(retries, retry_index)
    return (models[0] if len(models) == 1 else "unknown", models, retries)


def _write_evidence(
    session: ProtocolSession,
    observations: list[dict[str, Any]],
) -> Path:
    transcript = session.finish()
    payload = {
        "schema": "atv.process-bridge-evidence/v1",
        "request_digest": session.request_digest,
        "observations": observations,
        "transcript_sha256": canonical_sha256(list(transcript.events)),
    }
    descriptor, raw_path = tempfile.mkstemp(
        prefix="atv-bridge-evidence-",
        suffix=".json",
    )
    os.close(descriptor)
    path = Path(raw_path)
    path.write_bytes(canonical_json_bytes(payload))
    return path


def _load_config() -> dict[str, Any]:
    encoded = os.environ.get(_BRIDGE_CONFIG_ENV)
    if not encoded:
        raise RuntimeError("controller bridge configuration is missing")
    value = strict_json_loads(base64.b64decode(encoded).decode("utf-8"))
    if not isinstance(value, dict) or value.get("schema") != (
        "atv.process-bridge-config/v1"
    ):
        raise RuntimeError("controller bridge configuration is malformed")
    return value


def _outer_request(path: Path) -> dict[str, Any]:
    value = strict_json_loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("outer adapter request is malformed")
    return value


def run_bridge(outer_request_path: Path) -> int:
    config = _load_config()
    outer = _outer_request(outer_request_path)
    trial_request = config["trial_request"]
    manifest = config["manifest"]
    command = render_argv_template(
        config["command"],
        goal=str(outer["goal"]),
        repo=str(Path(outer["repo_path"]).resolve()),
        bot_file=str(outer["bot_file"]),
        model=str(outer["model"]),
        request_path=str(outer_request_path),
    )
    child_env = build_child_environment(
        tuple(config["env_allowlist"]),
        source=os.environ,
    )
    session = ProtocolSession(manifest, trial_request)
    observations: list[dict[str, Any]] = []
    timestamp = _timestamp_factory()
    stderr_tail = bytearray()
    process = subprocess.Popen(
        command,
        cwd=str(Path(outer["repo_path"]).resolve()),
        env=child_env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process.stderr, stderr_tail),
        daemon=True,
    )
    stderr_thread.start()
    try:
        process.stdin.write(canonical_json_bytes(trial_request) + b"\n")
        process.stdin.flush()
        while True:
            line = process.stdout.readline(session.limits.max_line_bytes + 3)
            if not line:
                break
            raw_event = decode_json_object_line(
                line,
                limits=session.limits,
                event_index=len(observations),
            )
            observed_at = timestamp()
            session.receive_harness_event(
                raw_event,
                recorded_at=observed_at,
                raw_size_bytes=len(line),
            )
            observations.append(
                {
                    "kind": "harness",
                    "recorded_at": observed_at,
                    "event": raw_event,
                }
            )
            if session.state is SessionState.WAIT_ACCEPT:
                accepted_at = timestamp()
                accepted = session.record_controller_accept(recorded_at=accepted_at)
                observations.append(
                    {
                        "kind": "controller_accept",
                        "recorded_at": accepted_at,
                    }
                )
                process.stdin.write(canonical_json_bytes(accepted) + b"\n")
                process.stdin.flush()
        process.stdin.close()
        exit_code = process.wait()
        stderr_thread.join(timeout=1)
        transcript = session.finish()
        evidence_path = _write_evidence(session, observations)
        model, models, retries = _model_summary(
            [dict(event) for event in transcript.events]
        )
        usage = transcript.result["reported_usage"]
        summary = {
            "status": _status_for_bridge(transcript.result["status"]),
            "model": model,
            "models": models,
            "usage": {
                "tokens": usage["model_total_tokens"] or 0,
                "turns": usage["model_calls"] or 0,
            },
            "retries": retries,
            "bridge_evidence_path": str(evidence_path),
        }
        print(json.dumps(summary, sort_keys=True), flush=True)
        return exit_code
    except Exception as exc:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        stderr_thread.join(timeout=1)
        print(
            json.dumps(
                {
                    "status": "error",
                    "bridge_error": str(exc),
                    "stderr_tail": bytes(stderr_tail).decode(
                        "utf-8", errors="replace"
                    ),
                },
                sort_keys=True,
            ),
            flush=True,
        )
        return 2


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) != 1:
        print(
            json.dumps(
                {
                    "status": "error",
                    "bridge_error": "expected one outer request path",
                }
            )
        )
        return 2
    return run_bridge(Path(values[0]))


if __name__ == "__main__":
    raise SystemExit(main())
