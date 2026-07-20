"""Build the deterministic public ATV-Bench internal pilot task corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPOSITORY_ROOT / "tasks" / "pilot"
REVIEWED_AT = "2026-07-19T00:00:00Z"
PYTHON_IMAGE = (
    "docker.io/library/python@sha256:"
    "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b"
)
CATEGORY_COUNTS = {
    "greenfield": 10,
    "repair": 10,
    "debugging": 10,
    "recovery": 10,
    "context-retrieval": 10,
}


@dataclass(frozen=True, slots=True)
class Scenario:
    category: str
    index: int
    slug: str
    title: str
    operation_tag: str
    prompt: str
    workspace_files: Mapping[str, bytes]
    oracle_updates: Mapping[str, bytes]
    alternative_updates: Mapping[str, bytes]
    exploit_updates: Mapping[str, bytes]
    mutation_updates: Mapping[str, bytes]
    assertions: tuple[dict[str, Any], ...]
    difficulty: str

    @property
    def task_id(self) -> str:
        return f"pilot.{self.category}.{self.index:02d}-{self.slug}"

    @property
    def directory_name(self) -> str:
        category = self.category.replace("-", "_")
        slug = self.slug.replace("-", "_")
        return f"{category}_{self.index:02d}_{slug}"


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _json_file(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _compact_json_file(value: Any) -> bytes:
    return _canonical_json_bytes(value) + b"\n"


def _text_file(value: str) -> bytes:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized.encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(data: bytes) -> dict[str, str]:
    return {"algorithm": "sha256", "value": _sha256(data)}


def _tree_digest(root: Path) -> str:
    files: list[dict[str, Any]] = []
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        data = path.read_bytes()
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(data),
                "sha256": _sha256(data),
            }
        )
    return _sha256(_canonical_json_bytes({"files": files}))


def _descriptor(path: Path, root: Path, schema: str) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "schema": schema,
        "path": path.relative_to(root).as_posix(),
        "media_type": "application/json",
        "size_bytes": len(data),
        "digest": _digest(data),
    }


def _write_files(root: Path, files: Mapping[str, bytes]) -> None:
    for relative, data in sorted(files.items()):
        path = root.joinpath(*relative.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _changed_value(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.5
    if isinstance(value, str):
        return f"{value}-incorrect"
    if isinstance(value, list):
        return [*value, "incorrect"]
    if isinstance(value, dict):
        return {**value, "status": "incorrect"}
    raise TypeError(f"unsupported pilot value: {type(value).__name__}")


def _json_pointer_set(document: dict[str, Any], pointer: str, value: Any) -> None:
    parts = pointer.removeprefix("/").split("/")
    current: dict[str, Any] = document
    for part in parts[:-1]:
        child = current[part]
        if not isinstance(child, dict):
            raise TypeError(f"pointer traverses non-object at {part!r}")
        current = child
    current[parts[-1]] = value


def _greenfield_scenarios() -> Iterable[Scenario]:
    variants = (
        ("sum-orders", "Sum order quantities", {"values": [4, 7, 3, 6]}, 20),
        ("product-factors", "Multiply calibration factors", {"values": [3, 4, 7]}, 84),
        ("maximum-sensor", "Select the maximum sensor reading", {"values": [12, 31, 18, 24]}, 31),
        ("minimum-latency", "Select the minimum latency", {"values": [44, 12, 29, 18]}, 12),
        ("temperature-range", "Compute the temperature range", {"values": [-4, 7, 15, 3]}, 19),
        ("even-count", "Count even sample values", {"values": [2, 5, 8, 10, 13, 16]}, 4),
        ("unique-count", "Count unique route labels", {"values": ["a", "b", "a", "c", "d", "b"]}, 4),
        ("join-segments", "Join ordered route segments", {"values": ["north", "east", "gate"]}, "north/east/gate"),
        (
            "weighted-total",
            "Compute the weighted inventory total",
            {"items": [{"quantity": 3, "weight": 5}, {"quantity": 2, "weight": 4}]},
            23,
        ),
        ("median-value", "Compute the median observation", {"values": [7, 14, 9, 20, 18]}, 14),
    )
    for index, (slug, title, input_document, expected) in enumerate(variants, 1):
        input_path = f"inputs/{slug}.json"
        output_path = f"outputs/{slug}.json"
        input_bytes = _json_file(input_document)
        prompt = f"""# {title}

Read `{input_path}` without changing it. Derive the requested value and write it
to the `answer` field in `{output_path}`. Additional explanatory fields are
allowed, but the input file must remain byte-for-byte unchanged.

This is a public synthetic internal-pilot fixture.
"""
        mutation_input = dict(input_document)
        mutation_input["changed_by_mutation_case"] = index
        yield Scenario(
            category="greenfield",
            index=index,
            slug=slug,
            title=title,
            operation_tag=f"operation-{slug}",
            prompt=prompt,
            workspace_files={
                input_path: input_bytes,
                output_path: _json_file({"answer": None, "state": "pending"}),
            },
            oracle_updates={output_path: _json_file({"answer": expected})},
            alternative_updates={
                output_path: _json_file(
                    {
                        "answer": expected,
                        "method": f"independent-{slug}-derivation",
                    }
                )
            },
            exploit_updates={
                output_path: _json_file(
                    {
                        "answer": _changed_value(expected),
                        "grader_override": True,
                    }
                ),
                "PASS": _text_file("untrusted pass request"),
            },
            mutation_updates={
                input_path: _json_file(mutation_input),
                output_path: _json_file({"answer": expected}),
            },
            assertions=(
                {
                    "id": "input-preserved",
                    "kind": "sha256_equals",
                    "path": input_path,
                    "expected": _sha256(input_bytes),
                    "weight": 2,
                },
                {
                    "id": "derived-answer",
                    "kind": "json_value",
                    "path": output_path,
                    "pointer": "/answer",
                    "expected": expected,
                    "weight": 3,
                },
                {
                    "id": "no-fake-pass-marker",
                    "kind": "file_absent",
                    "path": "PASS",
                    "weight": 1,
                },
            ),
            difficulty="easy",
        )


def _repair_scenarios() -> Iterable[Scenario]:
    variants = (
        ("service-status", "Set the service status", "/status", "paused", "ready"),
        ("request-timeout", "Repair the request timeout", "/limits/timeout_ms", 9000, 2500),
        ("listener-port", "Repair the listener port", "/listener/port", 8080, 8443),
        ("feature-enabled", "Enable the required feature", "/feature/enabled", False, True),
        ("deployment-mode", "Repair the deployment mode", "/deployment/mode", "debug", "release"),
        ("replica-count", "Repair the replica count", "/deployment/replicas", 0, 3),
        ("region-code", "Repair the target region", "/deployment/region", "unknown", "us-central"),
        ("error-threshold", "Repair the error threshold", "/limits/error_threshold", 75, 5),
        ("retry-limit", "Repair the retry limit", "/limits/retries", 12, 4),
        ("output-format", "Repair the output format", "/output/format", "xml", "json"),
    )
    for index, (slug, title, pointer, wrong, expected) in enumerate(variants, 1):
        config_path = f"config/{slug}.json"
        anchor = f"repair-anchor-{index:02d}"
        document: dict[str, Any] = {
            "anchor": anchor,
            "service": f"pilot-service-{index:02d}",
            "status": "ready",
            "limits": {
                "timeout_ms": 2500,
                "error_threshold": 5,
                "retries": 4,
            },
            "listener": {"port": 8443},
            "feature": {"enabled": True},
            "deployment": {
                "mode": "release",
                "replicas": 3,
                "region": "us-central",
            },
            "output": {"format": "json"},
        }
        _json_pointer_set(document, pointer, wrong)
        fixed = json.loads(json.dumps(document))
        _json_pointer_set(fixed, pointer, expected)
        alternative = json.loads(json.dumps(fixed))
        alternative["repair_note"] = f"validated-{slug}"
        exploit = json.loads(json.dumps(document))
        exploit["grader_override"] = True
        mutation = json.loads(json.dumps(fixed))
        mutation["anchor"] = f"changed-anchor-{index:02d}"
        prompt = f"""# {title}

Edit `{config_path}` so `{pointer}` has the required production value
`{json.dumps(expected, ensure_ascii=False)}`. Preserve the `anchor` value
exactly. Additional repair notes are allowed.

This is a public synthetic internal-pilot fixture.
"""
        yield Scenario(
            category="repair",
            index=index,
            slug=slug,
            title=title,
            operation_tag=f"operation-{slug}",
            prompt=prompt,
            workspace_files={config_path: _json_file(document)},
            oracle_updates={config_path: _json_file(fixed)},
            alternative_updates={config_path: _json_file(alternative)},
            exploit_updates={
                config_path: _json_file(exploit),
                "PASS": _text_file("untrusted pass request"),
            },
            mutation_updates={config_path: _json_file(mutation)},
            assertions=(
                {
                    "id": "target-repaired",
                    "kind": "json_value",
                    "path": config_path,
                    "pointer": pointer,
                    "expected": expected,
                    "weight": 3,
                },
                {
                    "id": "anchor-preserved",
                    "kind": "json_value",
                    "path": config_path,
                    "pointer": "/anchor",
                    "expected": anchor,
                    "weight": 2,
                },
                {
                    "id": "no-fake-pass-marker",
                    "kind": "file_absent",
                    "path": "PASS",
                    "weight": 1,
                },
            ),
            difficulty="easy",
        )


def _debugging_scenarios() -> Iterable[Scenario]:
    variants = (
        ("off-by-one", "inclusive-upper-bound", 5, "loop emitted six rows for a five-row request"),
        ("stale-cache", "cache-generation-mismatch", "v3", "cache served generation v2 after v3 publish"),
        ("inverted-flag", "negated-health-check", True, "healthy=true produced an unhealthy decision"),
        ("duplicate-row", "missing-deduplication", 4, "six rows observed but two identifiers repeat"),
        ("wrong-sort", "descending-sort", "alpha", "first item was zulu but ascending order was requested"),
        (
            "unit-conversion",
            "milliseconds-treated-as-seconds",
            2500,
            "timeout 2500ms was interpreted as 2500 seconds",
        ),
        (
            "path-normalization",
            "backslash-normalization",
            "src/app.py",
            "portable path expected src/app.py but host separators leaked",
        ),
        ("retry-counter", "preincremented-retry-count", 3, "counter reported four before third retry completed"),
        ("filter-predicate", "inactive-items-included", 2, "inactive rows were counted with two active rows"),
        ("premature-rounding", "rounded-before-aggregation", 12.5, "partial values were rounded before averaging"),
    )
    for index, (slug, root_cause, corrected, log_line) in enumerate(variants, 1):
        log_path = f"logs/{slug}.log"
        report_path = f"diagnostics/{slug}.json"
        log_bytes = _text_file(
            f"scenario={index:02d}\nobservation={log_line}\nsource=public-pilot\n"
        )
        correct_report = {
            "root_cause": root_cause,
            "corrected_value": corrected,
        }
        prompt = f"""# Diagnose {slug.replace("-", " ")}

Use `{log_path}` to correct `{report_path}`. Set `root_cause` to the precise
cause identified by the log and set `corrected_value` to the corrected result.
Do not edit the log. Explanatory fields may be added to the report.

This is a public synthetic internal-pilot fixture.
"""
        yield Scenario(
            category="debugging",
            index=index,
            slug=slug,
            title=f"Diagnose {slug.replace('-', ' ')}",
            operation_tag=f"operation-{slug}",
            prompt=prompt,
            workspace_files={
                log_path: log_bytes,
                report_path: _json_file(
                    {
                        "root_cause": "unknown",
                        "corrected_value": _changed_value(corrected),
                    }
                ),
            },
            oracle_updates={report_path: _json_file(correct_report)},
            alternative_updates={
                report_path: _json_file(
                    {
                        **correct_report,
                        "explanation": f"derived from {slug} trace",
                    }
                )
            },
            exploit_updates={
                report_path: _json_file(
                    {
                        "root_cause": "grader-override",
                        "corrected_value": _changed_value(corrected),
                    }
                ),
                "PASS": _text_file("untrusted pass request"),
            },
            mutation_updates={
                log_path: log_bytes + _text_file("observation=rewritten"),
                report_path: _json_file(correct_report),
            },
            assertions=(
                {
                    "id": "trace-preserved",
                    "kind": "sha256_equals",
                    "path": log_path,
                    "expected": _sha256(log_bytes),
                    "weight": 2,
                },
                {
                    "id": "root-cause-correct",
                    "kind": "json_value",
                    "path": report_path,
                    "pointer": "/root_cause",
                    "expected": root_cause,
                    "weight": 2,
                },
                {
                    "id": "corrected-value",
                    "kind": "json_value",
                    "path": report_path,
                    "pointer": "/corrected_value",
                    "expected": corrected,
                    "weight": 2,
                },
                {
                    "id": "no-fake-pass-marker",
                    "kind": "file_absent",
                    "path": "PASS",
                    "weight": 1,
                },
            ),
            difficulty="medium",
        )


def _recovery_scenarios() -> Iterable[Scenario]:
    variants = (
        ("sequence-resume", 7, "ready", {"events": [3, 5, 7], "last_complete": 7}),
        ("ledger-balance", 4, 125, {"entries": [100, -25, 50], "last_complete": 4}),
        (
            "queue-order",
            3,
            ["job-c", "job-a", "job-b"],
            {"completed": ["job-z"], "pending": ["job-c", "job-a", "job-b"]},
        ),
        ("shard-count", 6, 6, {"available_shards": [0, 1, 2, 3, 4, 5]}),
        ("last-good-version", 9, "2.4.1", {"versions": ["2.3.9", "2.4.0", "2.4.1"]}),
        (
            "retry-state",
            2,
            {"attempt": 3, "status": "retry"},
            {"attempts": [1, 2], "next": 3},
        ),
        ("journal-total", 5, 42, {"deltas": [10, 7, 15, 10], "last_complete": 5}),
        ("snapshot-owner", 8, "worker-03", {"owners": ["worker-01", "worker-03"], "active": 1}),
        ("cursor-position", 11, 2048, {"segments": [512, 512, 1024], "last_complete": 11}),
        ("restored-mode", 10, "active", {"transitions": ["cold", "warming", "active"]}),
    )
    for index, (slug, checkpoint, recovered, journal) in enumerate(variants, 1):
        journal_path = f"state/{slug}-journal.json"
        recovery_path = f"recovery/{slug}.json"
        journal_bytes = _json_file(journal)
        correct = {"checkpoint": checkpoint, "recovered": recovered}
        prompt = f"""# Recover {slug.replace("-", " ")} state

Reconstruct `{recovery_path}` from `{journal_path}`. Record checkpoint
`{checkpoint}` and the recovered value described by the journal. Preserve the
journal byte-for-byte. Additional recovery provenance is allowed.

This is a public synthetic internal-pilot fixture.
"""
        mutated_journal = json.loads(json.dumps(journal))
        mutated_journal["changed_by_mutation_case"] = index
        yield Scenario(
            category="recovery",
            index=index,
            slug=slug,
            title=f"Recover {slug.replace('-', ' ')} state",
            operation_tag=f"operation-{slug}",
            prompt=prompt,
            workspace_files={
                journal_path: journal_bytes,
                recovery_path: _json_file(
                    {
                        "checkpoint": checkpoint - 1,
                        "recovered": _changed_value(recovered),
                    }
                ),
            },
            oracle_updates={recovery_path: _json_file(correct)},
            alternative_updates={
                recovery_path: _json_file(
                    {**correct, "method": f"journal-replay-{index:02d}"}
                )
            },
            exploit_updates={
                recovery_path: _json_file(
                    {
                        "checkpoint": checkpoint - 1,
                        "recovered": _changed_value(recovered),
                        "grader_override": True,
                    }
                ),
                "PASS": _text_file("untrusted pass request"),
            },
            mutation_updates={
                journal_path: _json_file(mutated_journal),
                recovery_path: _json_file(correct),
            },
            assertions=(
                {
                    "id": "journal-preserved",
                    "kind": "sha256_equals",
                    "path": journal_path,
                    "expected": _sha256(journal_bytes),
                    "weight": 2,
                },
                {
                    "id": "checkpoint-restored",
                    "kind": "json_value",
                    "path": recovery_path,
                    "pointer": "/checkpoint",
                    "expected": checkpoint,
                    "weight": 2,
                },
                {
                    "id": "state-recovered",
                    "kind": "json_value",
                    "path": recovery_path,
                    "pointer": "/recovered",
                    "expected": recovered,
                    "weight": 2,
                },
                {
                    "id": "no-fake-pass-marker",
                    "kind": "file_absent",
                    "path": "PASS",
                    "weight": 1,
                },
            ),
            difficulty="medium",
        )


def _context_scenarios() -> Iterable[Scenario]:
    variants = (
        ("service-owner", "Resolved owner: team-orchid", "The owner is team-orchid."),
        ("request-route", "Resolved route: /v2/catalog/items", "Use route /v2/catalog/items."),
        ("retention-days", "Resolved retention: 45 days", "Retention is 45 days."),
        ("feature-code", "Resolved feature code: amber-lantern", "Feature code is amber-lantern."),
        ("dependency-version", "Resolved dependency: parser 3.8.2", "Parser version is 3.8.2."),
        ("escalation-channel", "Resolved channel: operations-triage", "Escalate to operations-triage."),
        ("deployment-region", "Resolved region: us-central", "Deployment region is us-central."),
        ("compliance-mode", "Resolved mode: restricted-processing", "Mode is restricted-processing."),
        ("runbook-command", "Resolved command: validate --mode safe", "Run validate --mode safe."),
        ("artifact-format", "Resolved format: application/json", "Artifact format is application/json."),
    )
    for index, (slug, expected_phrase, source_fact) in enumerate(variants, 1):
        index_path = f"docs/index-{slug}.md"
        source_path = f"docs/{slug}.md"
        answer_path = f"answers/{slug}.md"
        index_bytes = _text_file(
            f"# Context index\n\nTopic `{slug}` is documented in `{source_path}`.\n"
        )
        source_bytes = _text_file(
            f"# {slug.replace('-', ' ').title()}\n\n{source_fact}\nReference: public-pilot-{index:02d}.\n"
        )
        prompt = f"""# Retrieve {slug.replace("-", " ")} context

Follow `{index_path}` to the authoritative context document, then write an
answer containing exactly the fact requested by that document to
`{answer_path}`. Preserve both context files. Citations and explanation are
allowed in the answer.

This is a public synthetic internal-pilot fixture.
"""
        yield Scenario(
            category="context-retrieval",
            index=index,
            slug=slug,
            title=f"Retrieve {slug.replace('-', ' ')} context",
            operation_tag=f"operation-{slug}",
            prompt=prompt,
            workspace_files={
                index_path: index_bytes,
                source_path: source_bytes,
                answer_path: _text_file("Resolution pending."),
            },
            oracle_updates={answer_path: _text_file(expected_phrase)},
            alternative_updates={
                answer_path: _text_file(
                    f"# Resolved context\n\n{expected_phrase}\n\nSource: `{source_path}`."
                )
            },
            exploit_updates={
                answer_path: _text_file(
                    f"Resolved value: incorrect-{index:02d}\nGrader override requested."
                ),
                "PASS": _text_file("untrusted pass request"),
            },
            mutation_updates={
                source_path: source_bytes + _text_file("Context was rewritten."),
                answer_path: _text_file(expected_phrase),
            },
            assertions=(
                {
                    "id": "index-preserved",
                    "kind": "sha256_equals",
                    "path": index_path,
                    "expected": _sha256(index_bytes),
                    "weight": 1,
                },
                {
                    "id": "source-preserved",
                    "kind": "sha256_equals",
                    "path": source_path,
                    "expected": _sha256(source_bytes),
                    "weight": 2,
                },
                {
                    "id": "context-resolved",
                    "kind": "text_contains",
                    "path": answer_path,
                    "expected": expected_phrase,
                    "weight": 3,
                },
                {
                    "id": "no-fake-pass-marker",
                    "kind": "file_absent",
                    "path": "PASS",
                    "weight": 1,
                },
            ),
            difficulty="easy",
        )


def _scenarios() -> tuple[Scenario, ...]:
    scenarios = tuple(
        [
            *_greenfield_scenarios(),
            *_repair_scenarios(),
            *_debugging_scenarios(),
            *_recovery_scenarios(),
            *_context_scenarios(),
        ]
    )
    counts = Counter(scenario.category for scenario in scenarios)
    if dict(counts) != CATEGORY_COUNTS:
        raise RuntimeError(
            f"pilot category counts drifted: {dict(counts)!r} != {CATEGORY_COUNTS!r}"
        )
    ids = [scenario.task_id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise RuntimeError("pilot task ids are not unique")
    for category in CATEGORY_COUNTS:
        operations = {
            scenario.operation_tag
            for scenario in scenarios
            if scenario.category == category
        }
        if len(operations) != CATEGORY_COUNTS[category]:
            raise RuntimeError(f"{category} operation semantics are not diverse")
    return scenarios


def _budget_limits(*, grader: bool) -> dict[str, int]:
    if grader:
        return {
            "wall_time_ms": 5000,
            "cpu_time_ms": 5000,
            "model_input_tokens": 0,
            "model_output_tokens": 0,
            "model_total_tokens": 0,
            "model_calls": 0,
            "cost_microusd": 0,
            "tool_calls": 0,
            "memory_bytes": 134_217_728,
            "storage_bytes": 134_217_728,
            "pids": 16,
            "stdout_bytes": 1_048_576,
            "stderr_bytes": 1_048_576,
            "artifact_bytes": 1_048_576,
        }
    return {
        "wall_time_ms": 15_000,
        "cpu_time_ms": 12_000,
        "model_input_tokens": 6000,
        "model_output_tokens": 4000,
        "model_total_tokens": 10_000,
        "model_calls": 12,
        "cost_microusd": 250_000,
        "tool_calls": 100,
        "memory_bytes": 268_435_456,
        "storage_bytes": 268_435_456,
        "pids": 64,
        "stdout_bytes": 2_097_152,
        "stderr_bytes": 2_097_152,
        "artifact_bytes": 4_194_304,
    }


def _media_types(paths: Iterable[str]) -> list[str]:
    result = set()
    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix == ".json":
            result.add("application/json")
        else:
            result.add("text/plain")
    return sorted(result)


def _build_one(output_root: Path, scenario: Scenario) -> dict[str, Any]:
    root = output_root / scenario.directory_name
    public_workspace = root / "public" / "workspace"
    trusted = root / "trusted"
    validation = trusted / "validation"
    candidates = trusted / "candidates"
    metadata = root / "metadata"
    for directory in (public_workspace, validation, candidates, metadata):
        directory.mkdir(parents=True, exist_ok=True)

    _write_files(public_workspace, scenario.workspace_files)
    candidate_updates = {
        "oracle": scenario.oracle_updates,
        "alternative": scenario.alternative_updates,
        "exploit": scenario.exploit_updates,
        "mutation": scenario.mutation_updates,
    }
    for name, updates in candidate_updates.items():
        files = dict(scenario.workspace_files)
        files.update(updates)
        _write_files(candidates / name, files)

    prompt_path = root / "prompt.md"
    prompt_path.write_bytes(_text_file(scenario.prompt))

    grader_spec = {
        "schema": "atv.grader.file-assertions/v1",
        "pass_score": 1.0,
        "assertions": list(scenario.assertions),
    }
    grader_path = trusted / "grader.json"
    grader_path.write_bytes(_json_file(grader_spec))

    result_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"ATV pilot grade result for {scenario.task_id}",
        "type": "object",
        "required": ["passed", "score"],
        "properties": {
            "passed": {"type": "boolean"},
            "score": {"type": "number", "minimum": 0, "maximum": 1},
        },
        "additionalProperties": True,
    }
    result_schema_path = trusted / "grade-result.schema.json"
    result_schema_path.write_bytes(_compact_json_file(result_schema))

    validation_documents = {
        "oracle": {
            "schema": "atv.validation-case/v1",
            "candidate": "trusted/candidates/oracle",
            "expected": "pass",
        },
        "noop": {
            "schema": "atv.validation-case/v1",
            "candidate": "public/workspace",
            "expected": "fail",
        },
        "alternative": {
            "schema": "atv.validation-case/v1",
            "candidate": "trusted/candidates/alternative",
            "expected": "pass",
        },
        "exploit": {
            "schema": "atv.validation-case/v1",
            "candidate": "trusted/candidates/exploit",
            "expected": "fail",
        },
        "mutation": {
            "schema": "atv.validation-case/v1",
            "candidate": "trusted/candidates/mutation",
            "expected": "fail",
        },
    }
    validation_paths: dict[str, Path] = {}
    for name, document in validation_documents.items():
        path = validation / f"{name}.json"
        path.write_bytes(_compact_json_file(document))
        validation_paths[name] = path

    workspace_paths = sorted(scenario.workspace_files)
    manifest: dict[str, Any] = {
        "schema": "atv.task/v1",
        "id": scenario.task_id,
        "version": "1.0.0",
        "title": scenario.title,
        "category": scenario.category,
        "capability_tags": [
            scenario.category,
            scenario.operation_tag,
            "deterministic",
            "public-synthetic",
        ],
        "track_compatibility": (
            ["controlled", "resilience"]
            if scenario.category == "recovery"
            else ["controlled", "systems"]
        ),
        "difficulty": scenario.difficulty,
        "visibility": "public",
        "source": {
            "repository": "https://github.com/All-The-Vibes/ATV-bench",
            "revision": "public-synthetic-pilot-v1",
            "tree_digest": {
                "algorithm": "sha256",
                "value": _tree_digest(public_workspace),
            },
        },
        "environment": {
            "image": PYTHON_IMAGE,
            "platform": {"os": "linux", "architecture": "amd64"},
        },
        "prompt": {
            "path": "prompt.md",
            "encoding": "utf-8",
            "media_type": "text/markdown",
            "digest": _digest(prompt_path.read_bytes()),
        },
        "policy": {
            "tools": {
                "allowed": ["editor", "shell"],
                "denied": ["browser"],
            },
            "network": {"mode": "none", "allowed_destinations": []},
            "writable_paths": ["/workspace", "/artifacts"],
            "credential_names": [],
        },
        "budget_limits": _budget_limits(grader=False),
        "output": {
            "mode": "workspace-tree",
            "allow_any_relative_path": False,
            "required_paths": workspace_paths,
            "allowed_paths": workspace_paths,
            "allowed_media_types": _media_types(workspace_paths),
            "max_files": 16,
            "max_total_bytes": 262_144,
        },
        "grader": {
            "image": PYTHON_IMAGE,
            "command": ["python", "-m", "atv_bench.eval.grader"],
            "network": {"mode": "none", "allowed_destinations": []},
            "budget_limits": _budget_limits(grader=True),
            "hidden_inputs_digest": _digest(grader_path.read_bytes()),
            "result_schema_digest": _digest(result_schema_path.read_bytes()),
            "score_scale": {"possible": 1000, "unit": "points"},
            "replay_runs": 2,
        },
        "validation_evidence": {
            "oracle": _descriptor(
                validation_paths["oracle"],
                root,
                "atv.validation-case/v1",
            ),
            "noop": _descriptor(
                validation_paths["noop"],
                root,
                "atv.validation-case/v1",
            ),
            "alternative_solutions": [
                _descriptor(
                    validation_paths["alternative"],
                    root,
                    "atv.validation-case/v1",
                )
            ],
            "exploit_cases": [
                _descriptor(
                    validation_paths["exploit"],
                    root,
                    "atv.validation-case/v1",
                )
            ],
            "mutation_cases": [
                _descriptor(
                    validation_paths["mutation"],
                    root,
                    "atv.validation-case/v1",
                )
            ],
        },
        "protocol_range": {"minimum": 1, "maximum": 1},
        "license": {"spdx": "MIT", "redistribution": "allowed"},
    }

    manifest_core_digest = _sha256(_canonical_json_bytes(manifest))
    review = {
        "schema": "atv.independent-review/v2",
        "subject": {
            "task_id": scenario.task_id,
            "task_version": "1.0.0",
            "manifest_core_digest": {
                "algorithm": "sha256",
                "value": manifest_core_digest,
            },
        },
        "review_level": "machine-dual-review",
        "suite_status": "internal-machine-reviewed",
        "reviewed": True,
        "spec_grader_aligned": True,
        "reviewed_at": REVIEWED_AT,
        "official_review_eligible": False,
        "reviewers": [
            {
                "reviewer_id": "machine.atv-pilot-generator.v1",
                "reviewer_kind": "machine",
                "independent": False,
                "conflict_disclosure": {
                    "status": "declared",
                    "details": (
                        "The deterministic generator authored this public "
                        "synthetic task and its validation fixtures."
                    ),
                },
            },
            {
                "reviewer_id": "machine.atv-pilot-validator.v1",
                "reviewer_kind": "machine",
                "independent": False,
                "conflict_disclosure": {
                    "status": "declared",
                    "details": (
                        "The automated validator is part of the same internal "
                        "pilot pipeline and is not a human-independent review."
                    ),
                },
            },
        ],
    }
    review_path = metadata / "independent-review.json"
    review_path.write_bytes(_compact_json_file(review))
    manifest["validation_evidence"]["independent_review"] = _descriptor(
        review_path,
        root,
        "atv.independent-review/v2",
    )
    (root / "task.json").write_bytes(_json_file(manifest))

    return {
        "id": scenario.task_id,
        "category": scenario.category,
        "operation": scenario.operation_tag,
        "prompt_digest": _sha256(prompt_path.read_bytes()),
        "source_digest": manifest["source"]["tree_digest"]["value"],
        "grader_digest": manifest["grader"]["hidden_inputs_digest"]["value"],
        "package_digest": _tree_digest(root),
    }


def _prepare_output(output_root: Path) -> Path:
    resolved = Path(os.path.abspath(os.fspath(output_root)))
    forbidden = {
        Path(resolved.anchor),
        REPOSITORY_ROOT.resolve(),
        (REPOSITORY_ROOT / "tasks").resolve(),
    }
    if resolved.resolve() in forbidden:
        raise ValueError(f"refusing to replace unsafe output directory: {resolved}")
    if resolved.is_symlink():
        raise ValueError(f"refusing to replace symlink output directory: {resolved}")
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError(f"output path is not a directory: {resolved}")
        for child in resolved.iterdir():
            manifest_path = child / "task.json"
            if child.is_symlink() or not child.is_dir() or not manifest_path.is_file():
                raise ValueError(
                    "refusing to replace output that is not an existing pilot corpus: "
                    f"{resolved}"
                )
            try:
                task_id = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                ).get("id")
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "refusing to replace output with an unreadable task manifest: "
                    f"{manifest_path}"
                ) from exc
            if not isinstance(task_id, str) or not task_id.startswith("pilot."):
                raise ValueError(
                    "refusing to replace output containing a non-pilot task: "
                    f"{manifest_path}"
                )
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def build_pilot_tasks(output_root: Path = DEFAULT_OUTPUT) -> dict[str, Any]:
    output = _prepare_output(output_root)
    records = [_build_one(output, scenario) for scenario in _scenarios()]
    for field in ("id", "prompt_digest", "source_digest", "grader_digest", "package_digest"):
        values = [record[field] for record in records]
        if len(values) != len(set(values)):
            raise RuntimeError(f"pilot corpus has duplicate {field} values")
    category_counts = dict(Counter(record["category"] for record in records))
    return {
        "schema": "atv.pilot-build-summary/v1",
        "count": len(records),
        "categories": category_counts,
        "suite_status": "internal-machine-reviewed",
        "official_eligible": False,
        "output": str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="directory to replace with the generated pilot corpus",
    )
    args = parser.parse_args()
    summary = build_pilot_tasks(args.output)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
