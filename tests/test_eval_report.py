"""Schema, generator, conclusion-gate, and offline-viewer tests."""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

import pytest

from atv_bench.eval.protocol_export import (
    ProtocolExport,
)
from atv_bench.eval.report import (
    CanonicalBundleInput,
    ReportError,
    ReportMetadata,
    generate_report,
    render_report_html,
    report_json_bytes,
    report_schema_path,
    validate_report,
    write_static_report,
)
from atv_bench.protocol import canonical_digest, canonical_json_bytes, sha256_bytes


ROOT = Path(__file__).resolve().parents[1]
GENERATED_AT = "2026-07-19T15:00:00Z"


def _fixture_module():
    name = "_atv_eval_protocol_fixture"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).with_name("test_eval_protocol_export.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@dataclass(frozen=True)
class _ReportExport:
    exported: ProtocolExport
    trust_policy: object | None


def _export(
    *,
    official: bool,
    task_count: int = 50,
    model_policy_id: str = "controlled-model",
    model_policy_digest_character: str = "9",
    model_policy_digest: str | None = None,
    budget_profile_id: str = "equal-cost",
    budget_max_cost_microusd: int = 500_000,
    direction: str = "a_better",
    track: str = "controlled",
) -> _ReportExport:
    module = _fixture_module()
    fixture = module._fixture(
        official=official,
        task_count=task_count,
        repetitions=5,
        model_policy_id=model_policy_id,
        model_policy_digest_character=model_policy_digest_character,
        model_policy_digest=model_policy_digest,
        budget_profile_id=budget_profile_id,
        budget_max_cost_microusd=budget_max_cost_microusd,
        direction=direction,
        track=track,
    )
    exported = module.export_protocol_bundle(
        spec=fixture.spec,
        attempt=fixture.attempt,
        outcome=fixture.outcome,
        grade=fixture.grade,
        analysis=fixture.analysis,
        evidence=fixture.evidence,
    )
    return _ReportExport(exported=exported, trust_policy=fixture.trust_policy)


def _community(source: _ReportExport) -> _ReportExport:
    bundle = deepcopy(source.exported.bundle)
    documents = dict(source.exported.documents)
    result = json.loads(documents["trial/result.json"])
    result["trust_tier"] = "community-reproducible"
    result["rankable"] = False
    data = canonical_json_bytes(result)
    documents["trial/result.json"] = data
    descriptor = bundle["contents"]["trial_result"]
    descriptor["size_bytes"] = len(data)
    descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(data),
    }
    bundle["trust_tier"] = "community-reproducible"
    bundle["contents_digest"] = canonical_digest(bundle["contents"])
    bundle["bundle_id"] = "bundle-" + bundle["contents_digest"]["value"][:32]
    candidate = ProtocolExport(bundle=bundle, documents=documents)
    candidate.verify()
    return _ReportExport(exported=candidate, trust_policy=None)


def _input(
    source: _ReportExport,
    suffix: str,
    *,
    include_policy: bool = True,
) -> CanonicalBundleInput:
    return CanonicalBundleInput(
        bundle=source.exported.bundle,
        documents=source.exported.documents,
        source_url=f"https://evidence.example/{suffix}.json",
        official_trust_policy=(
            source.trust_policy if include_policy else None
        ),
    )


def _reused_analysis_input(
    *,
    source: _ReportExport,
    target: _ReportExport,
    suffix: str,
) -> CanonicalBundleInput:
    bundle = deepcopy(target.exported.bundle)
    documents = dict(target.exported.documents)
    target_descriptor = next(
        item
        for item in bundle["contents"]["logs"]
        if item["schema"] == "atv.paired-analysis/v1"
    )
    source_descriptor = next(
        item
        for item in source.exported.bundle["contents"]["logs"]
        if item["schema"] == "atv.paired-analysis/v1"
    )
    source_data = source.exported.documents[source_descriptor["path"]]
    documents[target_descriptor["path"]] = source_data
    target_descriptor["size_bytes"] = len(source_data)
    target_descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(source_data),
    }
    result_descriptor = bundle["contents"]["trial_result"]
    result = json.loads(documents[result_descriptor["path"]])
    result["analysis"]["document"] = deepcopy(target_descriptor)
    result_data = canonical_json_bytes(result)
    documents[result_descriptor["path"]] = result_data
    result_descriptor["size_bytes"] = len(result_data)
    result_descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(result_data),
    }
    bundle["contents_digest"] = canonical_digest(bundle["contents"])
    bundle["bundle_id"] = (
        "bundle-" + bundle["contents_digest"]["value"][:32]
    )
    return CanonicalBundleInput(
        bundle=bundle,
        documents=documents,
        source_url=f"https://evidence.example/{suffix}.json",
        official_trust_policy=target.trust_policy,
    )


def _metadata(**overrides) -> ReportMetadata:
    values = {"generated_at": GENERATED_AT}
    values.update(overrides)
    return ReportMetadata(**values)


def _walk_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def test_schema_accepts_generated_empty_report_and_rejects_invalid_shapes():
    report = generate_report([], metadata=_metadata())
    validate_report(report)
    assert report["global_conclusion"]["status"] == "no-data"
    assert report["global_conclusion"]["numeric_rankings"] is None
    assert report["warnings"] == ["No canonical bundle evidence was supplied."]

    invalid = deepcopy(report)
    invalid["unexpected"] = True
    with pytest.raises(ReportError, match="schema validation"):
        validate_report(invalid)

    missing_track = deepcopy(report)
    del missing_track["tracks"]["resilience"]
    with pytest.raises(ReportError, match="schema validation"):
        validate_report(missing_track)

    illegal_rank = deepcopy(report)
    illegal_rank["global_conclusion"]["numeric_rankings"] = [
        {"rank": 1, "harness_id": "not-allowed"}
    ]
    with pytest.raises(ReportError, match="numeric rankings"):
        validate_report(illegal_rank)


def test_official_report_input_requires_explicit_matching_trust_policy():
    signed = _export(
        official=True,
        model_policy_id="signed-policy",
        model_policy_digest_character="c",
    )
    with pytest.raises(ReportError, match="explicit OfficialTrustPolicy"):
        generate_report(
            [_input(signed, "missing-policy", include_policy=False)],
            metadata=_metadata(),
        )

    other = _export(
        official=True,
        model_policy_id="other-policy",
        model_policy_digest_character="d",
    )
    mismatched = CanonicalBundleInput(
        bundle=signed.exported.bundle,
        documents=signed.exported.documents,
        source_url="https://evidence.example/mismatched-policy.json",
        official_trust_policy=other.trust_policy,
    )
    with pytest.raises(ReportError, match="canonical bundle verification failed"):
        generate_report([mismatched], metadata=_metadata())


def test_unsigned_or_mutated_official_evidence_fails_closed():
    signed = _export(
        official=True,
        model_policy_id="signed-policy",
        model_policy_digest_character="e",
    )
    mutated_documents = dict(signed.exported.documents)
    mutated_documents["trial/request.json"] += b" "
    mutated = CanonicalBundleInput(
        bundle=signed.exported.bundle,
        documents=mutated_documents,
        source_url="https://evidence.example/mutated.json",
        official_trust_policy=signed.trust_policy,
    )
    with pytest.raises(ReportError, match="canonical bundle verification failed"):
        generate_report([mutated], metadata=_metadata())

    local = _export(official=False, task_count=2)
    bundle = deepcopy(local.exported.bundle)
    documents = dict(local.exported.documents)
    result = json.loads(documents["trial/result.json"])
    result["trust_tier"] = "official-attested"
    result["rankable"] = True
    data = canonical_json_bytes(result)
    documents["trial/result.json"] = data
    descriptor = bundle["contents"]["trial_result"]
    descriptor["size_bytes"] = len(data)
    descriptor["digest"] = {
        "algorithm": "sha256",
        "value": sha256_bytes(data),
    }
    bundle["trust_tier"] = "official-attested"
    bundle["contents_digest"] = canonical_digest(bundle["contents"])
    bundle["bundle_id"] = "bundle-" + bundle["contents_digest"]["value"][:32]
    unsigned = CanonicalBundleInput(
        bundle=bundle,
        documents=documents,
        source_url="https://evidence.example/unsigned.json",
        official_trust_policy=signed.trust_policy,
    )
    with pytest.raises(ReportError, match="canonical bundle verification failed"):
        generate_report([unsigned], metadata=_metadata())


@pytest.mark.parametrize(
    ("source_kwargs", "target_kwargs"),
    [
        (
            {
                "model_policy_id": "policy-a",
                "model_policy_digest_character": "1",
            },
            {
                "model_policy_id": "policy-b",
                "model_policy_digest_character": "2",
            },
        ),
        (
            {
                "budget_profile_id": "budget-a",
                "budget_max_cost_microusd": 500_000,
            },
            {
                "budget_profile_id": "budget-b",
                "budget_max_cost_microusd": 600_000,
            },
        ),
    ],
)
def test_report_rejects_analysis_reused_across_policy_or_budget_identity(
    source_kwargs,
    target_kwargs,
):
    source = _export(official=True, **source_kwargs)
    target = _export(official=True, **target_kwargs)
    reused = _reused_analysis_input(
        source=source,
        target=target,
        suffix="reused-analysis",
    )
    with pytest.raises(
        ReportError,
        match="canonical bundle verification failed",
    ):
        generate_report([reused], metadata=_metadata())


def test_report_uses_full_policy_and_budget_digests_without_prefix_collisions():
    shared_prefix = "a" * 12
    first_digest = shared_prefix + "1" * 52
    second_digest = shared_prefix + "2" * 52
    first = _export(
        official=True,
        model_policy_id="same-policy",
        model_policy_digest=first_digest,
    )
    second = _export(
        official=True,
        model_policy_id="same-policy",
        model_policy_digest=second_digest,
    )
    report = generate_report(
        [_input(first, "same-prefix-a"), _input(second, "same-prefix-b")],
        metadata=_metadata(),
    )

    policy_keys = {
        row["model_policy_key"] for row in report["evidence"]
    }
    budget_ids = {row["budget_id"] for row in report["evidence"]}
    assert policy_keys == {
        f"same-policy@1.0.0#sha256:{first_digest}",
        f"same-policy@1.0.0#sha256:{second_digest}",
    }
    assert all(len(key.rsplit(":", 1)[1]) == 64 for key in policy_keys)
    assert all(len(identity.rsplit(":", 1)[1]) == 64 for identity in budget_ids)
    assert report["global_conclusion"]["status"] == "winner"


def test_one_policy_refuses_global_winner_and_uses_category_only_language():
    first = _export(official=True)
    report = generate_report([_input(first, "one-policy")], metadata=_metadata())
    conclusion = report["global_conclusion"]
    assert conclusion["status"] == "category-only"
    assert conclusion["winner"] is None
    assert conclusion["numeric_rankings"] is None
    assert "one immutable model policy only" in conclusion["language"]


def test_two_immutable_policies_with_consistent_direction_produce_winner():
    first = _export(
        official=True,
        model_policy_id="policy-a",
        model_policy_digest_character="1",
    )
    second = _export(
        official=True,
        model_policy_id="policy-b",
        model_policy_digest_character="2",
    )
    report = generate_report(
        [_input(first, "policy-a"), _input(second, "policy-b")],
        metadata=_metadata(),
    )
    conclusion = report["global_conclusion"]
    assert conclusion["status"] == "winner"
    assert conclusion["winner"] == "harness-a"
    assert conclusion["publication_eligible"] is True
    assert conclusion["numeric_rankings"] == [
        {"rank": 1, "harness_id": "harness-a"}
    ]
    assert len(conclusion["model_policy_directions"]) == 2


def test_conflicting_model_policies_are_inconclusive_without_numeric_rank():
    first = _export(
        official=True,
        model_policy_id="policy-a",
        model_policy_digest_character="3",
        direction="a_better",
    )
    second = _export(
        official=True,
        model_policy_id="policy-b",
        model_policy_digest_character="4",
        direction="b_better",
    )
    report = generate_report(
        [_input(first, "conflict-a"), _input(second, "conflict-b")],
        metadata=_metadata(),
    )
    conclusion = report["global_conclusion"]
    assert conclusion["status"] == "inconclusive"
    assert conclusion["winner"] is None
    assert conclusion["numeric_rankings"] is None
    assert any(
        item["code"] == "model-policy-direction-conflict"
        for item in conclusion["gate_failures"]
    )


def test_two_policy_equivalence_and_incident_state_suppress_winner():
    first = _export(
        official=True,
        model_policy_id="policy-a",
        model_policy_digest_character="5",
        direction="equivalent",
    )
    second = _export(
        official=True,
        model_policy_id="policy-b",
        model_policy_digest_character="6",
        direction="equivalent",
    )
    equivalent = generate_report(
        [_input(first, "equivalent-a"), _input(second, "equivalent-b")],
        metadata=_metadata(),
    )
    assert equivalent["global_conclusion"]["status"] == "equivalent"
    assert equivalent["global_conclusion"]["numeric_rankings"] is None

    incident = generate_report(
        [_input(first, "incident-a"), _input(second, "incident-b")],
        metadata=_metadata(
            contamination_status="under-review",
            contamination_note="open review",
        ),
    )
    assert incident["global_conclusion"]["status"] == "inconclusive"
    assert incident["global_conclusion"]["numeric_rankings"] is None
    assert any(
        item["code"] == "unresolved-contamination"
        for item in incident["global_conclusion"]["gate_failures"]
    )


def test_local_evidence_is_unofficial_and_never_mixed_into_winner_table():
    official = _export(
        official=True,
        model_policy_id="official-policy",
        model_policy_digest_character="7",
    )
    local = _export(official=False, task_count=2)
    community = _community(_export(official=False, task_count=2))
    report = generate_report(
        [
            _input(official, "official"),
            _input(local, "local"),
            _input(community, "community"),
        ],
        metadata=_metadata(),
    )
    controlled = report["tracks"]["controlled"]
    assert len(controlled["official_results"]) == 1
    assert len(controlled["unofficial_results"]) == 2
    assert controlled["metrics"]["official"]["trial_count"] == 1
    assert controlled["metrics"]["unofficial"]["trial_count"] == 2
    assert report["global_conclusion"]["status"] == "category-only"
    unofficial_tiers = {
        row["trust_tier"] for row in report["evidence"] if not row["official"]
    }
    assert unofficial_tiers == {
        "local-self-attested",
        "community-reproducible",
    }


def test_tracks_breakdowns_metrics_and_systems_boundary_are_present():
    controlled = _export(
        official=True,
        model_policy_id="controlled-policy",
        model_policy_digest_character="8",
        track="controlled",
    )
    systems = _export(
        official=True,
        model_policy_id="systems-policy",
        model_policy_digest_character="9",
        track="systems",
    )
    resilience = _export(
        official=True,
        model_policy_id="resilience-policy",
        model_policy_digest_character="a",
        track="resilience",
    )
    report = generate_report(
        [
            _input(controlled, "controlled"),
            _input(systems, "systems"),
            _input(resilience, "resilience"),
        ],
        metadata=_metadata(),
    )
    for track_id in ("controlled", "systems", "resilience"):
        track = report["tracks"][track_id]
        assert track["metrics"]["official"]["trial_count"] == 1
        assert track["breakdowns"]["official"]["tasks"]
        assert track["breakdowns"]["official"]["categories"]
        assert track["breakdowns"]["official"]["model_policies"]
        assert track["breakdowns"]["official"]["budgets"]
        assert track["analysis_summaries"]
        summary = track["analysis_summaries"][0]
        assert "ci_low" in summary and "ci_high" in summary
        assert "practical_margin" in summary
        assert "descriptive_decision" in summary
        assert "publication_decision" in summary
        assert track["paired_effects"]
        assert "failure_taxonomy" in track
    systems_text = report["tracks"]["systems"]["interpretation"].lower()
    assert "complete-system performance" in systems_text
    assert "does not isolate harness effect" in systems_text


def test_no_legacy_rating_field_raw_evidence_links_and_versions():
    exported = _export(
        official=True,
        model_policy_id="evidence-policy",
        model_policy_digest_character="b",
    )
    report = generate_report(
        [_input(exported, "raw-bundle")],
        metadata=_metadata(),
    )
    assert all(str(key).lower() != "elo" for key in _walk_keys(report))
    row = report["evidence"][0]
    assert re.fullmatch(r"[0-9a-f]{64}", row["bundle_digest"])
    assert row["bundle_url"] == "https://evidence.example/raw-bundle.json"
    assert re.fullmatch(r"[0-9a-f]{64}", row["analysis_digest"])
    assert report["versions"]["benchmark_releases"]
    assert report["versions"]["protocol_versions"] == [1]
    assert report["versions"]["task_sets"]
    assert report["versions"]["runners"]


def test_viewer_copies_are_identical_offline_and_escape_embedded_data(tmp_path):
    bundled = ROOT / "src" / "atv_bench" / "view" / "eval.html"
    assert report_schema_path() == ROOT / "src" / "atv_bench" / "eval" / "report.schema.json"
    template = bundled.read_text(encoding="utf-8")
    assert "https://" not in template
    assert "http://" not in template
    assert "innerHTML" not in template
    assert "textContent" in template

    attack = '</script><img src=x onerror="alert(1)">'
    report = generate_report(
        [],
        metadata=_metadata(
            contamination_status="under-review",
            contamination_note=attack,
        ),
    )
    rendered = render_report_html(report)
    assert attack not in rendered
    assert "\\u003c/script\\u003e" in rendered
    assert rendered == render_report_html(report)

    report_path, viewer_path = write_static_report(report, tmp_path)
    assert report_path.read_bytes() == report_json_bytes(report)
    assert viewer_path.read_text(encoding="utf-8") == rendered


def test_generation_is_deterministic_and_empty_viewer_state_is_embedded():
    report = generate_report([], metadata=_metadata())
    assert report_json_bytes(report) == report_json_bytes(
        generate_report([], metadata=_metadata())
    )
    html = render_report_html(report)
    match = re.search(
        r'<script id="report-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert match
    embedded = json.loads(match.group(1))
    assert embedded["global_conclusion"]["status"] == "no-data"
    assert embedded["evidence"] == []
