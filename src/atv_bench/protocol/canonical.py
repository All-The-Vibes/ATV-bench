"""Canonical JSON and SHA-256 helpers for protocol identity.

ATV canonical JSON v1 deliberately permits only the portable JSON data model used by
the v1 schemas: objects with string keys, arrays, strings, booleans, null, and IEEE-754
safe integers. Floating-point values are rejected; protocol quantities use explicit
integer units such as milliseconds, bytes, tokens, and micro-US dollars.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .errors import CanonicalizationError, IntegrityError, ProtocolDecodeError

CANONICALIZATION_ID = "atv.canonical-json/v1"
MAX_SAFE_INTEGER = 9_007_199_254_740_991


def _validate_json_value(value: Any, *, path: str = "$", depth: int = 0) -> None:
    if depth > 128:
        raise CanonicalizationError(f"canonical value exceeds depth limit at {path}")
    if value is None or isinstance(value, bool):
        return
    if isinstance(value, int):
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise CanonicalizationError(
                f"integer outside portable safe range at {path}: {value}"
            )
        return
    if isinstance(value, float):
        raise CanonicalizationError(
            f"floating-point values are not permitted by {CANONICALIZATION_ID} at {path}"
        )
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise CanonicalizationError(
                f"string is not strict UTF-8 at {path}: {exc}"
            ) from exc
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(
                    f"object key is not a string at {path}: {type(key).__name__}"
                )
            try:
                key.encode("ascii", errors="strict")
            except UnicodeEncodeError as exc:
                raise CanonicalizationError(
                    f"object key is not ASCII at {path}: {key!r}"
                ) from exc
            _validate_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    raise CanonicalizationError(
        f"unsupported canonical JSON type at {path}: {type(value).__name__}"
    )


def canonical_json_text(value: Any) -> str:
    """Return deterministic, whitespace-free ATV canonical JSON v1."""
    _validate_json_value(value)
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_text(value).encode("utf-8", errors="strict")


def canonical_jsonl(events: Iterable[Mapping[str, Any]]) -> bytes:
    """Serialize canonical events as UTF-8 JSON Lines with a final LF."""
    return b"".join(canonical_json_bytes(dict(event)) + b"\n" for event in events)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def canonical_digest(value: Any) -> dict[str, str]:
    return {"algorithm": "sha256", "value": canonical_sha256(value)}


def verify_digest(data: bytes, digest: Mapping[str, str]) -> None:
    if digest.get("algorithm") != "sha256":
        raise IntegrityError("only sha256 digests are supported")
    expected = digest.get("value")
    actual = sha256_bytes(data)
    if expected != actual:
        raise IntegrityError(
            f"sha256 mismatch: expected {expected!r}, observed {actual!r}"
        )


def strict_json_loads(text: str) -> Any:
    """Parse JSON while rejecting duplicate keys and non-standard constants."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ProtocolDecodeError(f"duplicate JSON object key: {key!r}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ProtocolDecodeError(f"non-standard JSON number is forbidden: {value}")

    def reject_float(value: str) -> None:
        raise ProtocolDecodeError(
            f"floating-point JSON numbers are forbidden by {CANONICALIZATION_ID}: {value}"
        )

    try:
        value = json.loads(
            text,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
            parse_float=reject_float,
        )
        _validate_json_value(value)
        return value
    except ProtocolDecodeError:
        raise
    except CanonicalizationError as exc:
        raise ProtocolDecodeError(exc.message) from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ProtocolDecodeError(f"malformed JSON: {exc}") from exc
