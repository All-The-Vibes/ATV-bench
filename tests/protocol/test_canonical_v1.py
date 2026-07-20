from __future__ import annotations

import pytest

from atv_bench.protocol import (
    CanonicalizationError,
    IntegrityError,
    ProtocolDecodeError,
    canonical_digest,
    canonical_json_bytes,
    canonical_json_text,
    canonical_sha256,
    strict_json_loads,
    verify_digest,
)


def test_canonical_json_golden_vector_is_key_order_independent_and_utf8():
    left = {"z": "café", "a": [True, None, 7]}
    right = {"a": [True, None, 7], "z": "café"}
    expected = '{"a":[true,null,7],"z":"café"}'
    assert canonical_json_text(left) == expected
    assert canonical_json_bytes(left) == expected.encode("utf-8")
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_sha256(left) == canonical_sha256(right)


@pytest.mark.parametrize(
    "value",
    [
        {"float": 1.5},
        {"too_large": 9_007_199_254_740_992},
        {1: "non-string-key"},
        {"surrogate": "\ud800"},
        {"naïve-key": "value"},
    ],
)
def test_canonical_json_rejects_nonportable_values(value):
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(value)


@pytest.mark.parametrize(
    "text",
    [
        '{"x":1,"x":2}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1.5}',
        '{"naïve-key":1}',
        '{"x":"\\ud800"}',
    ],
)
def test_strict_json_rejects_ambiguous_or_nonportable_input(text):
    with pytest.raises(ProtocolDecodeError):
        strict_json_loads(text)


def test_digest_round_trip_and_one_byte_tamper_detection():
    value = {"schema": "example/v1", "count": 1}
    data = canonical_json_bytes(value)
    digest = canonical_digest(value)
    verify_digest(data, digest)
    with pytest.raises(IntegrityError):
        verify_digest(data + b"x", digest)


def test_array_order_is_hash_significant():
    assert canonical_sha256([1, 2]) != canonical_sha256([2, 1])
