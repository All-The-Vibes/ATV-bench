"""Per-value secret scanner (eng T2).

Every string that would enter a published fingerprint manifest passes through
`is_secret()` first. If it matches a known secret shape OR scores as high-entropy,
it is rejected and the field becomes `unknown[{field, reason}]` upstream.

This is a REJECT filter, not a redactor: we never try to scrub a secret out of a
value and keep the rest. A value is either provably clean (a short, low-entropy,
pattern-free name) or it does not enter the manifest at all.
"""
from __future__ import annotations

import math
import re
import unicodedata

# High-confidence secret token shapes. Ordered by specificity; any match => secret.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),                      # GitHub PAT
    re.compile(r"gho_[A-Za-z0-9]{20,}"),                      # GitHub OAuth
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),              # GitHub fine-grained PAT
    re.compile(r"sk-[A-Za-z0-9-]{16,}"),                      # OpenAI / Anthropic style
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"),               # Slack
    re.compile(r"AKIA[0-9A-Z]{16}"),                          # AWS access key id
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),                   # Google API key
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),          # bearer token / JWT
    re.compile(r"eyJ[A-Za-z0-9._\-]{10,}"),                   # raw JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),        # PEM private key
    re.compile(r"[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]+@"),  # URL/DSN with credentials
)

# Characters that have no business in a skill/MCP/plugin NAME. Presence => reject
# (covers injection payloads: ://, @, ANSI escapes, HTML, control chars, whitespace).
_UNSAFE_NAME = re.compile(r"[\s:@/\\<>\"'`\x00-\x1f\x7f]|://")

# A safe name is a slug: it must match this ALLOWLIST end-to-end (after NFKC
# normalization). Allowlist-by-construction beats denylist: anything not matching
# this exact shape is rejected, so we don't play whack-a-mole with Unicode tricks,
# zero-width chars, homoglyphs, or novel injection payloads.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Names longer than this are suspicious as a "name" and likely encode data.
_MAX_NAME_LEN = 64
# Shannon-entropy threshold (bits/char) above which a token is treated as a secret.
# Real slugs ("compound-engineering") sit below; random blobs sit above. Applied
# from a short length now (red-team found short secrets bypassing a len-20 gate).
_ENTROPY_BITS = 3.5
_ENTROPY_MIN_LEN = 12


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# Separator characters that break a slug into segments. A legitimate name is short
# tokens joined by these ("systematic-debugging", "claude-opus-4.8"); a secret blob is
# one long dense run ("aB3xK9mQ2pL5nR8w"). The entropy gate scores the longest SEGMENT,
# not the whole string, so a long hyphenated slug isn't mistaken for a high-entropy token.
_NAME_SEPARATORS = re.compile(r"[-_.]+")


def _longest_segment(value: str) -> str:
    """Longest separator-delimited run in `value` (the whole string if no separators)."""
    segments = [s for s in _NAME_SEPARATORS.split(value) if s]
    return max(segments, key=len) if segments else value


def _normalize(value: str) -> str:
    """NFKC-normalize and strip zero-width / formatting characters.

    Unicode zero-width joiners (U+200D), zero-width space, BOM, and other Cf
    (format) code points can encode data invisibly and defeat pattern/entropy
    checks. We remove them before any decision so an invisible char can neither
    hide a secret nor survive into the manifest.
    """
    nfkc = unicodedata.normalize("NFKC", value)
    return "".join(ch for ch in nfkc if unicodedata.category(ch) != "Cf")


# Secret-signal keywords. A NAME containing any of these is rejected even if it is
# a legal slug with low entropy — a short slug like "db-password-prod" is
# indistinguishable from a real skill name by shape alone, so we treat the presence
# of a credential word as disqualifying. False positives (a skill honestly named
# "secret-santa") land in unknown[], which is safe, not leaky.
_SECRET_KEYWORDS = (
    "password", "passwd", "secret", "token", "apikey", "api-key", "api_key",
    "credential", "private-key", "privatekey", "access-key", "accesskey",
    "auth-token", "bearer", "session-key",
    # round-2: shorter credential stems (a name containing these is suspicious)
    "passphrase", "-pass", "pass-", "_pass", "pass_", "-pwd", "pwd-", "passwd",
)


def _has_secret_keyword(value: str) -> bool:
    low = value.lower()
    return any(kw in low for kw in _SECRET_KEYWORDS)


# Credential token PREFIXES. A name that STARTS with one of these is rejected even
# if it is short and low-entropy — no legitimate skill/MCP/plugin is named `sk-…`,
# `ghp…`, `xox…`, or `AKIA…`. Closes the round-2 "short token prefix" evasion where
# `sk-proj-exposed` (15 chars, entropy 3.24) slipped under the entropy gate.
_CREDENTIAL_PREFIXES = ("sk-", "sk_", "ghp", "gho", "ghs", "ghu", "github_pat",
                        "xox", "akia", "aiza", "asia")


def _has_credential_prefix(value: str) -> bool:
    low = value.lower()
    return any(low.startswith(p) for p in _CREDENTIAL_PREFIXES)


# Common weak secrets / default credentials. Short, low-entropy, keyword-free, so the
# shape heuristics miss them — but they are the MOST predictable secrets a config could
# carry. An exact-match (case-insensitive) denylist rejects them as defense-in-depth.
# Not exhaustive: arbitrary user-chosen names remain a consent-surface boundary.
_COMMON_WEAK_SECRETS = frozenset({
    "hunter2", "admin", "root", "letmein", "password1", "passw0rd", "s3cr3t",
    "changeme", "default", "guest", "test123", "qwerty", "abc123", "welcome",
    "administrator", "toor", "pass", "secret1", "login", "master",
})


def _is_common_weak_secret(value: str) -> bool:
    low = value.lower()
    if low in _COMMON_WEAK_SECRETS:
        return True
    # round-2: a common weak secret with a short suffix (hunter2x, hunter2024,
    # admin123). Treat any value that STARTS with a known weak secret as suspicious.
    if any(low.startswith(w) for w in _COMMON_WEAK_SECRETS):
        return True
    # or whose alpha-only core is a known weak secret (adminX -> admin)
    stripped = re.sub(r"[^a-z]", "", low)
    return stripped in _COMMON_WEAK_SECRETS


def _is_all_digits(value: str) -> bool:
    # round-2: a long all-digit string (123456789012) is not a real skill name and is
    # a common secret/PIN shape. Short numeric versions (v2, 4) stay allowed.
    return len(value) >= 6 and value.isdigit()


def is_secret(value: str) -> bool:
    """True if `value` must NOT enter a published manifest.

    Rejects: known token shapes, credential prefixes, common weak secrets,
    credentials-in-URL, PEM blocks, high-entropy strings, and names carrying
    credential keywords. Deliberately conservative — a false positive costs one
    dropped field; a false negative costs a leaked secret on a public leaderboard.
    """
    if not isinstance(value, str):
        return True
    norm = _normalize(value)
    # if normalization changed the string, invisible/compat chars were present:
    # treat as suspicious rather than reasoning about the cleaned form.
    if norm != value:
        return True
    if _is_common_weak_secret(value):
        return True
    if _is_all_digits(value):
        return True
    if _has_secret_keyword(value):
        return True
    if _has_credential_prefix(value):
        return True
    for pat in _SECRET_PATTERNS:
        if pat.search(value):
            return True
    # Entropy gate: score the longest separator-delimited SEGMENT, not the whole string.
    # A hyphenated slug ("finishing-a-development-branch") has high whole-string entropy
    # but short low-entropy segments; a real random token is one long dense run. Credential
    # tokens with structure (sk-…, ghp_…, xox…) are already caught by the prefix/pattern
    # gates above, so scoring the segment here loses no secret-detection power.
    segment = _longest_segment(value)
    if len(segment) >= _ENTROPY_MIN_LEN and _shannon_entropy(segment) >= _ENTROPY_BITS:
        return True
    return False


def is_safe_name(value: str) -> bool:
    """True if `value` is safe to emit as a skill/MCP/plugin/model NAME.

    Allowlist-by-construction: the name must be a plain slug (after NFKC), free of
    unsafe characters, not a secret shape, and not high-entropy.
    """
    if not isinstance(value, str) or not value:
        return False
    if len(value) > _MAX_NAME_LEN:
        return False
    # reject anything that normalization would change (zero-width/homoglyph/compat)
    if _normalize(value) != value:
        return False
    if not _SAFE_NAME.match(value):
        return False
    if _UNSAFE_NAME.search(value):
        return False
    if is_secret(value):
        return False
    return True
