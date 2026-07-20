"""Section 3 (ENG-E): scrub secrets out of adapter LOG tails on the persistence path.

The captured bot tree is already leak-scanned, but `AdapterResult.log` — the stdout/stderr
tails contract.py collects from the harness CLI — is NOT. Those logs flow verbatim into the
persisted match record / replay / leaderboard and can carry a leaked token, bearer header,
PEM key, or credentialed URL.

`scrub_log` redacts secrets AT REST, wired into `AdapterResult.to_dict()` (the single
serialization choke point). It REUSES capture's secret patterns
(`fingerprint.scan._SECRET_PATTERNS`) so a token capture rejects is also redacted here and
the two scanners can never drift.
"""
from __future__ import annotations

from atv_bench.fingerprint.scan import _SECRET_PATTERNS

# Marker substituted in place of each matched secret run.
REDACTION = "[REDACTED]"


def _scrub_line(line: str) -> str:
    """Replace every secret-pattern match in `line` with REDACTION."""
    for pat in _SECRET_PATTERNS:
        line = pat.sub(REDACTION, line)
    return line


def scrub_log(text: str) -> str:
    """Redact secret substrings from `text`, line-scoped.

    Only substrings matching capture's `_SECRET_PATTERNS` are replaced with REDACTION;
    clean lines and clean context around a secret pass through byte-for-byte. Splitting
    keeps the redaction line-scoped so a secret on one line can't blank surrounding output.
    """
    if not text:
        return text
    return "\n".join(_scrub_line(line) for line in text.split("\n"))
