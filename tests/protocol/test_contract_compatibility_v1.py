from __future__ import annotations

import pytest

from atv_bench.protocol import (
    CANONICAL_EXTERNAL_BUNDLE_SCHEMA,
    CANONICAL_EXTERNAL_RESULT_SCHEMA,
    CANONICAL_EXTERNAL_SCHEMAS,
    EXTERNAL_CONTRACT_NOTE,
    PRIVATE_EVAL_SCHEMAS,
    SchemaValidationError,
    require_canonical_external_schema,
)


def test_publication_contract_is_protocol_result_and_bundle(protocol_documents):
    assert CANONICAL_EXTERNAL_SCHEMAS == {
        "atv.trial-result/v1",
        "atv.bundle/v1",
    }
    assert (
        require_canonical_external_schema(protocol_documents["trial_result"])
        == CANONICAL_EXTERNAL_RESULT_SCHEMA
    )
    assert (
        require_canonical_external_schema(protocol_documents["bundle"])
        == CANONICAL_EXTERNAL_BUNDLE_SCHEMA
    )


def test_eval_private_records_require_explicit_mapping_before_publication():
    assert PRIVATE_EVAL_SCHEMAS == {
        "atv.trial-spec/v1",
        "atv.trial-attempt/v1",
        "atv.trial-outcome/v1",
        "atv.trial-bundle/v1",
    }
    assert "must be mapped" in EXTERNAL_CONTRACT_NOTE
    for schema in PRIVATE_EVAL_SCHEMAS:
        with pytest.raises(SchemaValidationError, match="map evaluator-private"):
            require_canonical_external_schema({"schema": schema})
