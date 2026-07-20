"""Fail-closed coverage for the final credibility launch audit."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import pytest

from atv_bench.launch_audit import (
    GATE_DEFINITIONS,
    LAUNCH_GATES,
    RELEASE_GATES,
    GateStatus,
    audit_launch,
    render_json,
    render_markdown,
    required_claims_for_gate,
)


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / "scripts" / "audit_launch_gates.py"
AUDIT_DATE = "2026-07-19"
GENERATED_AT = "2026-07-19T12:00:00Z"


def _gate(report, gate_id: str):
    return next(item for item in report.gates if item.id == gate_id)


def _write_proof_fixture(
    repo_root: Path,
    *,
    gate_ids: list[str] | None = None,
    generated_at: str = GENERATED_AT,
    mutate_claims: dict[str, Callable[[dict[str, Any]], None]] | None = None,
) -> dict[str, Any]:
    selected = gate_ids or [gate.id for gate in GATE_DEFINITIONS]
    mutations = mutate_claims or {}
    gates: dict[str, Any] = {}
    for gate_id in selected:
        claims = required_claims_for_gate(gate_id)
        if gate_id in mutations:
            mutations[gate_id](claims)
        gates[gate_id] = {"result": "passed", "claims": claims}
    document = {
        "schema": "atv.launch-proof/v1",
        "generated_at": generated_at,
        "gates": gates,
    }
    data = (
        json.dumps(document, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    relative = Path("evidence") / "launch-proof.json"
    path = repo_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return {
        "schema": "atv.launch-evidence-manifest/v1",
        "proofs": {
            gate_id: {
                "artifact": relative.as_posix(),
                "sha256": digest,
                "command": "python -m pytest -q tests/test_launch_audit.py",
                "exit_code": 0,
            }
            for gate_id in selected
        },
    }


def _valid_governance() -> dict[str, Any]:
    findings = [
        {
            "id": "default_branch.protected",
            "passed": True,
            "summary": "default branch is protected",
            "evidence": {
                "classic_protection": True,
                "matching_rulesets": [],
                "errors": [],
            },
        },
        {
            "id": "default_branch.required_checks",
            "passed": True,
            "summary": "required checks are configured",
            "evidence": {
                "configured": ["hermetic", "pr-path-guard"],
                "required": ["hermetic", "pr-path-guard"],
                "missing": [],
                "errors": [],
            },
        },
        {
            "id": "default_branch.codeowners_review",
            "passed": True,
            "summary": "CODEOWNERS review is enforced",
            "evidence": {"classic": True, "ruleset": False, "errors": []},
        },
        {
            "id": "environment.league_match_reviewers",
            "passed": True,
            "summary": "independent environment review is enforced",
            "evidence": {
                "reviewers": ["benchmark-maintainers"],
                "can_admins_bypass": False,
                "prevent_self_review": True,
                "errors": [],
            },
        },
        {
            "id": "actions.sha_pinning",
            "passed": True,
            "summary": "full SHA pinning is required",
            "evidence": {
                "sha_pinning_required": True,
                "endpoint": "repos/All-The-Vibes/ATV-bench/actions/permissions",
                "status": 200,
                "error": None,
            },
        },
        {
            "id": "release.immutable_release_or_tag",
            "passed": True,
            "summary": "an immutable release exists",
            "evidence": {
                "immutable_releases": ["v1.0.0"],
                "immutable_tags": [],
                "errors": [],
            },
        },
    ]
    return {
        "schema_version": 1,
        "source": "github-rest-via-gh",
        "repository": "All-The-Vibes/ATV-bench",
        "default_branch": "main",
        "generated_at": GENERATED_AT,
        "passed": True,
        "failure_count": 0,
        "failures": [],
        "findings": findings,
    }


def _checklist_items(text: str) -> list[str]:
    return re.findall(r"^- \[ \] (.+)$", text, flags=re.MULTILINE)


def test_definitions_cover_every_launch_gate_and_release_checklist_item():
    blueprint = (ROOT / "docs" / "HARNESS_BENCHMARKING_BLUEPRINT.md").read_text(
        encoding="utf-8"
    )
    launch_text = blueprint.split("## Credibility launch gates", 1)[1].split(
        "## Sources", 1
    )[0]
    checklist = (ROOT / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert len(LAUNCH_GATES) == 20
    assert [gate.title for gate in LAUNCH_GATES[:17]] == _checklist_items(launch_text)
    assert len(RELEASE_GATES) == 44
    assert [gate.title for gate in RELEASE_GATES] == _checklist_items(checklist)
    assert len(GATE_DEFINITIONS) == 64
    assert len({gate.id for gate in GATE_DEFINITIONS}) == 64
    assert {gate.section for gate in RELEASE_GATES} == {
        "Repository and supply chain",
        "Protocol and adapters",
        "Security",
        "Tasks",
        "Experiment",
        "Analysis",
        "Publication and operations",
    }


def test_failure_registry_has_no_unmitigated_critical_silent_rows():
    blueprint = (
        ROOT / "docs" / "HARNESS_BENCHMARKING_BLUEPRINT.md"
    ).read_text(encoding="utf-8")
    registry = blueprint.split("## Failure modes registry", 1)[1].split(
        "## Test strategy",
        1,
    )[0]
    rows = []
    for line in registry.splitlines():
        if not line.startswith("| ") or line.startswith("|---"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] == "Codepath":
            continue
        rows.append(cells)

    assert len(rows) >= 15
    for codepath, failure, tests, rescue, silent, target in rows:
        evidence = f"{codepath}: {failure}: {target}"
        assert tests.startswith("Yes"), evidence
        assert rescue not in {"No", "Partial", "Warning only"}, evidence
        assert silent == "No", evidence


def test_fully_synthetic_all_green_fixture_is_launch_ready(tmp_path):
    manifest = _write_proof_fixture(tmp_path)

    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest=manifest,
    )

    assert report.launch_ready is True
    assert report.blocker_count == 0
    assert report.status_counts == {
        "achieved": 64,
        "blocked": 0,
        "failed": 0,
        "unverified": 0,
    }
    assert all(gate.status is GateStatus.ACHIEVED for gate in report.gates)
    assert all(
        evidence.path and evidence.command
        for gate in report.gates
        for evidence in gate.evidence
    )


def test_realistic_current_repository_refuses_launch():
    report = audit_launch(ROOT, audit_date=AUDIT_DATE)

    assert report.launch_ready is False
    assert report.blocker_count > 0
    assert _gate(report, "launch.task_portfolio").status is GateStatus.BLOCKED
    task_summary = _gate(report, "launch.task_portfolio").summary
    assert "5 categories" in task_summary
    assert "0 independent-human reviews" in task_summary
    assert "machine/fixture review does not pass" in task_summary
    assert _gate(report, "launch.ephemeral_runner").status is GateStatus.UNVERIFIED
    assert (
        _gate(report, "launch.process_oci_conformance").status
        is GateStatus.UNVERIFIED
    )
    assert _gate(report, "launch.external_reproduction").status is GateStatus.UNVERIFIED
    assert _gate(report, "launch.live_governance").status is GateStatus.UNVERIFIED
    assert _gate(report, "launch.immutable_release").status is GateStatus.BLOCKED
    assert all(
        evidence.path and evidence.command
        for gate in report.gates
        for evidence in gate.evidence
    )


def test_missing_and_ambiguous_evidence_fail_closed(tmp_path):
    missing = audit_launch(tmp_path, audit_date=AUDIT_DATE)
    missing_gate = _gate(missing, "launch.independent_trial")

    assert missing_gate.status is GateStatus.UNVERIFIED
    assert missing_gate.evidence[0].path.endswith(
        "#/proofs/launch.independent_trial"
    )
    assert "audit_launch_gates.py" in (missing_gate.evidence[0].command or "")

    ambiguous = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest={
            "proofs": {"launch.independent_trial": ["not", "an", "object"]}
        },
    )
    assert _gate(
        ambiguous, "launch.independent_trial"
    ).status is GateStatus.FAILED


def test_stale_proof_output_is_unverified(tmp_path):
    manifest = _write_proof_fixture(
        tmp_path,
        gate_ids=["launch.independent_trial"],
        generated_at="2026-05-01T00:00:00Z",
    )

    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest=manifest,
    )
    result = _gate(report, "launch.independent_trial")

    assert result.status is GateStatus.UNVERIFIED
    assert "stale or future-dated" in result.summary


def test_forged_passed_boolean_is_not_proof(tmp_path):
    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest={
            "proofs": {"launch.independent_trial": {"passed": True}}
        },
    )

    result = _gate(report, "launch.independent_trial")
    assert result.status is GateStatus.FAILED
    assert "claimed booleans are not proof" in result.summary


def test_forged_governance_finding_boolean_is_not_proof(tmp_path):
    governance = _valid_governance()
    for finding in governance["findings"]:
        finding["evidence"] = {}

    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        governance=governance,
    )

    assert _gate(report, "launch.live_governance").status is GateStatus.FAILED
    assert _gate(
        report, "release.repository.default_branch_protected"
    ).status is GateStatus.FAILED
    assert "supporting evidence" in _gate(
        report, "launch.live_governance"
    ).summary


def test_structurally_supported_live_governance_is_accepted(tmp_path):
    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        governance=_valid_governance(),
    )

    assert _gate(report, "launch.live_governance").status is GateStatus.ACHIEVED
    for gate_id in (
        "release.repository.default_branch_protected",
        "release.repository.required_checks",
        "release.repository.codeowners",
        "release.repository.protected_environment",
        "release.repository.actions_pinned",
    ):
        assert _gate(report, gate_id).status is GateStatus.ACHIEVED


@pytest.mark.parametrize(
    ("gate_id", "mutation", "expected"),
    [
        (
            "launch.task_portfolio",
            lambda claims: claims.update(review_kind="machine-reviewed"),
            "machine or fixture review",
        ),
        (
            "launch.signed_bundle",
            lambda claims: claims.update(signature_scheme="hmac-sha256"),
            "local HMAC integrity",
        ),
        (
            "launch.ephemeral_runner",
            lambda claims: claims.update(hard_storage_quota=False),
            "storage monitoring",
        ),
        (
            "launch.process_oci_conformance",
            lambda claims: claims.update(authority_mode="posthoc"),
            "post-hoc protocol parsing",
        ),
        (
            "launch.external_reproduction",
            lambda claims: claims.update(synthetic=True),
            "synthetic or same-operator",
        ),
        (
            "launch.signed_bundle",
            lambda claims: claims.update(trust_tier="local-self-attested"),
            "local or community bundles",
        ),
    ],
)
def test_known_credibility_distinctions_remain_blockers(
    tmp_path,
    gate_id,
    mutation,
    expected,
):
    manifest = _write_proof_fixture(
        tmp_path,
        gate_ids=[gate_id],
        mutate_claims={gate_id: mutation},
    )

    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest=manifest,
    )
    result = _gate(report, gate_id)

    assert result.status is GateStatus.BLOCKED
    assert expected in result.summary


def test_proof_artifact_digest_tampering_fails(tmp_path):
    manifest = _write_proof_fixture(
        tmp_path,
        gate_ids=["launch.independent_trial"],
    )
    proof_path = tmp_path / manifest["proofs"]["launch.independent_trial"]["artifact"]
    proof_path.write_text('{"tampered":true}\n', encoding="utf-8")

    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest=manifest,
    )

    assert _gate(
        report, "launch.independent_trial"
    ).status is GateStatus.FAILED
    assert "digest does not match" in _gate(
        report, "launch.independent_trial"
    ).summary


def test_hardlinked_proof_artifact_is_rejected_when_supported(tmp_path):
    manifest = _write_proof_fixture(
        tmp_path,
        gate_ids=["launch.independent_trial"],
    )
    relative = Path(manifest["proofs"]["launch.independent_trial"]["artifact"])
    proof_path = tmp_path / relative
    original = tmp_path / "original-proof.json"
    proof_path.replace(original)
    try:
        os.link(original, proof_path)
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")

    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest=manifest,
    )

    assert _gate(
        report, "launch.independent_trial"
    ).status is GateStatus.FAILED
    assert "unreadable" in _gate(report, "launch.independent_trial").summary


def test_rendering_is_deterministic_and_contains_evidence(tmp_path):
    manifest = _write_proof_fixture(
        tmp_path,
        gate_ids=["launch.independent_trial"],
    )
    manifest["proofs"]["launch.independent_trial"][
        "command"
    ] = "verify --label '<proof>|result'"
    report = audit_launch(
        tmp_path,
        audit_date=AUDIT_DATE,
        evidence_manifest=manifest,
    )

    first_json = render_json(report)
    second_json = render_json(report)
    first_markdown = render_markdown(report)
    second_markdown = render_markdown(report)

    assert first_json == second_json
    assert first_markdown == second_markdown
    assert json.loads(first_json)["schema"] == "atv.credibility-audit/v1"
    assert "evidence/launch-proof.json" in first_markdown.replace("\\", "/")
    assert "&lt;proof&gt;&#124;result" in first_markdown
    assert "<filter object" not in first_markdown
    assert "Next proof needed" in first_markdown
    assert "Launch blockers by severity" in first_markdown


def test_cli_rejects_self_hashed_but_unvalidated_evidence_manifest(tmp_path):
    manifest = _write_proof_fixture(tmp_path)
    manifest_path = tmp_path / "evidence-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    json_out = tmp_path / "audit.json"
    markdown_out = tmp_path / "audit.md"

    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(tmp_path),
            "--audit-date",
            AUDIT_DATE,
            "--evidence-manifest",
            str(manifest_path),
            "--json-out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert process.returncode == 2
    assert "launch audit input error" in process.stderr
    assert not json_out.exists()
    assert not markdown_out.exists()


def test_cli_returns_nonzero_for_partial_state(tmp_path):
    json_out = tmp_path / "audit.json"

    process = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--repo-root",
            str(tmp_path),
            "--audit-date",
            AUDIT_DATE,
            "--json-out",
            str(json_out),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )

    assert process.returncode == 1
    report = json.loads(json_out.read_text(encoding="utf-8"))
    assert report["launch_ready"] is False
    assert report["blocker_count"] > 0
