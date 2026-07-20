"""Compatibility boundary between private evaluator records and public contracts."""
from __future__ import annotations

from typing import Any, Mapping

from .errors import SchemaValidationError

CANONICAL_EXTERNAL_RESULT_SCHEMA = "atv.trial-result/v1"
CANONICAL_EXTERNAL_BUNDLE_SCHEMA = "atv.bundle/v1"
CANONICAL_EXTERNAL_SCHEMAS = frozenset(
    {
        CANONICAL_EXTERNAL_RESULT_SCHEMA,
        CANONICAL_EXTERNAL_BUNDLE_SCHEMA,
    }
)
PRIVATE_EVAL_SCHEMAS = frozenset(
    {
        "atv.trial-spec/v1",
        "atv.trial-attempt/v1",
        "atv.trial-outcome/v1",
        "atv.trial-bundle/v1",
    }
)
EXTERNAL_CONTRACT_NOTE = (
    "atv.trial-result/v1 and atv.bundle/v1 are the canonical external protocol "
    "contracts. Evaluator-private trial-spec, trial-attempt, trial-outcome, and "
    "trial-bundle records must be mapped into these contracts before publication."
)


def require_canonical_external_schema(document: Mapping[str, Any]) -> str:
    schema = document.get("schema")
    if schema not in CANONICAL_EXTERNAL_SCHEMAS:
        raise SchemaValidationError(
            f"{schema!r} is not a canonical external protocol contract; "
            "map evaluator-private records before publication"
        )
    assert isinstance(schema, str)
    return schema
