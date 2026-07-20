"""Corpus integrity gates for the rating engine (plan Section 5).

A match record enters the rating corpus only if it is signed by the trusted referee (the
arena entrypoint that produces the honest scored outcome), and each replay is counted
ONCE (Sybil / duplicate-replay guard). This mirrors the trust boundary in publish.py /
match_record.py: an untrusted bot controls only its moves, never the scored record.

Signing is HMAC-SHA256 over a canonical serialization of the record fields (excluding the
signature itself). ``RefereeSigner.public_key`` is the verification handle; for the
symmetric HMAC scheme it carries the same secret (verification requires the shared key).
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Iterable

_SIG_FIELD = "referee_sig"


class CorpusError(Exception):
    """Raised when a record fails the corpus trust/dedup gate."""


def _canonical_bytes(record: dict[str, Any]) -> bytes:
    """Deterministic serialization of the record MINUS the signature field.

    Sorted keys + compact separators so the signer and verifier agree byte-for-byte
    regardless of dict insertion order.
    """
    payload = {k: v for k, v in record.items() if k != _SIG_FIELD}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


class RefereeSigner:
    """HMAC-SHA256 referee signer over match records."""

    def __init__(self, key: bytes):
        if not isinstance(key, (bytes, bytearray)):
            raise TypeError("referee key must be bytes")
        self._key = bytes(key)

    @property
    def public_key(self) -> bytes:
        """Verification handle. Symmetric HMAC => the shared secret."""
        return self._key

    def sign(self, record: dict[str, Any]) -> dict[str, Any]:
        """Return a new record dict with a ``referee_sig`` HMAC attached (no mutation)."""
        sig = hmac.new(self._key, _canonical_bytes(record), hashlib.sha256).hexdigest()
        return {**record, _SIG_FIELD: sig}


def _verify(record: dict[str, Any], verify_key: bytes) -> bool:
    sig = record.get(_SIG_FIELD)
    if not isinstance(sig, str):
        return False
    expected = hmac.new(verify_key, _canonical_bytes(record), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def ingest_corpus(
    records: Iterable[dict[str, Any]],
    *,
    verify_key: bytes,
    on_duplicate: str = "dedup",
) -> list[dict[str, Any]]:
    """Verify + dedup a stream of signed match records into the rating corpus.

    - Every record MUST carry a valid referee signature over the verify key, else
      ``CorpusError`` (a bot cannot inject a match the referee never scored).
    - Records are deduplicated by ``replay_hash`` — the replay bytes are the true match
      identity, so two records with the same hash (even under distinct match_ids) count
      once. ``on_duplicate="raise"`` turns a duplicate into a ``CorpusError``; the default
      ``"dedup"`` keeps the first and drops the rest silently.
    """
    if on_duplicate not in ("dedup", "raise"):
        raise ValueError("on_duplicate must be 'dedup' or 'raise'")

    accepted: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for rec in records:
        if not _verify(rec, verify_key):
            raise CorpusError(
                f"record {rec.get('match_id')!r} failed referee signature verification")
        rh = rec.get("replay_hash")
        if rh is None:
            raise CorpusError(f"record {rec.get('match_id')!r} has no replay_hash")
        if rh in seen_hashes:
            if on_duplicate == "raise":
                raise CorpusError(f"duplicate replay_hash {rh!r} (Sybil/replay guard)")
            continue
        seen_hashes.add(rh)
        # strip the signature from the corpus copy — it has served its purpose
        accepted.append({k: v for k, v in rec.items() if k != _SIG_FIELD})
    return accepted
