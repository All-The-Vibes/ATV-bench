"""ATV-Bench harness protocol v1.

This package is independent of existing vendor adapters. It defines the versioned
schemas, strict JSONL transcript state machine, capability negotiation, canonical
serialization, hashing, and black-box conformance checks used by future adapters.
"""
from .canonical import (
    CANONICALIZATION_ID,
    canonical_digest,
    canonical_json_bytes,
    canonical_json_text,
    canonical_jsonl,
    canonical_sha256,
    sha256_bytes,
    strict_json_loads,
    verify_digest,
)
from .capabilities import build_accepted_event, negotiate_capabilities
from .conformance import (
    ConformanceReport,
    validate_conformance,
    verify_bundle_manifest,
    verify_merged_transcript,
)
from .contracts import (
    CANONICAL_EXTERNAL_BUNDLE_SCHEMA,
    CANONICAL_EXTERNAL_RESULT_SCHEMA,
    CANONICAL_EXTERNAL_SCHEMAS,
    EXTERNAL_CONTRACT_NOTE,
    PRIVATE_EVAL_SCHEMAS,
    require_canonical_external_schema,
)
from .errors import (
    CanonicalizationError,
    CapabilityNegotiationError,
    IntegrityError,
    ProtocolAuthorityError,
    ProtocolDecodeError,
    ProtocolError,
    ProtocolLimitError,
    ProtocolStateError,
    SchemaLoadError,
    SchemaValidationError,
)
from .jsonl import (
    JsonlProtocolParser,
    MergedTranscriptVerifier,
    ProtocolLimits,
    decode_json_object_line,
    verify_artifact_event,
)
from .schemas import SchemaKind, SchemaStore, default_schema_store
from .session import HARNESS_EVENT_SCHEMA, ProtocolSession, SessionState
from .types import (
    EventType,
    HarnessStatus,
    NegotiatedProtocol,
    ProtocolTranscript,
    TrialStatus,
)

__all__ = [
    "CANONICALIZATION_ID",
    "CANONICAL_EXTERNAL_BUNDLE_SCHEMA",
    "CANONICAL_EXTERNAL_RESULT_SCHEMA",
    "CANONICAL_EXTERNAL_SCHEMAS",
    "CanonicalizationError",
    "CapabilityNegotiationError",
    "ConformanceReport",
    "EventType",
    "EXTERNAL_CONTRACT_NOTE",
    "HARNESS_EVENT_SCHEMA",
    "HarnessStatus",
    "IntegrityError",
    "JsonlProtocolParser",
    "MergedTranscriptVerifier",
    "NegotiatedProtocol",
    "PRIVATE_EVAL_SCHEMAS",
    "ProtocolAuthorityError",
    "ProtocolDecodeError",
    "ProtocolError",
    "ProtocolLimitError",
    "ProtocolLimits",
    "ProtocolSession",
    "ProtocolStateError",
    "ProtocolTranscript",
    "SchemaKind",
    "SchemaLoadError",
    "SchemaStore",
    "SchemaValidationError",
    "SessionState",
    "TrialStatus",
    "build_accepted_event",
    "canonical_digest",
    "canonical_json_bytes",
    "canonical_json_text",
    "canonical_jsonl",
    "canonical_sha256",
    "default_schema_store",
    "decode_json_object_line",
    "negotiate_capabilities",
    "require_canonical_external_schema",
    "sha256_bytes",
    "strict_json_loads",
    "validate_conformance",
    "verify_artifact_event",
    "verify_bundle_manifest",
    "verify_digest",
    "verify_merged_transcript",
]
