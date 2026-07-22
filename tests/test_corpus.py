"""Corpus integrity gates for the rating engine (plan Section 5).

The rating engine consumes a corpus of match records. A record only enters the corpus if
it is signed by the trusted referee (the arena entrypoint that produces the honest outcome),
and each replay may be counted ONCE (Sybil / duplicate-replay guard). These mirror the
existing trust boundary in publish.py / match_record.py: an untrusted bot controls only its
moves, never the scored record.

RED before src/atv_bench/corpus.py exists.
"""
from __future__ import annotations

import pytest

from atv_bench.corpus import (
    CorpusError,
    RefereeSigner,
    ingest_corpus,
)


def _record(match_id: str, replay_hash: str, *, harness_a="claude-code",
            harness_b="copilot-cli"):
    return {
        "match_id": match_id,
        "replay_hash": replay_hash,
        "harness_a": harness_a,
        "harness_b": harness_b,
        "model_a": "claude",
        "model_b": "gpt",
        "score_a": 1.0,
    }


def test_match_record_signed_by_referee():
    """A record signed with the referee's key is accepted and enters the corpus."""
    signer = RefereeSigner(key=b"trusted-referee-secret-key-0001")
    rec = _record("m1", "a" * 64)
    signed = signer.sign(rec)
    accepted = ingest_corpus([signed], verify_key=signer.public_key)
    assert len(accepted) == 1
    assert accepted[0]["match_id"] == "m1"


def test_unsigned_record_rejected():
    """An unsigned (or wrong-key-signed) record is rejected — a bot cannot inject a match the
    referee never scored."""
    signer = RefereeSigner(key=b"trusted-referee-secret-key-0001")
    forger = RefereeSigner(key=b"attacker-forged-key-999999999999")
    unsigned = _record("m2", "b" * 64)  # no signature at all
    with pytest.raises(CorpusError):
        ingest_corpus([unsigned], verify_key=signer.public_key)
    # signed by the WRONG key => also rejected
    forged = forger.sign(_record("m3", "c" * 64))
    with pytest.raises(CorpusError):
        ingest_corpus([forged], verify_key=signer.public_key)


def test_duplicate_replay_hash_rejected():
    """Two records with the SAME replay_hash count once (Sybil/dup guard): replaying an
    identical match to farm rating is refused. The engine dedups by replay_hash even across
    distinct match_ids, because the replay bytes are the true match identity."""
    signer = RefereeSigner(key=b"trusted-referee-secret-key-0001")
    dup_hash = "d" * 64
    r1 = signer.sign(_record("m4", dup_hash))
    r2 = signer.sign(_record("m5", dup_hash))  # different id, SAME replay bytes
    with pytest.raises(CorpusError):
        ingest_corpus([r1, r2], verify_key=signer.public_key, on_duplicate="raise")
    # default policy dedups silently to a single counted record
    kept = ingest_corpus([r1, r2], verify_key=signer.public_key)
    assert len(kept) == 1
