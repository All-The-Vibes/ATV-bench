"""Fail-closed local auditor for ATV-Bench credibility launch requirements."""
from __future__ import annotations

import dataclasses
import enum
import hashlib
import html
import json
import os
import re
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from atv_bench.capture import CaptureRejected, read_confined_regular_file


MAX_PROOF_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class GateStatus(str, enum.Enum):
    ACHIEVED = "achieved"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNVERIFIED = "unverified"


class Severity(str, enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"


@dataclasses.dataclass(frozen=True, slots=True)
class GateDefinition:
    id: str
    title: str
    source: str
    section: str
    severity: Severity
    evaluator: str
    next_proof: str
    governance_finding: str | None = None
    freshness_days: int = 30


@dataclasses.dataclass(frozen=True, slots=True)
class EvidenceReference:
    path: str | None
    command: str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True, slots=True)
class GateResult:
    id: str
    title: str
    source: str
    section: str
    severity: Severity
    status: GateStatus
    summary: str
    evidence: tuple[EvidenceReference, ...]
    next_proof: str

    @property
    def blocks_launch(self) -> bool:
        return self.status is not GateStatus.ACHIEVED

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "source": self.source,
            "section": self.section,
            "severity": self.severity.value,
            "status": self.status.value,
            "summary": self.summary,
            "evidence": [item.to_dict() for item in self.evidence],
            "next_proof": self.next_proof,
            "blocks_launch": self.blocks_launch,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class AuditReport:
    audit_date: str
    repo_root: str
    launch_ready: bool
    blocker_count: int
    status_counts: Mapping[str, int]
    severity_counts: Mapping[str, int]
    section_summaries: Mapping[str, Mapping[str, int]]
    gates: tuple[GateResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "atv.credibility-audit/v1",
            "audit_date": self.audit_date,
            "repo_root": self.repo_root,
            "launch_ready": self.launch_ready,
            "blocker_count": self.blocker_count,
            "status_counts": dict(self.status_counts),
            "severity_counts": dict(self.severity_counts),
            "section_summaries": {
                key: dict(value) for key, value in self.section_summaries.items()
            },
            "gates": [gate.to_dict() for gate in self.gates],
        }


def _gate(
    id_: str,
    title: str,
    source: str,
    section: str,
    evaluator: str,
    next_proof: str,
    *,
    severity: Severity = Severity.CRITICAL,
    governance_finding: str | None = None,
    freshness_days: int = 30,
) -> GateDefinition:
    return GateDefinition(
        id=id_,
        title=title,
        source=source,
        section=section,
        severity=severity,
        evaluator=evaluator,
        next_proof=next_proof,
        governance_finding=governance_finding,
        freshness_days=freshness_days,
    )


LAUNCH_GATES: tuple[GateDefinition, ...] = (
    _gate("launch.product_separation", "Controlled and Systems tracks are separated.", "launch", "Credibility launch gates", "product_separation", "Publish track-specific report evidence showing League, Controlled, Systems, and Resilience remain separate."),
    _gate("launch.independent_trial", "Trial is the independent unit in schema and analysis.", "launch", "Credibility launch gates", "trial_unit", "Provide fresh schema and statistical tests proving games, tests, and rounds cannot increase trial count."),
    _gate("launch.versioned_protocol_task", "Harness protocol and task schema are versioned.", "launch", "Credibility launch gates", "schemas", "Validate and publish harness, task, request, event, result, and bundle v1 schemas."),
    _gate("launch.process_oci_conformance", "Process and OCI adapters pass conformance.", "launch", "Credibility launch gates", "process_oci", "Attach one fresh conformance artifact proving both process and OCI runtimes passed the same suite."),
    _gate("launch.ephemeral_runner", "Runner is ephemeral and resource-bounded.", "launch", "Credibility launch gates", "runner_resources", "Demonstrate single-use execution plus hard CPU, memory, PID, disk, output, and wall limits."),
    _gate("launch.secret_isolation", "Provider secrets never enter harness runtime.", "launch", "Credibility launch gates", "secret_isolation", "Run a canary-secret test proving provider, GitHub, cloud, registry, and signing keys are absent."),
    _gate("launch.model_attestation", "Resolved model identity is attested.", "launch", "Credibility launch gates", "model_attestation", "Publish gateway-signed requested/resolved/provider model receipts."),
    _gate("launch.hidden_grader", "Hidden grader is inaccessible before harness exit.", "launch", "Credibility launch gates", "hidden_grader", "Provide a late-mount test and lifecycle receipt proving hidden inputs appeared only after exit."),
    _gate("launch.signed_bundle", "Trial bundle is content-addressed and signed.", "launch", "Credibility launch gates", "signed_bundle", "Publish an official Ed25519-DSSE or equivalent public-signature bundle and verify it offline."),
    _gate("launch.task_portfolio", "At least 50 reviewed tasks across five categories exist.", "launch", "Credibility launch gates", "tasks_reviewed", "Provide 50 eligible task manifests across five categories with independent human reviewer records."),
    _gate("launch.five_trials", "At least five fresh trials per cell run.", "launch", "Credibility launch gates", "five_trials", "Publish the accepted trial ledger proving at least five fresh trials in every cell."),
    _gate("launch.paired_schedule", "Scheduling is paired and randomized.", "launch", "Credibility launch gates", "paired_schedule", "Attach scheduler output proving paired coverage, randomized order, and balanced workers."),
    _gate("launch.clustered_uncertainty", "Task-clustered uncertainty is published.", "launch", "Credibility launch gates", "clustered_uncertainty", "Publish task-clustered intervals derived from the accepted immutable trial set."),
    _gate("launch.winner_rule", "Winner/equivalence rule is automated.", "launch", "Credibility launch gates", "winner_rule", "Attach a report artifact showing the automated margin and publication decision."),
    _gate("launch.contamination_retraction", "Contamination and retraction policies are published.", "launch", "Credibility launch gates", "contamination_policy", "Publish and review contamination, incident, invalidation, and retraction procedures."),
    _gate("launch.external_reproduction", "One independent reproduction succeeds.", "launch", "Credibility launch gates", "external_reproduction", "Obtain a non-affiliated operator's signed reproduction against released evidence."),
    _gate("launch.no_silent_failure", "No critical silent failure remains in the registry.", "launch", "Credibility launch gates", "silent_failure", "Run the critical-failure registry audit and resolve every open critical item."),
    _gate("launch.packaging_cp1252", "Packaging and CP1252 compatibility are proven.", "launch", "Additional required launch gates", "packaging_cp1252", "Attach clean wheel/sdist/uv installation and UTF-8 plus CP1252 CLI results."),
    _gate("launch.live_governance", "Live repository governance is enforced.", "launch", "Additional required launch gates", "live_governance", "Run scripts/audit_github_governance.py against the live repository and attach the fresh JSON report.", governance_finding="__all__", freshness_days=7),
    _gate("launch.immutable_release", "An immutable signed release/tag exists.", "launch", "Additional required launch gates", "immutable_release", "Publish a signed immutable release tag and verify it through live governance evidence.", freshness_days=365),
)


def _release_gates() -> tuple[GateDefinition, ...]:
    rows: list[GateDefinition] = []

    def add(section: str, slug: str, title: str, evaluator: str, proof: str, *, finding: str | None = None, severity: Severity = Severity.HIGH) -> None:
        rows.append(_gate(f"release.{slug}", title, "release-checklist", section, evaluator, proof, severity=severity, governance_finding=finding))

    section = "Repository and supply chain"
    add(section, "repository.default_branch_protected", "Default branch protected.", "live_governance", "Fresh live governance report with protected default branch.", finding="default_branch.protected")
    add(section, "repository.required_checks", "Required CI/security/task/reproducibility checks enabled.", "live_governance", "Fresh live governance report listing every required check.", finding="default_branch.required_checks")
    add(section, "repository.codeowners", "CODEOWNERS review enforced.", "live_governance", "Fresh live governance report proving CODEOWNERS review.", finding="default_branch.codeowners_review")
    add(section, "repository.protected_environment", "Protected official-evaluation environment configured.", "live_governance", "Fresh live governance report proving protected environment reviewers.", finding="environment.league_match_reviewers")
    add(section, "repository.actions_pinned", "Third-party Actions pinned to full commit SHAs.", "actions_pinned", "Run the workflow supply-chain audit and attach its artifact.", finding="actions.sha_pinning")
    add(section, "repository.signed_tag", "Release tag signed.", "signed_release", "Provide git/tag signature verification and immutable remote tag evidence.")
    add(section, "repository.clean_packages", "Wheel and source distribution install from clean environments.", "clean_packages", "Attach clean wheel and sdist installation logs.")
    add(section, "repository.uv_run", "`uv sync --extra run` succeeds from a clean checkout.", "uv_run", "Attach a clean-checkout locked uv sync result.")

    section = "Protocol and adapters"
    add(section, "protocol.schemas", "Schemas are versioned and published.", "schemas", "Validate all published v1 schemas.")
    add(section, "protocol.process_oci", "Process and OCI adapters pass the same conformance suite.", "process_oci", "Attach common conformance results for both runtime kinds.")
    add(section, "protocol.third_party", "A third-party adapter integrates without ATV-Bench core-code changes.", "third_party_adapter", "Provide an external adapter package and conformance artifact.")
    add(section, "protocol.unknown_versions", "Unknown protocol versions fail closed.", "unknown_versions", "Attach negative protocol-version negotiation tests.")
    add(section, "protocol.cancellation", "Cancellation kills all descendants.", "cancellation", "Attach process-tree/container cancellation and cleanup evidence.")

    section = "Security"
    add(section, "security.no_credentials", "Harness receives no provider/GitHub/cloud/signing credential.", "secret_isolation", "Attach canary credential-isolation results.", severity=Severity.CRITICAL)
    add(section, "security.gateway_egress", "Only model-gateway egress is allowed.", "gateway_egress", "Attach network-policy and peer-set verification.", severity=Severity.CRITICAL)
    add(section, "security.hidden_tests", "Hidden tests are unavailable during harness execution.", "hidden_grader", "Attach late-mount denial and lifecycle evidence.", severity=Severity.CRITICAL)
    add(section, "security.filesystem", "Symlink/junction/hardlink/special-file escapes fail.", "filesystem_confinement", "Attach cross-platform confinement tests.", severity=Severity.CRITICAL)
    add(section, "security.bombs", "Resource and output bombs remain contained.", "resource_bombs", "Attach fork/memory/disk/output bomb results.", severity=Severity.CRITICAL)
    add(section, "security.signatures", "Attestation signatures and workload identities verify.", "signature_identities", "Attach public-key DSSE verification with role-bound identities.", severity=Severity.CRITICAL)
    add(section, "security.independent_review", "Independent security review completed.", "security_review", "Obtain a signed review from an independent security reviewer.", severity=Severity.CRITICAL)

    section = "Tasks"
    add(section, "tasks.gates", "Every task passes oracle/no-op/regression/alternative/exploit/mutation gates.", "task_gates", "Attach complete task-validation results.")
    add(section, "tasks.deterministic", "Graders replay deterministically.", "grader_determinism", "Attach grader replay rate below the preregistered threshold.")
    add(section, "tasks.human_review", "Task author and independent reviewer approved.", "human_review", "Provide independent human reviewer records; fixture or machine review is insufficient.")
    add(section, "tasks.split", "Public/private/rotation split recorded.", "task_split", "Publish the task visibility and rotation manifest.")
    add(section, "tasks.contamination", "Contamination review completed.", "contamination_review", "Attach contamination review results for every task.")

    section = "Experiment"
    add(section, "experiment.trial_unit", "Fresh trial is the independent unit.", "trial_unit", "Attach schema and analysis tests for independent trial units.")
    add(section, "experiment.paired", "Harness order and worker assignment are paired/randomized.", "paired_schedule", "Attach balanced randomized schedule evidence.")
    add(section, "experiment.trial_count", "Required trial count or precision target met.", "five_trials", "Attach trial ledger or preregistered precision calculation.")
    add(section, "experiment.model_budget", "Exact model policy and budgets frozen.", "model_budget", "Publish immutable model-policy and budget digests.")
    add(section, "experiment.infrastructure", "Infrastructure failures excluded/requeued by policy.", "infrastructure_policy", "Attach accepted/excluded ledger with retry reasons.")
    add(section, "experiment.human_baseline", "Human baseline included where required.", "human_baseline", "Attach preregistered human-baseline attempts for applicable tasks.")

    section = "Analysis"
    add(section, "analysis.clustered", "Task-clustered uncertainty computed.", "clustered_uncertainty", "Attach clustered analysis artifact.")
    add(section, "analysis.margin", "Equivalence margin preregistered.", "equivalence_margin", "Publish the preregistered practical margin.")
    add(section, "analysis.winner", "Winner rule enforced automatically.", "winner_rule", "Attach automated publication decision output.")
    add(section, "analysis.sensitivity", "Category/model/budget sensitivity reported.", "sensitivity", "Publish all required sensitivity tables.")
    add(section, "analysis.accepted_set", "Accepted/excluded trial set content-addressed.", "accepted_set", "Publish immutable accepted/excluded trial manifests.")
    add(section, "analysis.independent_review", "Independent statistics review completed.", "statistics_review", "Obtain an independent signed statistical review.")

    section = "Publication and operations"
    add(section, "publication.retention", "Raw sealed evidence retention configured.", "retention", "Attach storage lifecycle configuration and retention test.")
    add(section, "publication.sanitized", "Sanitized bundles scanned and published.", "sanitized_publication", "Attach secret scan and publication manifest.")
    add(section, "publication.reproduction_command", "Reproduction command succeeds.", "reproduction_command", "Attach one-command offline reproduction output.")
    add(section, "publication.external_reproduction", "One external reproduction completed.", "external_reproduction", "Provide non-affiliated signed reproduction evidence.")
    add(section, "publication.appeals", "Appeals contact and 14-day window published.", "appeals", "Publish appeals contact, timeline, reviewer, and escalation path.")
    add(section, "publication.incident_log", "Incident/retraction log reviewed.", "incident_log_review", "Record reviewer and review date for the append-only incident log.")
    add(section, "publication.cost_forecast", "Cost/capacity forecast approved.", "cost_forecast", "Attach approved capacity, queue, and cost forecast.")
    return tuple(rows)


RELEASE_GATES = _release_gates()
GATE_DEFINITIONS = LAUNCH_GATES + RELEASE_GATES
_GATES_BY_ID = {gate.id: gate for gate in GATE_DEFINITIONS}


_VALID_CLAIMS: dict[str, dict[str, Any]] = {
    "product_separation": {"controlled_systems_separate": True, "league_separate": True},
    "trial_unit": {"independent_unit": "trial", "nested_observations_excluded": True},
    "schemas": {"versioned_schemas": ["harness", "task", "trial-request", "event", "trial-result", "bundle"]},
    "process_oci": {"process_passed": True, "oci_passed": True, "same_suite": True, "authority_mode": "interactive-controller"},
    "runner_resources": {"ephemeral": True, "resource_bounded": True, "hard_storage_quota": True},
    "secret_isolation": {"provider_secret_present": False, "github_secret_present": False, "cloud_secret_present": False, "signing_secret_present": False},
    "model_attestation": {"resolved_model_attested": True, "gateway_receipts": True},
    "hidden_grader": {"late_mount": True, "harness_access": False},
    "signed_bundle": {"signature_scheme": "ed25519-dsse", "trust_tier": "official-attested", "content_addressed": True},
    "tasks_reviewed": {"task_count": 50, "category_count": 5, "human_reviewed_count": 50, "review_kind": "independent-human"},
    "five_trials": {"min_trials_per_cell": 5},
    "paired_schedule": {"paired": True, "randomized": True, "worker_balanced": True},
    "clustered_uncertainty": {"cluster_unit": "task", "confidence_interval_published": True},
    "winner_rule": {"automated": True, "equivalence_supported": True},
    "contamination_policy": {"contamination_policy_published": True, "retraction_policy_published": True, "reviewed": True},
    "incident_log_review": {"append_only": True, "reviewed": True, "reviewer_recorded": True, "review_date_recorded": True},
    "external_reproduction": {"external_operator": True, "synthetic": False, "reproduction_succeeded": True},
    "silent_failure": {"registry_audited": True, "critical_open_count": 0},
    "packaging_cp1252": {"wheel_clean_install": True, "sdist_clean_install": True, "uv_extra_run": True, "cp1252_passed": True},
    "live_governance": {"live_governance_passed": True},
    "immutable_release": {"immutable": True, "signed": True, "tag": "v1.0.0"},
    "actions_pinned": {"all_actions_full_sha": True},
    "signed_release": {"immutable": True, "signed": True, "tag": "v1.0.0"},
    "clean_packages": {"wheel_clean_install": True, "sdist_clean_install": True},
    "uv_run": {"uv_extra_run": True, "clean_checkout": True},
    "third_party_adapter": {"external_package": True, "core_code_changes": False, "conformance_passed": True},
    "unknown_versions": {"unknown_versions_fail_closed": True},
    "cancellation": {"descendants_killed": True, "cleanup_confirmed": True},
    "gateway_egress": {"gateway_only": True, "peer_set_verified": True},
    "filesystem_confinement": {"symlink": True, "junction": True, "hardlink": True, "special_file": True},
    "resource_bombs": {"fork_bomb_contained": True, "memory_bomb_contained": True, "disk_bomb_contained": True, "output_bomb_contained": True, "hard_storage_quota": True},
    "signature_identities": {"signature_scheme": "ed25519-dsse", "role_identities_verified": True},
    "security_review": {"independent": True, "review_completed": True},
    "task_gates": {"all_tasks_passed": True, "oracle": True, "noop": True, "regression": True, "alternative": True, "exploit": True, "mutation": True},
    "grader_determinism": {"replay_count": 1000, "nondeterminism_rate": 0.0},
    "human_review": {"review_kind": "independent-human", "author_approved": True, "reviewer_approved": True},
    "task_split": {"public_count": 1, "private_count": 1, "rotating_count": 1},
    "contamination_review": {"all_tasks_reviewed": True},
    "model_budget": {"model_policy_digest_frozen": True, "budget_digest_frozen": True},
    "infrastructure_policy": {"failures_excluded": True, "requeue_policy_applied": True},
    "human_baseline": {"included_where_required": True},
    "equivalence_margin": {"preregistered": True, "margin": 0.05},
    "sensitivity": {"category": True, "model": True, "budget": True},
    "accepted_set": {"accepted_set_content_addressed": True, "excluded_set_content_addressed": True},
    "statistics_review": {"independent": True, "review_completed": True},
    "retention": {"raw_retention_configured": True, "retention_days": 180},
    "sanitized_publication": {"secret_scan_passed": True, "published": True},
    "reproduction_command": {"command_succeeded": True, "offline": True},
    "appeals": {"contact_published": True, "window_days": 14},
    "cost_forecast": {"approved": True, "capacity_forecasted": True},
}


def required_claims_for_gate(gate_id: str) -> dict[str, Any]:
    gate = _GATES_BY_ID[gate_id]
    return json.loads(json.dumps(_VALID_CLAIMS.get(gate.evaluator, {"requirement_satisfied": True})))


def _claim_errors(evaluator: str, claims: Mapping[str, Any]) -> list[str]:
    expected = _VALID_CLAIMS.get(evaluator, {"requirement_satisfied": True})
    errors: list[str] = []
    for key, value in expected.items():
        observed = claims.get(key)
        if evaluator == "tasks_reviewed" and key in {"task_count", "category_count", "human_reviewed_count"}:
            if not isinstance(observed, int) or observed < value:
                errors.append(f"{key}={observed!r} is below {value}")
        elif evaluator == "five_trials" and key == "min_trials_per_cell":
            if not isinstance(observed, int) or observed < value:
                errors.append(f"min_trials_per_cell={observed!r} is below {value}")
        elif evaluator == "grader_determinism" and key == "nondeterminism_rate":
            if not isinstance(observed, (int, float)) or observed >= 0.001:
                errors.append("grader nondeterminism must be < 0.001")
        elif evaluator == "retention" and key == "retention_days":
            if not isinstance(observed, int) or observed < value:
                errors.append(f"retention_days={observed!r} is below {value}")
        elif observed != value:
            errors.append(f"{key}={observed!r} does not equal {value!r}")
    return errors


def _known_distinction(evaluator: str, claims: Mapping[str, Any]) -> str | None:
    if evaluator in {"tasks_reviewed", "human_review"} and claims.get("review_kind") != "independent-human":
        return "machine or fixture review does not satisfy independent human review"
    if evaluator in {"signed_bundle", "signature_identities"} and str(claims.get("signature_scheme", "")).lower() in {"hmac", "hmac-sha256", "local-hmac"}:
        return "local HMAC integrity does not satisfy public signature verification"
    if evaluator in {"runner_resources", "resource_bombs"} and claims.get("hard_storage_quota") is not True:
        return "storage monitoring does not satisfy a hard aggregate storage quota"
    if evaluator == "process_oci" and claims.get("authority_mode") != "interactive-controller":
        return "post-hoc protocol parsing does not satisfy interactive controller authority"
    if evaluator == "external_reproduction" and (claims.get("synthetic") is True or claims.get("external_operator") is not True):
        return "synthetic or same-operator evidence does not satisfy external reproduction"
    if evaluator == "signed_bundle" and claims.get("trust_tier") not in {"official-attested", "independently-reproduced"}:
        return "local or community bundles do not satisfy official attestation"
    return None


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _evidence(path: Path | None, command: str | None, detail: str) -> tuple[EvidenceReference, ...]:
    return (EvidenceReference(str(path) if path else None, command, detail),)


def _governance_command() -> str:
    return (
        "python scripts/audit_github_governance.py "
        "--repo All-The-Vibes/ATV-bench --out governance-audit.json"
    )


def _governance_finding_errors(
    finding_id: str,
    row: Mapping[str, Any],
) -> list[str]:
    """Validate the evidence behind a passing governance finding.

    The companion governance auditor computes these fields from live REST
    responses. Merely copying a finding id and setting ``passed: true`` is not
    accepted by this launch auditor.
    """

    errors: list[str] = []
    if not isinstance(row.get("summary"), str) or not row["summary"].strip():
        errors.append("summary is missing")
    if not isinstance(row.get("passed"), bool):
        errors.append("passed is not boolean")
    evidence = row.get("evidence")
    if not isinstance(evidence, Mapping):
        return errors + ["evidence is not an object"]
    evidence_errors = evidence.get("errors")
    if evidence_errors not in (None, []):
        errors.append("evidence reports errors")

    if row.get("passed") is not True:
        return errors
    if finding_id == "default_branch.protected":
        classic = evidence.get("classic_protection") is True
        rulesets = evidence.get("matching_rulesets")
        if not classic and not (isinstance(rulesets, list) and bool(rulesets)):
            errors.append("no classic protection or matching active ruleset")
    elif finding_id == "default_branch.required_checks":
        configured = evidence.get("configured")
        required = evidence.get("required")
        missing = evidence.get("missing")
        if not isinstance(configured, list) or not isinstance(required, list):
            errors.append("required-check evidence is incomplete")
        elif not {"hermetic", "pr-path-guard"} <= {
            str(item).strip().casefold() for item in configured
        }:
            errors.append("required checks are not configured")
        if missing != []:
            errors.append("required checks remain missing")
    elif finding_id == "default_branch.codeowners_review":
        if evidence.get("classic") is not True and evidence.get("ruleset") is not True:
            errors.append("CODEOWNERS enforcement source is absent")
    elif finding_id == "environment.league_match_reviewers":
        reviewers = evidence.get("reviewers")
        if not isinstance(reviewers, list) or not reviewers:
            errors.append("independent reviewers are absent")
        if evidence.get("can_admins_bypass") is not False:
            errors.append("administrators can bypass review or the field is ambiguous")
        if evidence.get("prevent_self_review") is not True:
            errors.append("self-review prevention is absent")
    elif finding_id == "actions.sha_pinning":
        if evidence.get("sha_pinning_required") is not True:
            errors.append("full SHA pinning is not required")
        if evidence.get("status") != 200 or evidence.get("error") is not None:
            errors.append("Actions policy endpoint was not authoritatively readable")
    elif finding_id == "release.immutable_release_or_tag":
        releases = evidence.get("immutable_releases")
        tags = evidence.get("immutable_tags")
        if not (
            isinstance(releases, list)
            and isinstance(tags, list)
            and bool(releases or tags)
        ):
            errors.append("no immutable release or protected tag is identified")
    return errors


class LaunchAuditor:
    def __init__(
        self,
        repo_root: Path | str,
        *,
        audit_date: str | date,
        governance: Mapping[str, Any] | None = None,
        evidence_manifest: Mapping[str, Any] | None = None,
    ) -> None:
        self.repo_root = Path(os.path.abspath(os.fspath(repo_root)))
        self.audit_date = audit_date.isoformat() if isinstance(audit_date, date) else str(audit_date)
        self.audit_time = _parse_time(self.audit_date + "T23:59:59Z" if "T" not in self.audit_date else self.audit_date)
        self.governance = dict(governance or {})
        self.evidence_manifest = dict(evidence_manifest or {})
        self._proof_cache: dict[Path, Mapping[str, Any]] = {}
        self._governance_errors = self._validate_governance_document()

    def audit(self) -> AuditReport:
        gates = tuple(self._evaluate(gate) for gate in GATE_DEFINITIONS)
        blocker_count = sum(gate.blocks_launch for gate in gates)
        status_counts = {status.value: sum(gate.status is status for gate in gates) for status in GateStatus}
        severity_counts = {
            severity.value: sum(gate.blocks_launch and gate.severity is severity for gate in gates)
            for severity in Severity
        }
        sections: dict[str, dict[str, int]] = {}
        for gate in gates:
            row = sections.setdefault(gate.section, {"total": 0, "achieved": 0, "blockers": 0})
            row["total"] += 1
            row["achieved"] += gate.status is GateStatus.ACHIEVED
            row["blockers"] += gate.blocks_launch
        return AuditReport(
            audit_date=self.audit_date[:10],
            repo_root="$REPO",
            launch_ready=blocker_count == 0,
            blocker_count=blocker_count,
            status_counts=status_counts,
            severity_counts=severity_counts,
            section_summaries=sections,
            gates=gates,
        )

    def _evaluate(self, gate: GateDefinition) -> GateResult:
        proof_record = self.evidence_manifest.get("proofs", {}).get(gate.id) if isinstance(self.evidence_manifest.get("proofs", {}), Mapping) else None
        if proof_record is not None:
            return self._evaluate_proof(gate, proof_record)
        if gate.governance_finding:
            governance_result = self._evaluate_governance(gate)
            if governance_result is not None:
                return governance_result
        local = self._evaluate_local(gate)
        if local is not None:
            return local
        return self._result(
            gate,
            GateStatus.UNVERIFIED,
            "No authoritative proof artifact was supplied.",
            self._expected_evidence(gate),
        )

    def _result(self, gate: GateDefinition, status: GateStatus, summary: str, evidence: tuple[EvidenceReference, ...]) -> GateResult:
        fallback = self._expected_evidence(gate)[0]
        normalized: list[EvidenceReference] = []
        for item in evidence or (fallback,):
            path = item.path or fallback.path
            if path:
                candidate = Path(path)
                if candidate.is_absolute():
                    try:
                        path = candidate.relative_to(self.repo_root).as_posix()
                    except ValueError:
                        path = candidate.as_posix()
                else:
                    path = path.replace("\\", "/")
            normalized.append(
                EvidenceReference(
                    path,
                    item.command or fallback.command,
                    item.detail,
                )
            )
        return GateResult(
            gate.id,
            gate.title,
            gate.source,
            gate.section,
            gate.severity,
            status,
            summary,
            tuple(normalized),
            gate.next_proof,
        )

    def _expected_evidence(
        self,
        gate: GateDefinition,
    ) -> tuple[EvidenceReference, ...]:
        if gate.governance_finding:
            pointer = (
                "all required live findings"
                if gate.governance_finding == "__all__"
                else gate.governance_finding
            )
            return (
                EvidenceReference(
                    "governance-audit.json#/findings",
                    _governance_command(),
                    f"missing fresh governance evidence for {pointer}",
                ),
            )
        return (
            EvidenceReference(
                f"evidence-manifest.json#/proofs/{gate.id}",
                (
                    "python scripts/audit_launch_gates.py --repo-root . "
                    "--evidence-manifest evidence-manifest.json "
                    f"--audit-date {self.audit_date[:10]}"
                ),
                "missing content-addressed proof record",
            ),
        )

    def _evaluate_proof(self, gate: GateDefinition, record: Any) -> GateResult:
        if not isinstance(record, Mapping):
            return self._result(gate, GateStatus.FAILED, "Evidence record is not an object.", _evidence(None, None, repr(record)))
        required = {"artifact", "sha256", "command", "exit_code"}
        if not required <= set(record):
            return self._result(gate, GateStatus.FAILED, "Evidence record is missing artifact, digest, command, or exit code; claimed booleans are not proof.", _evidence(None, None, json.dumps(dict(record), sort_keys=True)))
        command = record.get("command")
        exit_code = record.get("exit_code")
        if (
            not isinstance(command, str)
            or not command.strip()
            or isinstance(exit_code, bool)
            or not isinstance(exit_code, int)
            or exit_code != 0
        ):
            return self._result(gate, GateStatus.FAILED, "Recorded command is absent or did not exit successfully.", _evidence(None, str(command) if command else None, f"exit_code={record.get('exit_code')!r}"))
        artifact = record.get("artifact")
        digest = record.get("sha256")
        if not isinstance(artifact, str) or not artifact.strip():
            return self._result(gate, GateStatus.FAILED, "Evidence artifact path is absent or invalid.", _evidence(None, command, repr(artifact)))
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            return self._result(gate, GateStatus.FAILED, "Evidence artifact digest is not a lowercase SHA-256 value.", _evidence(None, command, repr(digest)))
        relative = Path(artifact)
        if relative.is_absolute() or ".." in relative.parts:
            return self._result(gate, GateStatus.FAILED, "Evidence path is not confined to the repository.", _evidence(relative, command, "unsafe path"))
        path = self.repo_root / relative
        try:
            data = read_confined_regular_file(
                self.repo_root,
                artifact,
                max_bytes=MAX_PROOF_BYTES,
            )
        except (CaptureRejected, OSError) as exc:
            return self._result(gate, GateStatus.FAILED, f"Evidence artifact is unreadable: {exc}", _evidence(path, command, "unreadable"))
        observed = hashlib.sha256(data).hexdigest()
        if observed != digest:
            return self._result(gate, GateStatus.FAILED, "Evidence artifact digest does not match the manifest.", _evidence(path, command, f"expected={record.get('sha256')} observed={observed}"))
        try:
            document = self._proof_cache.get(path) or json.loads(data)
            self._proof_cache[path] = document
            if document.get("schema") != "atv.launch-proof/v1":
                raise ValueError("schema must be atv.launch-proof/v1")
            gate_proof = document["gates"][gate.id]
            generated_at = _parse_time(document["generated_at"])
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            return self._result(gate, GateStatus.FAILED, f"Evidence artifact is malformed or does not cover this gate: {exc}", _evidence(path, command, "malformed proof"))
        age_days = (self.audit_time - generated_at).total_seconds() / 86400
        if age_days < 0 or age_days > gate.freshness_days:
            return self._result(gate, GateStatus.UNVERIFIED, f"Evidence is stale or future-dated ({age_days:.1f} days old).", _evidence(path, command, document["generated_at"]))
        if not isinstance(gate_proof, Mapping):
            return self._result(gate, GateStatus.FAILED, "Gate proof is not an object.", _evidence(path, command, repr(gate_proof)))
        result = gate_proof.get("result")
        claims = gate_proof.get("claims")
        if result not in {"passed", "failed", "blocked"} or not isinstance(claims, Mapping):
            return self._result(gate, GateStatus.FAILED, "Gate proof lacks a typed result and claims object.", _evidence(path, command, json.dumps(dict(gate_proof), sort_keys=True)))
        distinction = _known_distinction(gate.evaluator, claims)
        if distinction:
            return self._result(gate, GateStatus.BLOCKED, distinction, _evidence(path, command, json.dumps(dict(claims), sort_keys=True)))
        if result == "failed":
            return self._result(gate, GateStatus.FAILED, "Authoritative proof reports failure.", _evidence(path, command, json.dumps(dict(claims), sort_keys=True)))
        if result == "blocked":
            return self._result(gate, GateStatus.BLOCKED, "Authoritative proof reports a blocker.", _evidence(path, command, json.dumps(dict(claims), sort_keys=True)))
        errors = _claim_errors(gate.evaluator, claims)
        if errors:
            return self._result(gate, GateStatus.BLOCKED, "; ".join(errors), _evidence(path, command, json.dumps(dict(claims), sort_keys=True)))
        return self._result(gate, GateStatus.ACHIEVED, "Fresh authoritative proof satisfies this requirement.", _evidence(path, command, f"sha256={observed}"))

    def _validate_governance_document(self) -> tuple[str, ...]:
        if not self.governance:
            return ()
        errors: list[str] = []
        if self.governance.get("schema_version") != 1:
            errors.append("schema_version must equal 1")
        if self.governance.get("source") != "github-rest-via-gh":
            errors.append("source must equal github-rest-via-gh")
        repository = self.governance.get("repository")
        if not isinstance(repository, str) or "/" not in repository:
            errors.append("repository identity is absent or invalid")
        findings = self.governance.get("findings")
        if not isinstance(findings, list):
            errors.append("findings must be an array")
            return tuple(errors)
        rows = [
            item
            for item in findings
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)
        ]
        if len(rows) != len(findings):
            errors.append("every finding must be an object with a string id")
        ids = [str(item["id"]) for item in rows]
        if len(ids) != len(set(ids)):
            errors.append("finding ids must be unique")
        computed_failures = sorted(
            str(item["id"]) for item in rows if item.get("passed") is not True
        )
        declared_failures = self.governance.get("failures")
        failure_count = self.governance.get("failure_count")
        passed = self.governance.get("passed")
        if not isinstance(declared_failures, list) or any(
            not isinstance(item, str) for item in declared_failures
        ):
            errors.append("failures must be an array of finding ids")
        elif sorted(declared_failures) != computed_failures:
            errors.append("declared failures do not match finding results")
        if (
            isinstance(failure_count, bool)
            or not isinstance(failure_count, int)
            or failure_count != len(computed_failures)
        ):
            errors.append("failure_count does not match finding results")
        if not isinstance(passed, bool) or passed is not (not computed_failures):
            errors.append("top-level passed does not match finding results")
        return tuple(errors)

    def _evaluate_governance(self, gate: GateDefinition) -> GateResult | None:
        if not self.governance:
            return None
        if self._governance_errors:
            return self._result(
                gate,
                GateStatus.FAILED,
                "Governance JSON is malformed or internally inconsistent: "
                + "; ".join(self._governance_errors),
                _evidence(
                    Path("governance-audit.json"),
                    _governance_command(),
                    "bare or inconsistent pass booleans are not authoritative",
                ),
            )
        generated = self.governance.get("generated_at")
        findings = self.governance.get("findings")
        if not isinstance(generated, str) or not isinstance(findings, list):
            return self._result(gate, GateStatus.FAILED, "Governance JSON is malformed; a passed boolean alone is not accepted.", _evidence(Path("governance-audit.json"), _governance_command(), "missing generated_at/findings"))
        try:
            age = (self.audit_time - _parse_time(generated)).total_seconds() / 86400
        except ValueError:
            return self._result(gate, GateStatus.FAILED, "Governance generated_at is invalid.", _evidence(Path("governance-audit.json"), _governance_command(), generated))
        if age < 0 or age > gate.freshness_days:
            return self._result(gate, GateStatus.UNVERIFIED, "Live governance evidence is stale or future-dated.", _evidence(Path("governance-audit.json"), _governance_command(), generated))
        rows = {row.get("id"): row for row in findings if isinstance(row, Mapping) and isinstance(row.get("id"), str)}
        if gate.governance_finding == "__all__":
            required = {
                "default_branch.protected",
                "default_branch.required_checks",
                "default_branch.codeowners_review",
                "environment.league_match_reviewers",
                "actions.sha_pinning",
                "release.immutable_release_or_tag",
            }
            missing = sorted(required - rows.keys())
            failed = sorted(
                id_
                for id_ in required
                if id_ in rows and rows[id_].get("passed") is not True
            )
            unsupported = {
                id_: _governance_finding_errors(id_, rows[id_])
                for id_ in sorted(required & rows.keys())
            }
            unsupported = {
                id_: errors for id_, errors in unsupported.items() if errors
            }
            if missing:
                return self._result(gate, GateStatus.UNVERIFIED, "Governance report lacks required findings: " + ", ".join(missing), _evidence(Path("governance-audit.json"), _governance_command(), generated))
            if failed:
                return self._result(gate, GateStatus.FAILED, "Live governance findings failed: " + ", ".join(failed), _evidence(Path("governance-audit.json"), _governance_command(), generated))
            if unsupported:
                detail = "; ".join(
                    f"{id_}: {', '.join(errors)}"
                    for id_, errors in unsupported.items()
                )
                return self._result(
                    gate,
                    GateStatus.FAILED,
                    "Passing governance booleans lack authoritative supporting evidence: "
                    + detail,
                    _evidence(
                        Path("governance-audit.json"),
                        _governance_command(),
                        generated,
                    ),
                )
            return self._result(gate, GateStatus.ACHIEVED, "Fresh live governance findings all pass.", _evidence(Path("governance-audit.json"), _governance_command(), generated))
        row = rows.get(gate.governance_finding)
        if row is None:
            return self._result(gate, GateStatus.UNVERIFIED, f"Governance finding {gate.governance_finding} is missing.", _evidence(Path("governance-audit.json"), _governance_command(), generated))
        row_errors = _governance_finding_errors(gate.governance_finding, row)
        if row.get("passed") is True and row_errors:
            return self._result(
                gate,
                GateStatus.FAILED,
                "Passing governance boolean lacks authoritative supporting evidence: "
                + "; ".join(row_errors),
                _evidence(
                    Path("governance-audit.json"),
                    _governance_command(),
                    json.dumps(row.get("evidence", {}), sort_keys=True),
                ),
            )
        status = GateStatus.ACHIEVED if row.get("passed") is True else GateStatus.FAILED
        return self._result(gate, status, str(row.get("summary") or gate.governance_finding), _evidence(Path("governance-audit.json"), _governance_command(), json.dumps(row.get("evidence", {}), sort_keys=True)))

    def _evaluate_local(self, gate: GateDefinition) -> GateResult | None:
        evaluator = gate.evaluator
        if evaluator == "product_separation":
            path = self.repo_root / "docs" / "PRODUCTS_AND_TRACKS.md"
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                return None
            phrases = ("## ATV League", "## ATV Controlled", "## ATV Systems", "system performance, not a causal harness-only effect")
            if all(phrase in text for phrase in phrases):
                return self._result(gate, GateStatus.ACHIEVED, "Track definitions and claim boundaries are explicitly separated.", _evidence(path, "content assertion over docs/PRODUCTS_AND_TRACKS.md", ", ".join(phrases)))
            return self._result(gate, GateStatus.FAILED, "Track document exists but does not contain all required claim boundaries.", _evidence(path, "content assertion", "missing required phrases"))
        if evaluator == "schemas":
            names = ("atv.harness.v1.schema.json", "atv.task.v1.schema.json", "atv.trial-request.v1.schema.json", "atv.event.v1.schema.json", "atv.trial-result.v1.schema.json", "atv.bundle.v1.schema.json")
            rows = []
            try:
                for name in names:
                    path = self.repo_root / "schemas" / name
                    document = json.loads(path.read_text(encoding="utf-8"))
                    if document.get("$schema") != "https://json-schema.org/draft/2020-12/schema" or not str(document.get("$id", "")).startswith("urn:atv-bench:schema:"):
                        raise ValueError(name)
                    rows.append(path.relative_to(self.repo_root).as_posix())
            except (OSError, ValueError, json.JSONDecodeError):
                return None
            return self._result(gate, GateStatus.ACHIEVED, "All required v1 schema documents parse with versioned offline identifiers.", _evidence(self.repo_root / "schemas", "python -m pytest -q tests/protocol/test_schemas_v1.py", "; ".join(rows)))
        if evaluator in {"tasks_reviewed", "human_review"}:
            tasks = sorted((self.repo_root / "tasks").rglob("task.json")) if (self.repo_root / "tasks").is_dir() else []
            categories: set[str] = set()
            human = 0
            review_paths: list[str] = []
            for task in tasks:
                try:
                    manifest = json.loads(task.read_text(encoding="utf-8"))
                    categories.add(str(manifest.get("category")))
                    descriptor = manifest["validation_evidence"]["independent_review"]
                    review = task.parent / descriptor["path"]
                    review_doc = json.loads(review.read_text(encoding="utf-8"))
                    reviewers = [str(item).lower() for item in review_doc.get("reviewer_ids", [])]
                    if reviewers and not any(item.startswith(("fixture", "machine", "synthetic")) for item in reviewers):
                        human += 1
                    review_paths.append(
                        review.relative_to(self.repo_root).as_posix()
                    )
                except (OSError, KeyError, TypeError, json.JSONDecodeError):
                    continue
            summary = f"{len(tasks)} tasks, {len(categories)} categories, {human} independent-human reviews"
            return self._result(gate, GateStatus.BLOCKED, summary + "; machine/fixture review does not pass the human-review gate.", _evidence(self.repo_root / "tasks", "scan task.json and independent_review descriptors", "; ".join(review_paths)))
        if evaluator == "signed_bundle":
            path = self.repo_root / "docs" / "COMMUNITY_LEAGUE.md"
            if path.is_file() and "HMAC" in path.read_text(encoding="utf-8", errors="ignore"):
                return self._result(gate, GateStatus.UNVERIFIED, "Public-signature implementation exists, but no official signed bundle proof was supplied; local HMAC is explicitly insufficient.", _evidence(path, "python -m pytest -q tests/test_security_signing.py tests/test_eval_protocol_export.py", "local HMAC is not public attestation"))
        if evaluator == "contamination_policy":
            governance = self.repo_root / "GOVERNANCE.md"
            incidents = self.repo_root / "INCIDENTS.md"
            try:
                gtext = governance.read_text(encoding="utf-8")
                itext = incidents.read_text(encoding="utf-8")
            except OSError:
                return None
            if "Contamination" in gtext and "retraction" in gtext.lower() and "append-only" in itext:
                return self._result(gate, GateStatus.ACHIEVED, "Published governance and append-only incident documents define contamination and retraction handling.", _evidence(governance, "content assertion over GOVERNANCE.md and INCIDENTS.md", incidents.relative_to(self.repo_root).as_posix()))
        if evaluator == "immutable_release":
            command = "git tag --list"
            try:
                process = subprocess.run(["git", "tag", "--list"], cwd=self.repo_root, capture_output=True, text=True, check=False)
            except OSError:
                return None
            tags = [line for line in process.stdout.splitlines() if line.strip()]
            if not tags:
                return self._result(gate, GateStatus.BLOCKED, "No local release tag exists and no fresh live immutable-release proof was supplied.", _evidence(self.repo_root / ".git", command, "no tags"))
        if evaluator == "external_reproduction":
            return self._result(gate, GateStatus.UNVERIFIED, "No independent external reproduction artifact was supplied; synthetic fixtures are not accepted.", _evidence(None, None, "synthetic != external"))
        if evaluator == "silent_failure":
            blueprint = self.repo_root / "docs" / "HARNESS_BENCHMARKING_BLUEPRINT.md"
            detail = (
                "source text alone cannot prove that every registry row has a "
                "passing regression test and fail-closed rescue"
            )
            return self._result(
                gate,
                GateStatus.UNVERIFIED,
                "Critical silent-failure clearance requires a fresh executable registry proof.",
                _evidence(
                    blueprint if blueprint.is_file() else None,
                    "python -m pytest -q tests/test_launch_audit.py",
                    detail,
                ),
            )
        if evaluator == "process_oci":
            controller = self.repo_root / "src" / "atv_bench" / "control_plane" / "trial_controller.py"
            if controller.is_file() and "posthoc-no-accepted-roundtrip" in controller.read_text(encoding="utf-8", errors="ignore"):
                return self._result(gate, GateStatus.BLOCKED, "A post-hoc protocol path remains and does not satisfy interactive controller authority.", _evidence(controller, "rg posthoc-no-accepted-roundtrip src/atv_bench/control_plane/trial_controller.py", "post-hoc != interactive authority"))
        return None


def audit_launch(
    repo_root: Path | str,
    *,
    audit_date: str | date,
    governance: Mapping[str, Any] | None = None,
    evidence_manifest: Mapping[str, Any] | None = None,
) -> AuditReport:
    return LaunchAuditor(repo_root, audit_date=audit_date, governance=governance, evidence_manifest=evidence_manifest).audit()


def render_json(report: AuditReport) -> str:
    return json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n"


def _markdown_text(value: Any) -> str:
    return (
        html.escape(str(value), quote=True)
        .replace("|", "&#124;")
        .replace("\r\n", "<br>")
        .replace("\n", "<br>")
        .replace("\r", "<br>")
    )


def _markdown_code(value: Any) -> str:
    return f"<code>{_markdown_text(value)}</code>"


def render_markdown(report: AuditReport) -> str:
    lines = [
        "# ATV-Bench Credibility Status",
        "",
        f"Audit date: **{report.audit_date}**",
        "",
        f"Official launch ready: **{'YES' if report.launch_ready else 'NO'}**",
        "",
        f"Required blockers: **{report.blocker_count}**",
        "",
        (
            "This is a fail-closed local snapshot. Unverified evidence remains "
            "a launch blocker and is not treated as achieved."
        ),
        "",
        "## Summary",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    for status in GateStatus:
        lines.append(f"| {status.value} | {report.status_counts.get(status.value, 0)} |")
    lines.extend(
        (
            "",
            "### Launch blockers by severity",
            "",
            "| Severity | Count |",
            "|---|---:|",
        )
    )
    for severity in Severity:
        lines.append(
            f"| {severity.value} | {report.severity_counts.get(severity.value, 0)} |"
        )
    for source, heading in (("launch", "Credibility launch gates"), ("release-checklist", "Official Benchmark Release Checklist")):
        lines.extend(("", f"## {heading}", ""))
        sections: list[str] = []
        for gate in report.gates:
            if gate.source == source and gate.section not in sections:
                sections.append(gate.section)
        for section in sections:
            if source == "release-checklist" or section != heading:
                lines.extend((f"### {section}", ""))
            lines.extend((
                "| Status | Severity | Requirement | Evidence | Next proof needed |",
                "|---|---|---|---|---|",
            ))
            for gate in report.gates:
                if gate.source != source or gate.section != section:
                    continue
                evidence_rows: list[str] = []
                for item in gate.evidence:
                    parts = []
                    if item.path:
                        parts.append("path: " + _markdown_code(item.path))
                    if item.command:
                        parts.append("command: " + _markdown_code(item.command))
                    if item.detail:
                        parts.append(_markdown_text(item.detail))
                    evidence_rows.append("<br>".join(parts))
                evidence = "<br><br>".join(evidence_rows)
                lines.append(
                    f"| {gate.status.value} | {gate.severity.value} | "
                    f"{_markdown_text(gate.title)} | {evidence} | "
                    f"{_markdown_text(gate.next_proof)} |"
                )
            lines.append("")
    lines.extend(
        (
            "## Evidence interpretation rules",
            "",
            "- Machine or fixture review does not satisfy independent human review.",
            "- Local HMAC integrity does not satisfy public-signature attestation.",
            "- Storage monitoring does not satisfy a hard aggregate storage quota.",
            "- Post-hoc protocol parsing does not satisfy interactive controller authority.",
            "- Synthetic fixtures do not satisfy external reproduction.",
            "- Local or community bundles do not satisfy official attestation.",
            "",
        )
    )
    return "\n".join(lines)
