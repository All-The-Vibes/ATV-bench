"""Section 3 (ENG-E): adapter-LOG leak/secret scrubbing on the persistence path.

The captured bot TREE is already leak-scanned (capture.scan_captured_tree). But
`AdapterResult.log` — stdout/stderr tails from the harness CLI (contract.py populates it
from `proc.stdout[-2000:]` / `proc.stderr[-2000:]` / a combined tail) — is NOT scrubbed.
Those logs flow into the persisted match record / replay / leaderboard verbatim and can
carry a leaked token, a bearer header, a PEM key, a host path, or an echoed prompt.

Complements Section 2.5 (live-exfiltration prevention) by scrubbing data AT REST: the
single serialization choke point is `AdapterResult.to_dict()`/`to_json()`, the only place
`.log` reaches a persisted artifact. `scrub_log` MUST reuse capture.py's secret patterns
(`fingerprint.scan._has_secret_pattern`) so a token capture rejects is also redacted here.
"""
from __future__ import annotations

import json

import pytest

from atv_bench.adapters.contract import AdapterResult, AdapterStatus, Usage
from atv_bench.fingerprint.scan import _has_secret_pattern

# The API under test (RED until implemented + wired into the persistence seam).
from atv_bench.logscan import REDACTION, scrub_log


def _result(log: str) -> AdapterResult:
    return AdapterResult(
        status=AdapterStatus.EDITED,
        diff="--- a\n+++ b\n",
        log=log,
        usage=Usage(seconds=1.0, turns=1),
        model="claude-opus-4-8",
    )


# Concrete leaked-secret shapes, each matching a capture.py `_SECRET_PATTERNS` entry.
_SK_TOKEN = "sk-ABCDEF0123456789ABCDEF0123456789"
_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
_BEARER = "Bearer abcdef0123456789ABCDEF"
_GHP_TOKEN = "ghp_0123456789abcdefABCD0123456789abcdEF"


@pytest.mark.parametrize("token", [_SK_TOKEN, _AWS_KEY, _BEARER, _GHP_TOKEN])
def test_secret_redacted_before_persist(token):
    """A secret in AdapterResult.log must be REDACTED before it lands in the persisted
    record/replay. The persisted artifact (to_dict/to_json) must contain the redaction
    marker and NOT the token. RED now: nothing scrubs the log on the persist path."""
    log = f"harness stdout tail:\nAuthorization used {token} while editing bot\n"
    result = _result(log)

    persisted = result.to_dict()
    assert token not in persisted["log"], f"secret leaked into persisted record: {token}"
    assert REDACTION in persisted["log"], "expected a redaction marker in scrubbed log"

    # Same guarantee through the JSON serialization used to write the artifact to disk.
    blob = result.to_json()
    assert token not in blob, f"secret leaked into serialized JSON: {token}"
    assert token not in json.loads(blob)["log"]


def test_clean_log_unchanged():
    """No false positives: an ordinary log with no secrets passes through byte-for-byte
    (no mangling of normal stdout/stderr tails)."""
    clean = (
        "Edited main.py: added a flood-fill survival heuristic.\n"
        "3 files changed, 42 insertions(+), 1 deletion(-)\n"
        "done in 12.4s\n"
    )
    result = _result(clean)
    assert result.to_dict()["log"] == clean
    assert scrub_log(clean) == clean


def test_scan_reuses_capture_patterns():
    """The log scrubber must use the SAME secret patterns as capture.py — a token that
    capture's `_has_secret_pattern` flags is also redacted out of a log line. This binds
    the two scanners so they can't drift apart."""
    for token in (_SK_TOKEN, _AWS_KEY, _BEARER, _GHP_TOKEN):
        line = f"leaked {token} here"
        assert _has_secret_pattern(line), "precondition: capture flags this token"
        scrubbed = scrub_log(line)
        assert token not in scrubbed, f"capture-flagged token survived scrub: {token}"
        assert not _has_secret_pattern(scrubbed), "scrubbed line still trips capture scanner"


def test_pem_private_key_redacted():
    """A PEM private-key block (capture `_SECRET_PATTERNS` entry) must not persist."""
    log = "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END RSA PRIVATE KEY-----\n"
    persisted = _result(log).to_dict()
    assert "BEGIN RSA PRIVATE KEY" not in persisted["log"]
    assert REDACTION in persisted["log"]


def test_credentialed_url_redacted():
    """A DSN/URL carrying inline credentials (capture pattern) must be scrubbed."""
    log = "connecting to postgres://admin:s3cr3tP4ss@db.internal:5432/prod\n"
    scrubbed = scrub_log(log)
    assert "s3cr3tP4ss" not in scrubbed
    assert not _has_secret_pattern(scrubbed)


def test_multiline_only_secret_lines_redacted():
    """Redaction is line-scoped: only lines carrying a secret are marked; clean context
    lines around them survive so the log stays useful for debugging."""
    log = (
        "starting build\n"
        f"token={_SK_TOKEN}\n"
        "wrote main.py\n"
    )
    scrubbed = scrub_log(log)
    assert "starting build" in scrubbed
    assert "wrote main.py" in scrubbed
    assert _SK_TOKEN not in scrubbed
