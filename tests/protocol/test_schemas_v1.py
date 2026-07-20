from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from atv_bench.protocol import (
    HarnessStatus,
    IntegrityError,
    SchemaKind,
    SchemaStore,
    SchemaValidationError,
    TrialStatus,
    canonical_digest,
    default_schema_store,
    verify_bundle_manifest,
)


def test_all_v1_schemas_are_draft_2020_12_meta_valid_and_use_offline_ids():
    store = default_schema_store()
    files = sorted(store.directory.glob("*.schema.json"))
    assert {path.name for path in files} == {
        "atv.bundle.v1.schema.json",
        "atv.common.v1.schema.json",
        "atv.event.v1.schema.json",
        "atv.harness.v1.schema.json",
        "atv.task.v1.schema.json",
        "atv.trial-request.v1.schema.json",
        "atv.trial-result.v1.schema.json",
    }
    for path in files:
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert document["$id"].startswith("urn:atv-bench:schema:")
        Draft202012Validator.check_schema(document)


def test_valid_documents_cover_every_public_schema(protocol_documents):
    store = default_schema_store()
    store.validate(protocol_documents["harness"], SchemaKind.HARNESS)
    store.validate(protocol_documents["task"], SchemaKind.TASK)
    store.validate(protocol_documents["request"], SchemaKind.TRIAL_REQUEST)
    for event in protocol_documents["events"]:
        store.validate(event, SchemaKind.EVENT)
    store.validate(protocol_documents["trial_result"], SchemaKind.TRIAL_RESULT)
    store.validate(protocol_documents["bundle"], SchemaKind.BUNDLE)


@pytest.mark.parametrize(
    ("document_name", "kind"),
    [
        ("request", SchemaKind.TRIAL_REQUEST),
        ("hello", SchemaKind.EVENT),
    ],
)
def test_unknown_protocol_versions_fail_closed(
    protocol_documents,
    document_name,
    kind,
):
    document = deepcopy(protocol_documents[document_name])
    document["protocol_version"] = 2
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(document, kind)


@pytest.mark.parametrize(
    ("document_name", "kind", "path"),
    [
        ("harness", SchemaKind.HARNESS, ("security",)),
        ("task", SchemaKind.TASK, ("policy",)),
        ("request", SchemaKind.TRIAL_REQUEST, ("policy",)),
        ("hello", SchemaKind.EVENT, ()),
        ("trial_result", SchemaKind.TRIAL_RESULT, ("execution",)),
        ("bundle", SchemaKind.BUNDLE, ("contents",)),
    ],
)
def test_security_critical_objects_reject_unknown_fields(
    protocol_documents, document_name, kind, path
):
    document = deepcopy(protocol_documents[document_name])
    target = document
    for key in path:
        target = target[key]
    target["untrusted_extra"] = "must-fail"
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(document, kind)


def test_harness_runtime_is_content_addressed(protocol_documents):
    harness = deepcopy(protocol_documents["harness"])
    del harness["runtime"]["executable_digest"]
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(harness, SchemaKind.HARNESS)

    harness = deepcopy(protocol_documents["harness"])
    harness["runtime"] = {
        "kind": "oci",
        "image": "ghcr.io/example/harness:latest",
        "entrypoint": ["/opt/harness/run"],
        "working_directory": "/workspace",
    }
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(harness, SchemaKind.HARNESS)


@pytest.mark.parametrize(
    "unsafe",
    ["../secret", "/absolute", "a/../../secret", r"a\\b", "./main.py", "a/./b", "a/"],
)
def test_relative_artifact_paths_are_canonical_and_confined(
    protocol_documents, unsafe
):
    task = deepcopy(protocol_documents["task"])
    task["output"]["required_paths"] = [unsafe]
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(task, SchemaKind.TASK)


def test_workspace_tree_tasks_do_not_require_a_single_primary_file(protocol_documents):
    task = deepcopy(protocol_documents["task"])
    task["output"]["mode"] = "workspace-tree"
    task["output"]["allow_any_relative_path"] = True
    task["output"]["required_paths"] = []
    task["output"]["allowed_paths"] = []
    default_schema_store().validate(task, SchemaKind.TASK)


def test_trial_request_rejects_raw_credential_value(protocol_documents):
    request = deepcopy(protocol_documents["request"])
    request["policy"]["credentials"][0]["secret"] = "provider-key"
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(request, SchemaKind.TRIAL_REQUEST)


def test_harness_terminal_event_cannot_claim_authoritative_outcomes(protocol_documents):
    event = deepcopy(protocol_documents["events"][-1])
    event["harness_result"]["status"] = "infrastructure_error"
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(event, SchemaKind.EVENT)

    event = deepcopy(protocol_documents["events"][-1])
    event["harness_result"] = deepcopy(protocol_documents["trial_result"])
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(event, SchemaKind.EVENT)


def test_authoritative_result_enforces_evaluation_and_rankability(protocol_documents):
    result = deepcopy(protocol_documents["trial_result"])
    result["status"] = "success"
    result["evaluation"]["task_outcome"] = "fail"
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(result, SchemaKind.TRIAL_RESULT)

    result = deepcopy(protocol_documents["trial_result"])
    result["rankable"] = True
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(result, SchemaKind.TRIAL_RESULT)


def test_python_status_enums_exactly_match_schema_contracts():
    store = default_schema_store()
    event_schema = store.schema(SchemaKind.EVENT)
    harness_values = set(
        event_schema["$defs"]["harnessResult"]["properties"]["status"]["enum"]
    )
    trial_values = set(
        store.schema(SchemaKind.TRIAL_RESULT)["properties"]["status"]["enum"]
    )
    assert harness_values == {status.value for status in HarnessStatus}
    assert trial_values == {status.value for status in TrialStatus}


def test_noncompleted_harness_result_requires_typed_failure(protocol_documents):
    event = deepcopy(protocol_documents["events"][-1])
    event["harness_result"]["status"] = "harness_crash"
    event["harness_result"]["failure"] = None
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(event, SchemaKind.EVENT)


def test_completed_harness_result_requires_clean_exit(protocol_documents):
    event = deepcopy(protocol_documents["events"][-1])
    event["harness_result"]["exit"]["code"] = 1
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(event, SchemaKind.EVENT)


def test_official_bundle_requires_role_typed_attestations(protocol_documents):
    bundle = deepcopy(protocol_documents["bundle"])
    bundle["trust_tier"] = "official-attested"
    with pytest.raises(SchemaValidationError):
        default_schema_store().validate(bundle, SchemaKind.BUNDLE)


def test_official_bundle_accepts_complete_role_typed_evidence(protocol_documents):
    bundle = deepcopy(protocol_documents["bundle"])
    bundle["trust_tier"] = "official-attested"
    roles = ["admission", "harness-build", "execution", "model", "evaluation"]
    bundle["contents"]["attestations"] = [
        {
            "role": role,
            "document": {
                "schema": "in-toto.statement/v1",
                "path": f"attestations/{role}.json",
                "media_type": "application/json",
                "size_bytes": 1,
                "digest": {"algorithm": "sha256", "value": str(index) * 64},
            },
        }
        for index, role in enumerate(roles)
    ]
    bundle["contents_digest"] = canonical_digest(bundle["contents"])
    default_schema_store().validate(bundle, SchemaKind.BUNDLE)


def test_model_free_official_bundle_does_not_require_model_evidence(
    protocol_documents,
):
    bundle = deepcopy(protocol_documents["bundle"])
    bundle["trust_tier"] = "official-attested"
    bundle["contents"]["model_receipts"] = []
    roles = ["admission", "harness-build", "execution", "evaluation"]
    bundle["contents"]["attestations"] = [
        {
            "role": role,
            "document": {
                "schema": "in-toto.statement/v1",
                "path": f"attestations/{role}.json",
                "media_type": "application/json",
                "size_bytes": 1,
                "digest": {"algorithm": "sha256", "value": str(index) * 64},
            },
        }
        for index, role in enumerate(roles)
    ]
    bundle["contents_digest"] = canonical_digest(bundle["contents"])
    default_schema_store().validate(bundle, SchemaKind.BUNDLE)


def test_bundle_contents_digest_verifies_and_tampering_fails(protocol_documents):
    bundle = deepcopy(protocol_documents["bundle"])
    verify_bundle_manifest(bundle)
    bundle["contents"]["artifacts"][0]["size_bytes"] += 1
    with pytest.raises(IntegrityError):
        verify_bundle_manifest(bundle)


def test_schema_store_infers_versioned_kind(protocol_documents):
    store = default_schema_store()
    assert store.validate_inferred(protocol_documents["request"]) is SchemaKind.TRIAL_REQUEST


def test_schema_files_live_in_the_dedicated_root_directory():
    root = Path(__file__).resolve().parents[2]
    assert default_schema_store().directory == (root / "schemas").resolve()


def test_embedded_wheel_fallback_is_byte_identical_and_usable(protocol_documents):
    from atv_bench.protocol._embedded_schemas import embedded_schema_texts

    root = Path(__file__).resolve().parents[2] / "schemas"
    embedded = embedded_schema_texts()
    expected = {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(root.glob("*.schema.json"))
    }
    assert embedded == expected
    store = SchemaStore.from_texts(embedded)
    store.validate(protocol_documents["request"], SchemaKind.TRIAL_REQUEST)


@pytest.mark.filterwarnings("ignore:jsonschema.RefResolver is deprecated")
def test_legacy_jsonschema_resolver_fallback_remains_offline(
    protocol_documents, monkeypatch
):
    import atv_bench.protocol.schemas as schema_module
    from atv_bench.protocol._embedded_schemas import embedded_schema_texts

    monkeypatch.setattr(schema_module, "Registry", None)
    monkeypatch.setattr(schema_module, "Resource", None)
    store = SchemaStore.from_texts(embedded_schema_texts())
    assert store.registry is None
    store.validate(protocol_documents["hello"], SchemaKind.EVENT)
