"""Versioned static reports derived only from canonical protocol bundles."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator

from atv_bench.protocol import canonical_digest, strict_json_loads
from atv_bench.security.signing import OfficialTrustPolicy

from .protocol_export import (
    budget_analysis_id,
    model_policy_analysis_id,
    verify_public_protocol_export,
)


REPORT_SCHEMA = "atv.eval-report/v1"
OFFICIAL_TRUST_TIERS = {"official-attested", "independently-reproduced"}
TRACKS = ("controlled", "systems", "resilience")


class ReportError(ValueError):
    """Canonical evidence cannot produce a valid evaluation report."""


@dataclass(frozen=True, slots=True)
class CanonicalBundleInput:
    bundle: Mapping[str, Any]
    documents: Mapping[str, bytes]
    source_url: str | None = None
    official_trust_policy: OfficialTrustPolicy | None = None


@dataclass(frozen=True, slots=True)
class ReportMetadata:
    generated_at: str
    report_version: str = "1.0.0"
    contamination_status: str = "clear"
    contamination_note: str = ""
    retraction_status: str = "none"
    retraction_note: str = ""

    def __post_init__(self) -> None:
        if self.contamination_status not in {
            "clear",
            "under-review",
            "confirmed",
        }:
            raise ReportError("invalid contamination_status")
        if self.retraction_status not in {"none", "pending", "retracted"}:
            raise ReportError("invalid retraction_status")
        if not self.generated_at or not self.report_version:
            raise ReportError("generated_at and report_version are required")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def report_schema_path() -> Path:
    candidate = Path(__file__).with_name("report.schema.json")
    if candidate.is_file():
        return candidate
    raise ReportError("packaged evaluation report schema was not found")


def _schema() -> dict[str, Any]:
    try:
        value = json.loads(report_schema_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot load report schema: {exc}") from exc
    Draft202012Validator.check_schema(value)
    return value


def _no_forbidden_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() == "elo":
                raise ReportError(f"forbidden legacy rating field at {path}.{key}")
            _no_forbidden_keys(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _no_forbidden_keys(item, path=f"{path}[{index}]")


def validate_report(report: Mapping[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(_schema()).iter_errors(report),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = "$" + "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}"
            for part in error.absolute_path
        )
        raise ReportError(f"report schema validation failed at {location}: {error.message}")
    _no_forbidden_keys(report)
    conclusion = report["global_conclusion"]
    if conclusion["status"] == "winner":
        if (
            conclusion["winner"] is None
            or not conclusion["publication_eligible"]
            or not conclusion["numeric_rankings"]
        ):
            raise ReportError("winner conclusion requires an eligible winner and numeric ranking")
    elif conclusion["numeric_rankings"] is not None:
        raise ReportError("numeric rankings are forbidden when the winner gate does not pass")
    systems = report["tracks"]["systems"]["interpretation"].lower()
    if "complete-system performance" not in systems or "does not isolate harness effect" not in systems:
        raise ReportError("Systems interpretation must state the complete-system boundary")


def _document(
    bundle: Mapping[str, Any],
    documents: Mapping[str, bytes],
    descriptor: Mapping[str, Any],
    *,
    relaxed: bool = False,
) -> Mapping[str, Any]:
    try:
        data = documents[str(descriptor["path"])]
        if relaxed:
            value = json.loads(data.decode("utf-8"))
        else:
            value = strict_json_loads(data.decode("utf-8"))
    except (KeyError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise ReportError(f"cannot parse bundled document {descriptor.get('path')!r}") from exc
    if not isinstance(value, Mapping):
        raise ReportError(f"bundled document {descriptor['path']!r} is not an object")
    return value


def _analysis_document(
    result: Mapping[str, Any],
    bundle: Mapping[str, Any],
    documents: Mapping[str, bytes],
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    try:
        descriptor = result["analysis"]["document"]
    except (KeyError, TypeError) as exc:
        raise ReportError(
            "verified trial result lacks an analysis document binding"
        ) from exc
    descriptors = [
        item
        for item in bundle["contents"]["logs"]
        if item["schema"] == "atv.paired-analysis/v1"
    ]
    if descriptors != [descriptor]:
        raise ReportError(
            "verified trial result analysis binding differs from bundle logs"
        )
    return _document(bundle, documents, descriptor, relaxed=True), descriptor


def _mean(values: Iterable[float | int | None]) -> float | None:
    materialized = [float(value) for value in values if value is not None]
    if not materialized:
        return None
    return round(sum(materialized) / len(materialized), 6)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _score(result: Mapping[str, Any]) -> float | None:
    score = result["evaluation"]["score"]
    if score is None:
        return None
    return round(score["earned"] / score["possible"], 6)


def _model_policy_key(policy: Mapping[str, Any]) -> str:
    return model_policy_analysis_id(policy)


def _record(item: CanonicalBundleInput) -> dict[str, Any]:
    trust_tier = str(item.bundle.get("trust_tier", ""))
    if (
        trust_tier in OFFICIAL_TRUST_TIERS
        and item.official_trust_policy is None
    ):
        raise ReportError(
            "official canonical evidence requires an explicit OfficialTrustPolicy"
        )
    try:
        result = verify_public_protocol_export(
            item.bundle,
            item.documents,
            official_trust_policy=item.official_trust_policy,
        )
    except Exception as exc:
        raise ReportError(f"canonical bundle verification failed: {exc}") from exc
    analysis, analysis_descriptor = _analysis_document(
        result,
        item.bundle,
        item.documents,
    )
    task_manifest = _document(
        item.bundle,
        item.documents,
        item.bundle["contents"]["task_manifest"],
    )
    request = _document(
        item.bundle,
        item.documents,
        item.bundle["contents"]["trial_request"],
    )
    trust_tier = str(item.bundle["trust_tier"])
    official = trust_tier in OFFICIAL_TRUST_TIERS and bool(result["rankable"])
    failure = result["failure"]
    authoritative_usage = result["usage"]["authoritative"]
    model_policy = result["model_policy"]
    budget = result["budget"]
    budget_digest = budget["limits_digest"]["value"]
    budget_profile_id = budget["profile_id"]
    return {
        "evidence_id": str(item.bundle["bundle_id"]),
        "bundle_digest": canonical_digest(item.bundle)["value"],
        "bundle_url": item.source_url,
        "trial_id": result["trial_id"],
        "attempt_id": result["attempt_id"],
        "trust_tier": trust_tier,
        "official": official,
        "rankable": bool(result["rankable"]),
        "track": result["track"],
        "status": result["status"],
        "failure": dict(failure) if failure is not None else None,
        "task_id": result["task"]["id"],
        "task_version": result["task"]["version"],
        "task_category": task_manifest["category"],
        "harness_id": result["harness"]["id"],
        "harness_version": result["harness"]["version"],
        "model_policy": dict(model_policy),
        "model_policy_key": _model_policy_key(model_policy),
        "budget_profile_id": budget_profile_id,
        "budget_id": budget_analysis_id(
            budget_profile_id,
            request["budget_limits"],
        ),
        "budget_digest": budget_digest,
        "budget_limits": dict(request["budget_limits"]),
        "score": _score(result),
        "task_success": result["evaluation"]["task_success"],
        "cost_microusd": authoritative_usage["cost_microusd"],
        "duration_ms": result["execution"]["duration_ms"],
        "infrastructure": bool(failure and failure["infrastructure"]),
        "benchmark_release": result["benchmark_release"],
        "protocol_version": result["protocol_version"],
        "task_set": dict(result["task_set"]),
        "runner": dict(result["execution"]["runner"]),
        "analysis": dict(analysis),
        "analysis_digest": analysis_descriptor["digest"]["value"],
    }


def _metrics(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(records)
    successes = sum(record["status"] == "success" for record in records)
    completed = sum(record["task_success"] is not None for record in records)
    infrastructure = sum(record["infrastructure"] for record in records)
    crashes = sum(record["status"] == "harness_crash" for record in records)
    timeouts = sum(record["status"] == "task_timeout" for record in records)
    return {
        "trial_count": count,
        "task_success_rate": _ratio(successes, count),
        "graded_result_rate": _ratio(completed, count),
        "reliability_rate": _ratio(count - infrastructure, count),
        "infrastructure_error_rate": _ratio(infrastructure, count),
        "crash_rate": _ratio(crashes, count),
        "timeout_rate": _ratio(timeouts, count),
        "mean_score": _mean(record["score"] for record in records),
        "mean_cost_microusd": _mean(record["cost_microusd"] for record in records),
        "mean_duration_ms": _mean(record["duration_ms"] for record in records),
    }


def _breakdown(
    records: Sequence[Mapping[str, Any]],
    *,
    field: str,
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        groups.setdefault(str(record[field]), []).append(record)
    return [
        {"key": key, **_metrics(groups[key])}
        for key in sorted(groups)
    ]


def _failure_taxonomy(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str, bool], int] = {}
    for record in records:
        failure = record["failure"]
        if failure is None:
            continue
        key = (failure["code"], failure["scope"], bool(failure["infrastructure"]))
        counts[key] = counts.get(key, 0) + 1
    return [
        {
            "code": code,
            "scope": scope,
            "infrastructure": infrastructure,
            "count": count,
        }
        for (code, scope, infrastructure), count in sorted(counts.items())
    ]


def _unique_analyses(
    records: Sequence[Mapping[str, Any]],
) -> list[tuple[Mapping[str, Any], Mapping[str, Any]]]:
    seen: set[tuple[str, str, str, str, str, str]] = set()
    identity_by_digest: dict[str, tuple[str, str, str, str, str]] = {}
    result: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for record in sorted(records, key=lambda value: value["bundle_digest"]):
        identity = (
            record["model_policy"]["id"],
            record["model_policy"]["version"],
            record["model_policy"]["policy_digest"]["value"],
            record["budget_profile_id"],
            record["budget_digest"],
        )
        prior_identity = identity_by_digest.setdefault(
            record["analysis_digest"],
            identity,
        )
        if prior_identity != identity:
            raise ReportError(
                "one paired analysis digest was reused across different "
                "model-policy/budget identities"
            )
        key = (record["analysis_digest"], *identity)
        if key in seen:
            continue
        seen.add(key)
        result.append((record, record["analysis"]))
    return result


def _track(
    track_id: str,
    records: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    scoped = [record for record in records if record["track"] == track_id]
    official = [record for record in scoped if record["official"]]
    unofficial = [record for record in scoped if not record["official"]]
    analyses = _unique_analyses(scoped)
    analysis_summaries: list[dict[str, Any]] = []
    paired_effects: list[dict[str, Any]] = []
    infrastructure_exclusions: list[dict[str, Any]] = []
    quality_failures: list[dict[str, Any]] = []
    for source, analysis in analyses:
        analysis_summaries.append(
            {
                "model_policy": source["model_policy_key"],
                "harness_a": analysis["harness_a"],
                "harness_b": analysis["harness_b"],
                "task_count": analysis["task_count"],
                "rankable_trial_count": analysis["rankable_trial_count"],
                "mean_difference": analysis["mean_difference"],
                "confidence": analysis["confidence"],
                "ci_low": analysis["ci_low"],
                "ci_high": analysis["ci_high"],
                "practical_margin": analysis["equivalence_margin"],
                "descriptive_decision": analysis["descriptive_decision"],
                "publication_decision": analysis["publication_decision"],
                "publication_eligible": analysis["publication_eligible"],
                "official": source["official"],
            }
        )
        for effect in analysis.get("effects", []):
            paired_effects.append(
                {
                    "task_id": effect["task_id"],
                    "model_policy": source["model_policy_key"],
                    "harness_a": analysis["harness_a"],
                    "harness_b": analysis["harness_b"],
                    "mean_a": effect["mean_a"],
                    "mean_b": effect["mean_b"],
                    "difference": effect["difference"],
                    "repetitions": list(effect["repetitions"]),
                    "official": source["official"],
                }
            )
        for exclusion in analysis.get("infrastructure_exclusions", []):
            infrastructure_exclusions.append(
                {
                    **dict(exclusion),
                    "model_policy": source["model_policy_key"],
                    "official": source["official"],
                }
            )
        for failure in analysis.get("quality_gate_failures", []):
            quality_failures.append(
                {
                    **dict(failure),
                    "model_policy": source["model_policy_key"],
                    "official": source["official"],
                }
            )
    interpretations = {
        "controlled": (
            "Harness-effect performance under fixed model, task, policy, and budget conditions."
        ),
        "systems": (
            "Complete-system performance; does not isolate harness effect."
        ),
        "resilience": (
            "Recovery and reliability under injected failures and constrained execution."
        ),
    }
    return {
        "id": track_id,
        "label": track_id.capitalize(),
        "interpretation": interpretations[track_id],
        "official_results": [record["evidence_id"] for record in official],
        "unofficial_results": [record["evidence_id"] for record in unofficial],
        "metrics": {
            "official": _metrics(official),
            "unofficial": _metrics(unofficial),
        },
        "breakdowns": {
            "official": {
                "tasks": _breakdown(official, field="task_id"),
                "categories": _breakdown(official, field="task_category"),
                "model_policies": _breakdown(official, field="model_policy_key"),
                "budgets": _breakdown(official, field="budget_id"),
            },
            "unofficial": {
                "tasks": _breakdown(unofficial, field="task_id"),
                "categories": _breakdown(unofficial, field="task_category"),
                "model_policies": _breakdown(unofficial, field="model_policy_key"),
                "budgets": _breakdown(unofficial, field="budget_id"),
            },
        },
        "paired_effects": sorted(
            paired_effects,
            key=lambda value: (
                value["model_policy"],
                value["task_id"],
                not value["official"],
            ),
        ),
        "analysis_summaries": sorted(
            analysis_summaries,
            key=lambda value: (value["model_policy"], not value["official"]),
        ),
        "infrastructure_exclusions": sorted(
            infrastructure_exclusions,
            key=lambda value: (
                value["model_policy"],
                value["task_id"],
                value["harness_id"],
            ),
        ),
        "failure_taxonomy": {
            "official": _failure_taxonomy(official),
            "unofficial": _failure_taxonomy(unofficial),
        },
        "quality_gate_failures": sorted(
            quality_failures,
            key=lambda value: (value["model_policy"], value["code"]),
        ),
    }


def _global_conclusion(
    records: Sequence[Mapping[str, Any]],
    metadata: ReportMetadata,
) -> dict[str, Any]:
    official_controlled = [
        record
        for record in records
        if record["track"] == "controlled" and record["official"]
    ]
    analyses = _unique_analyses(official_controlled)
    gate_failures: list[dict[str, str]] = []
    if metadata.contamination_status != "clear":
        gate_failures.append(
            {
                "code": "unresolved-contamination",
                "message": f"contamination status is {metadata.contamination_status}",
            }
        )
    if metadata.retraction_status != "none":
        gate_failures.append(
            {
                "code": "unresolved-retraction",
                "message": f"retraction status is {metadata.retraction_status}",
            }
        )
    policy_rows: list[dict[str, Any]] = []
    all_publication_gates_pass = True
    for source, analysis in analyses:
        failures = list(analysis.get("quality_gate_failures", []))
        eligible = bool(analysis.get("publication_eligible")) and not failures
        all_publication_gates_pass &= eligible
        decision = analysis.get("publication_decision", "inconclusive")
        winner = None
        if decision == "a_better":
            winner = analysis.get("harness_a")
        elif decision == "b_better":
            winner = analysis.get("harness_b")
        policy_rows.append(
            {
                "model_policy": source["model_policy_key"],
                "policy_digest": source["model_policy"]["policy_digest"]["value"],
                "decision": decision,
                "winner": winner,
                "publication_eligible": eligible,
            }
        )
        for failure in failures:
            gate_failures.append(
                {
                    "code": failure["code"],
                    "message": (
                        f"{source['model_policy_key']}: {failure['message']}"
                    ),
                }
            )

    unique_policy_digests = {row["policy_digest"] for row in policy_rows}
    winners = {row["winner"] for row in policy_rows if row["winner"] is not None}
    decisions = {row["decision"] for row in policy_rows}
    directions_by_policy: dict[str, set[tuple[str, str | None]]] = {}
    for row in policy_rows:
        directions_by_policy.setdefault(row["policy_digest"], set()).add(
            (row["decision"], row["winner"])
        )
    within_policy_conflict = any(
        len(directions) > 1 for directions in directions_by_policy.values()
    )
    incident_clear = (
        metadata.contamination_status == "clear"
        and metadata.retraction_status == "none"
    )
    winner: str | None = None
    rankings: list[dict[str, Any]] | None = None
    publication_eligible = False

    if not policy_rows:
        status = "no-data"
        language = (
            "No official Controlled evidence is available. No global harness conclusion."
        )
    elif not all_publication_gates_pass or not incident_clear:
        status = "inconclusive"
        language = (
            "Official publication gates or incident checks did not pass. "
            "No global winner is reported."
        )
    elif within_policy_conflict:
        status = "inconclusive"
        language = (
            "At least one immutable model policy has conflicting analyses. "
            "No global winner is reported."
        )
        gate_failures.append(
            {
                "code": "within-policy-direction-conflict",
                "message": "one immutable model policy has conflicting directions",
            }
        )
    elif len(unique_policy_digests) < 2:
        status = "category-only"
        row = policy_rows[0]
        if row["winner"]:
            language = (
                f"{row['winner']} was better under one immutable model policy only; "
                "this is not a global harness winner."
            )
        elif row["decision"] == "equivalent":
            language = (
                "The harnesses were equivalent under one immutable model policy only."
            )
        else:
            language = "One model policy is insufficient for a global conclusion."
        gate_failures.append(
            {
                "code": "insufficient-model-policy-replication",
                "message": "direction must persist across at least two model policies",
            }
        )
    elif decisions == {"equivalent"}:
        status = "equivalent"
        language = (
            "Official Controlled evidence is practically equivalent across "
            "multiple immutable model policies."
        )
        publication_eligible = True
    elif len(winners) == 1 and decisions <= {"a_better", "b_better"}:
        status = "winner"
        winner = next(iter(winners))
        language = (
            f"{winner} is the official Controlled-track winner across "
            f"{len(unique_policy_digests)} immutable model policies."
        )
        rankings = [{"rank": 1, "harness_id": winner}]
        publication_eligible = True
    else:
        status = "inconclusive"
        language = (
            "Model-policy directions conflict or include inconclusive results. "
            "No global winner is reported."
        )
        gate_failures.append(
            {
                "code": "model-policy-direction-conflict",
                "message": "controlled policy directions are not stable",
            }
        )
    return {
        "status": status,
        "winner": winner,
        "language": language,
        "publication_eligible": publication_eligible,
        "numeric_rankings": rankings,
        "gate_failures": sorted(gate_failures, key=lambda value: value["code"]),
        "model_policy_directions": sorted(
            policy_rows, key=lambda value: value["model_policy"]
        ),
    }


def generate_report(
    bundles: Iterable[CanonicalBundleInput],
    *,
    metadata: ReportMetadata,
) -> dict[str, Any]:
    records = sorted(
        (_record(item) for item in bundles),
        key=lambda value: value["bundle_digest"],
    )
    evidence = [
        {
            key: record[key]
            for key in (
                "evidence_id",
                "bundle_digest",
                "bundle_url",
                "trial_id",
                "attempt_id",
                "trust_tier",
                "official",
                "rankable",
                "track",
                "status",
                "task_id",
                "task_version",
                "task_category",
                "harness_id",
                "harness_version",
                "model_policy_key",
                "budget_id",
                "budget_digest",
                "benchmark_release",
                "protocol_version",
                "analysis_digest",
            )
        }
        for record in records
    ]
    report = {
        "schema": REPORT_SCHEMA,
        "report_version": metadata.report_version,
        "generated_at": metadata.generated_at,
        "versions": {
            "benchmark_releases": sorted(
                {record["benchmark_release"] for record in records}
            ),
            "protocol_versions": sorted(
                {record["protocol_version"] for record in records}
            ),
            "task_sets": sorted(
                {
                    (
                        record["task_set"]["id"],
                        record["task_set"]["version"],
                        record["task_set"]["manifest_digest"]["value"],
                    )
                    for record in records
                }
            ),
            "runners": sorted(
                {
                    (
                        record["runner"]["id"],
                        record["runner"]["version"],
                        record["runner"]["manifest_digest"]["value"],
                    )
                    for record in records
                }
            ),
            "report": metadata.report_version,
        },
        "integrity": {
            "contamination": {
                "status": metadata.contamination_status,
                "note": metadata.contamination_note,
            },
            "retraction": {
                "status": metadata.retraction_status,
                "note": metadata.retraction_note,
            },
        },
        "tracks": {
            track_id: _track(track_id, records)
            for track_id in TRACKS
        },
        "global_conclusion": _global_conclusion(records, metadata),
        "evidence": evidence,
        "warnings": (
            ["No canonical bundle evidence was supplied."]
            if not records
            else []
        ),
    }
    # Tuples are not the public JSON representation.
    report["versions"]["task_sets"] = [
        {"id": id_, "version": version, "manifest_digest": digest}
        for id_, version, digest in report["versions"]["task_sets"]
    ]
    report["versions"]["runners"] = [
        {"id": id_, "version": version, "manifest_digest": digest}
        for id_, version, digest in report["versions"]["runners"]
    ]
    validate_report(report)
    return report


def report_json_bytes(report: Mapping[str, Any]) -> bytes:
    validate_report(report)
    return (
        json.dumps(
            report,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _viewer_template_path() -> Path:
    candidate = Path(__file__).resolve().parents[1] / "view" / "eval.html"
    if candidate.is_file():
        return candidate
    raise ReportError("evaluation report viewer template was not found")


def render_report_html(report: Mapping[str, Any]) -> str:
    validate_report(report)
    template = _viewer_template_path().read_text(encoding="utf-8")
    marker = '"__ATV_REPORT_JSON__"'
    if template.count(marker) != 1:
        raise ReportError("viewer template has an invalid report marker")
    payload = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    payload = (
        payload.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return template.replace(marker, payload)


def write_static_report(
    report: Mapping[str, Any],
    directory: Path | str,
) -> tuple[Path, Path]:
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    report_path = target / "report.json"
    viewer_path = target / "index.html"
    report_path.write_bytes(report_json_bytes(report))
    viewer_path.write_text(render_report_html(report), encoding="utf-8", newline="\n")
    return report_path, viewer_path
