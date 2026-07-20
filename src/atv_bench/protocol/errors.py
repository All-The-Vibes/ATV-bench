"""Typed failures for the versioned ATV harness protocol."""
from __future__ import annotations

from typing import Any


class ProtocolError(ValueError):
    """Base class for fail-closed protocol and conformance errors."""

    code = "protocol_error"

    def __init__(
        self,
        message: str,
        *,
        line_number: int | None = None,
        event_index: int | None = None,
        path: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.line_number = line_number
        self.event_index = event_index
        self.path = path

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "line_number": self.line_number,
            "event_index": self.event_index,
            "path": self.path,
        }


class CanonicalizationError(ProtocolError):
    code = "canonicalization_error"


class SchemaLoadError(ProtocolError):
    code = "schema_load_error"


class SchemaValidationError(ProtocolError):
    code = "schema_validation_error"


class ProtocolDecodeError(ProtocolError):
    code = "protocol_decode_error"


class ProtocolLimitError(ProtocolError):
    code = "protocol_limit_error"


class ProtocolStateError(ProtocolError):
    code = "protocol_state_error"


class ProtocolAuthorityError(ProtocolError):
    code = "protocol_authority_error"


class CapabilityNegotiationError(ProtocolError):
    code = "capability_negotiation_error"


class IntegrityError(ProtocolError):
    code = "integrity_error"
