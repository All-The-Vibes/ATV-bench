"""Leaderboard JSON contract (design-review T1).

Locked, versioned schema the Action writes and the static viewer validates on load.
`build_leaderboard_doc` turns match history + submission metadata into the published
document; `validate_leaderboard` enforces the schema (also enforced in the viewer).
"""
from __future__ import annotations

from typing import Any

from atv_bench.elo import MatchResult, compute_leaderboard

SCHEMA_VERSION = 1
# rated rows with fewer than this many matches are shown but marked low-confidence
# and demoted below stable rows (matches the viewer's treatment).
_LOW_CONFIDENCE_MATCHES = 5

# forfeit/unknown reason enums must match the probe + elo modules exactly.
_UNKNOWN_REASONS = [
    "not_readable", "malformed", "empty", "permission_denied",
    "symlink_escape", "name_failed_safety_scan",
]

LEADERBOARD_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ATV-bench Community League leaderboard",
    "type": "object",
    "required": ["schema_version", "updated_at", "rows"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "updated_at": {
            "type": "string",
            # ISO-8601 UTC (Z)
            "pattern": r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$",
        },
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "rank", "elo", "rated", "match_count", "ci", "identity",
                    "harness_name", "fingerprint_summary", "details", "bot_sha256",
                    "fingerprint_probe_version", "pr_url", "logs_url",
                    "low_confidence", "fingerprint_gstack",
                ],
                "properties": {
                    "rank": {"type": "integer", "minimum": 1},
                    "elo": {"type": "number"},
                    "rated": {"type": "boolean"},
                    "low_confidence": {"type": "boolean"},
                    "fingerprint_gstack": {"type": "boolean"},
                    "match_count": {"type": "integer", "minimum": 0},
                    "status": {"type": "string"},
                    "wins": {"type": "integer", "minimum": 0},
                    "losses": {"type": "integer", "minimum": 0},
                    "draws": {"type": "integer", "minimum": 0},
                    "forfeits": {"type": "integer", "minimum": 0},
                    "ci": {
                        "type": "object",
                        "required": ["lo", "hi"],
                        "additionalProperties": False,
                        "properties": {"lo": {"type": "number"}, "hi": {"type": "number"}},
                    },
                    "identity": {"type": "string", "minLength": 1},
                    "harness_name": {"type": "string"},
                    "fingerprint_summary": {"type": "string"},
                    "details": {
                        "type": "object",
                        "required": ["skills", "mcps", "plugins", "unknown"],
                        "additionalProperties": False,
                        "properties": {
                            "skills": {"type": "array", "items": {"type": "string"}},
                            "mcps": {"type": "array", "items": {"type": "string"}},
                            "plugins": {"type": "array", "items": {"type": "string"}},
                            "unknown": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["field", "reason"],
                                    "additionalProperties": False,
                                    "properties": {
                                        "field": {"type": "string"},
                                        "reason": {"type": "string", "enum": _UNKNOWN_REASONS},
                                    },
                                },
                            },
                        },
                    },
                    "bot_sha256": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
                    "fingerprint_probe_version": {"type": "string"},
                    # http(s) only — a javascript:/data: URL here is a stored-XSS-on-
                    # click vector in the viewer. Enforced by pattern (jsonschema does
                    # not enforce `format` without a format_checker), on BOTH urls.
                    "pr_url": {"type": "string", "pattern": r"^https?://"},
                    "logs_url": {"type": "string", "pattern": r"^https?://"},
                },
            },
        },
    },
}


def validate_leaderboard(doc: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if `doc` violates the locked contract."""
    import jsonschema
    jsonschema.validate(doc, LEADERBOARD_SCHEMA)


def _summary(fp: dict[str, Any]) -> str:
    bits = []
    if fp.get("gstack"):
        bits.append("gstack")
    n_sk, n_mcp, n_pl = len(fp.get("skills", [])), len(fp.get("mcps", [])), len(fp.get("plugins", []))
    bits.append(f"{n_sk} skills")
    if n_mcp:
        bits.append(f"{n_mcp} MCP")
    if n_pl:
        bits.append(f"{n_pl} plugins")
    return " · ".join(bits)


def build_leaderboard_doc(
    matches: list[MatchResult],
    submissions: dict[str, dict[str, Any]],
    *,
    updated_at: str,
) -> dict[str, Any]:
    """Compose the published leaderboard document.

    `submissions[name]` carries: fingerprint (probe manifest), identity (GitHub
    login), bot_sha256, pr_url, logs_url. ELO comes from `matches`.
    """
    board = compute_leaderboard(matches, entrants=list(submissions))

    def _low_conf(n: str) -> bool:
        b = board[n]
        return bool(b["rated"] and 0 < b["match_count"] < _LOW_CONFIDENCE_MATCHES)

    # Rank tiers (must match the viewer's display order so JSON rank == visual row):
    #   1. stable rated rows (>= _LOW_CONFIDENCE_MATCHES matches)
    #   2. low-confidence rated rows (demoted)
    #   3. unrated / provisional rows (no matches yet)
    # Within a tier: highest ELO first, then identity for a stable tie-break.
    def _tier(n: str) -> int:
        b = board[n]
        if not b["rated"]:
            return 2
        return 1 if _low_conf(n) else 0

    ordered = sorted(
        submissions,
        key=lambda n: (_tier(n), -board[n]["elo"], submissions[n]["identity"]),
    )
    rows: list[dict[str, Any]] = []
    for rank, name in enumerate(ordered, start=1):
        b = board[name]
        sub = submissions[name]
        fp = sub["fingerprint"]
        low_confidence = _low_conf(name)
        rows.append({
            "rank": rank,
            "elo": b["elo"],
            "rated": b["rated"],
            "low_confidence": low_confidence,
            "fingerprint_gstack": bool(fp.get("gstack", False)),
            "match_count": b["match_count"],
            "status": b["status"],
            "wins": b["wins"],
            "losses": b["losses"],
            "draws": b["draws"],
            "forfeits": b["forfeits"],
            "ci": b["ci"],
            "identity": sub["identity"],
            "harness_name": fp.get("harness", "unknown"),
            "fingerprint_summary": _summary(fp),
            "details": {
                "skills": fp.get("skills", []),
                "mcps": fp.get("mcps", []),
                "plugins": fp.get("plugins", []),
                "unknown": fp.get("unknown", []),
            },
            "bot_sha256": sub["bot_sha256"],
            "fingerprint_probe_version": fp.get("probe_version", "unknown"),
            "pr_url": sub["pr_url"],
            "logs_url": sub["logs_url"],
        })
    return {"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "rows": rows}
