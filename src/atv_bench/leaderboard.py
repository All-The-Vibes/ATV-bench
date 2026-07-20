"""Leaderboard JSON contract (design-review T1).

Locked, versioned schema the Action writes and the static viewer validates on load.
`build_leaderboard_doc` turns match history + submission metadata into the published
document; `validate_leaderboard` enforces the schema (also enforced in the viewer).
"""
from __future__ import annotations

import math
from typing import Any

from atv_bench.elo import (
    ANCHOR_IDENTITY,
    MatchResult,
    MAX_CI_WIDTH,
    MIN_RATED_MATCHES,
    compute_leaderboard,
)
from atv_bench.fingerprint.scan import is_safe_name, is_secret

SCHEMA_VERSION = 1
# Low-confidence gate — reuses elo's canonical variance-gate thresholds (single source)
# so the board's demotion uses the SAME numeric teeth as elo.variance_gate, not a
# separate set of numbers. A rated row with fewer than MIN_RATED_MATCHES matches, or a
# CI wider than the ELO-CI ceiling, is marked low-confidence and demoted.
_LOW_CONFIDENCE_MATCHES = MIN_RATED_MATCHES
# Per-row CI ceiling: the ELO confidence interval (points) tolerated for a stable row.
# This is a FULL width (hi - lo), the SAME quantity elo.variance_gate() thresholds, so
# both gates MUST share the SAME number. variance_gate demotes a pair at MAX_CI_WIDTH;
# the row gate below must demote at the same MAX_CI_WIDTH (not *2), or an n=10-12 window
# opens where variance_gate calls a pair 'ci_too_wide' yet its row still publishes as
# stable. hi-lo == 2*_ci_width(n), and variance_gate compares 2*_ci_width(n) too, so the
# scales already match — no doubling belongs here.
_MAX_PUBLISH_CI_WIDTH = MAX_CI_WIDTH

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
        # Integrity gate (Section 6): when present and false, the viewer refuses to render
        # any rank and shows the integrity-gate reframe instead. Omitted -> verified board.
        "verified": {"type": "boolean"},
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
                    # Section 5.5 harness LIFT over the bare model, with its CI, plus the
                    # secondary per-bundle theta. Optional: only a VERIFIED board with a
                    # bare baseline present carries them; a baseline-less row omits them.
                    "lift": {"type": "number"},
                    "lift_ci": {
                        "type": "object",
                        "required": ["lo", "hi"],
                        "additionalProperties": False,
                        "properties": {"lo": {"type": "number"}, "hi": {"type": "number"}},
                    },
                    "theta": {"type": "number"},
                    # Section 4 BudgetVector — tokens / tool_calls / wall_time_s. Each field
                    # is nullable (never fabricated when an adapter did not measure it).
                    "budget": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "tokens": {"type": ["integer", "null"]},
                            "tool_calls": {"type": ["integer", "null"]},
                            "wall_time_s": {"type": ["number", "null"]},
                        },
                    },
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
                            "nested_skills": {"type": "array", "items": {"type": "string"}},
                            "tools": {"type": "array", "items": {"type": "string"}},
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


def _summary_from_details(fp: dict[str, Any], details: dict[str, Any]) -> str:
    """Build the summary from SANITIZED details, not the raw fingerprint (H2, santa round-3).

    Counting raw list lengths published a non-zero '3 skills' even when every entry was
    scrubbed (details empty), leaking that scrubbed entries existed and mismatching the row.
    Count only the entries that survived sanitization.
    """
    bits = []
    if fp.get("gstack"):
        bits.append("gstack")
    n_sk = len(details["skills"])
    n_nested = len(details.get("nested_skills", []))
    n_mcp, n_pl = len(details["mcps"]), len(details["plugins"])
    # A repo-harness's substance can be entirely in nested (plugin-provided) skills, so
    # count them toward the visible skill total rather than showing a misleading "0 skills".
    total_skills = n_sk + n_nested
    bits.append(f"{total_skills} skills")
    if n_mcp:
        bits.append(f"{n_mcp} MCP")
    if n_pl:
        bits.append(f"{n_pl} plugins")
    return " · ".join(bits)


def _sanitized_unknown_entry(entry: Any) -> dict[str, str]:
    """Re-validate a single unknown[] entry (H1, santa round-3).

    The unknown[] array is hand-editable in a merged record and was copied verbatim, so a
    secret-shaped `field` (or an out-of-enum `reason`) leaked onto the public board. Redact
    an unsafe field and constrain the reason to the locked schema enum.
    """
    field = entry.get("field") if isinstance(entry, dict) else None
    reason = entry.get("reason") if isinstance(entry, dict) else None
    if not isinstance(field, str) or not is_safe_name(field):
        # is_safe_name rejects secret-shaped, high-entropy, and non-slug values.
        field = "redacted"
    if reason not in _UNKNOWN_REASONS:
        reason = "name_failed_safety_scan"
    return {"field": field, "reason": reason}


def _is_safe_fingerprint_name(field: str, name: str) -> bool:
    """Leak-safety check for one fingerprint name.

    Most fields are plain slugs (is_safe_name). `nested_skills` legitimately carry a
    `plugin:skill` form (Section 4 schema-v2), so each colon-separated segment is scanned
    with the SAME allowlist scanner — the whole name is safe iff every segment is a safe
    slug (1 or 2 segments only). This admits `gstack:land` without weakening the scanner
    for any other field.
    """
    if field == "nested_skills" and ":" in name:
        parts = name.split(":")
        return len(parts) == 2 and all(is_safe_name(p) for p in parts)
    return is_safe_name(name)


def _sanitized_details(fp: dict[str, Any]) -> dict[str, Any]:
    """Re-validate a merged fingerprint for leak-safety on the trusted publish path (F2).

    The probe is leak-safe at emit time, but `submission.json` is a plain committed file a
    contributor can hand-edit. EVERY value that would enter a published row — skills, mcps,
    plugins, AND the pre-existing unknown[] entries — is re-scanned with the SAME allowlist
    scanner the probe uses. A failing value is dropped/redacted and recorded in
    unknown[{field, reason:"name_failed_safety_scan"}] — never published, never crashes.
    """
    # Sanitize the incoming unknown[] first (H1): its field/reason are hand-editable.
    unknown: list[dict[str, str]] = [
        _sanitized_unknown_entry(e) for e in (fp.get("unknown", []) or [])
    ]
    clean: dict[str, list[str]] = {}
    for field in ("skills", "nested_skills", "tools", "mcps", "plugins"):
        raw = fp.get(field, [])
        kept: list[str] = []
        if isinstance(raw, list):
            for name in raw:
                if isinstance(name, str) and _is_safe_fingerprint_name(field, name):
                    kept.append(name)
                else:
                    unknown.append({"field": field, "reason": "name_failed_safety_scan"})
        else:
            # G1 (santa round-2): a non-list value (e.g. a string) must be rejected
            # WHOLESALE, never iterated — iterating a string scans it character by
            # character and leaks the safe chars of a secret onto the board.
            unknown.append({"field": field, "reason": "name_failed_safety_scan"})
        clean[field] = kept
    clean["unknown"] = unknown
    return clean


def _safe_str_field(fp: dict[str, Any], key: str, default: str = "unknown") -> str:
    """A scalar string row field re-scanned for leak-safety (harness, probe_version).

    Any non-string or secret-shaped value collapses to `default` rather than publishing.
    """
    value = fp.get(key, default)
    if not isinstance(value, str) or is_secret(value):
        return default
    return value


def _safe_harness_name(fp: dict[str, Any]) -> str:
    """Harness is copied to a top-level row field; a secret-shaped value must not publish."""
    return _safe_str_field(fp, "harness", "unknown")


def build_leaderboard_doc(
    matches: list[MatchResult],
    submissions: dict[str, dict[str, Any]],
    *,
    updated_at: str,
    verified: bool | None = None,
    lifts: dict[str, Any] | None = None,
    budgets: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the published leaderboard document.

    `submissions[name]` carries: fingerprint (probe manifest), identity (GitHub
    login), bot_sha256, pr_url, logs_url. ELO comes from `matches`.

    `lifts` (Section 5.5): optional {harness_name -> LiftResult} from `lift.compute_lift`;
    when a row's harness has a lift result, its LIFT (over the bare model) + CI + secondary
    theta become part of the row. `budgets` (Section 4): optional {identity -> BudgetVector}
    from the match records; the row surfaces tokens / tool_calls / wall_time_s. Both are
    honest-optional — a row without lift/budget data simply omits those keys (the viewer
    renders only what is present), so an unverified/baseline-less board is unchanged.
    """
    board = compute_leaderboard(matches, entrants=list(submissions),
                                anchors=[ANCHOR_IDENTITY])

    def _low_conf(n: str) -> bool:
        b = board[n]
        if not b["rated"]:
            return False
        # Variance-gate teeth (wired from elo._MAX_CI_WIDTH, not just a match count):
        # a rated row whose confidence interval is wider than the publishable maximum,
        # OR that has too few matches, carries insufficient signal -> low confidence.
        ci_width = b["ci"]["hi"] - b["ci"]["lo"]
        if ci_width > _MAX_PUBLISH_CI_WIDTH:
            return True
        return 0 < b["match_count"] < _LOW_CONFIDENCE_MATCHES

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
        details = _sanitized_details(fp)
        row: dict[str, Any] = {
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
            "harness_name": _safe_harness_name(fp),
            "fingerprint_summary": _summary_from_details(fp, details),
            "details": details,
            "bot_sha256": sub["bot_sha256"],
            "fingerprint_probe_version": _safe_str_field(fp, "probe_version", "unknown"),
            "pr_url": sub["pr_url"],
            "logs_url": sub["logs_url"],
        }
        _attach_lift(row, lifts, row["harness_name"], fp)
        _attach_budget(row, budgets, sub["identity"], row["harness_name"])
        rows.append(row)
    doc: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "updated_at": updated_at, "rows": rows}
    if verified is not None:
        doc["verified"] = verified
    return doc


def _attach_lift(
    row: dict[str, Any], lifts: dict[str, Any] | None, harness: str, fp: dict[str, Any]
) -> None:
    """Attach Section-5.5 LIFT (over the bare model) + CI + secondary theta to a row.

    Sourced from `lift.compute_lift` (a {harness -> LiftResult}); the row is keyed by its
    published harness name. Only finite values publish (a NaN/inf lift is dropped rather than
    bricking the viewer's number formatting). theta may come from the LiftResult or the probe
    manifest — whichever the caller threaded — and is always the SECONDARY metric.
    """
    if not lifts:
        return
    res = lifts.get(harness)
    if res is None:
        return
    lift = getattr(res, "lift", None)
    lo = getattr(res, "lo", None)
    hi = getattr(res, "hi", None)
    if not (isinstance(lift, (int, float)) and math.isfinite(lift)):
        return
    row["lift"] = float(lift)
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and math.isfinite(lo) and math.isfinite(hi):
        row["lift_ci"] = {"lo": float(lo), "hi": float(hi)}
    theta = getattr(res, "theta", None)
    if theta is None:
        theta = fp.get("theta")
    if isinstance(theta, (int, float)) and math.isfinite(theta):
        row["theta"] = float(theta)


def _attach_budget(
    row: dict[str, Any], budgets: dict[str, Any] | None, identity: str, harness: str
) -> None:
    """Attach a Section-4 BudgetVector (tokens / tool_calls / wall_time_s) to a row.

    Keyed by identity first (a per-entrant budget), then harness. Values are copied as-is
    (nullable — never fabricated). A malformed/absent budget simply omits the key.
    """
    if not budgets:
        return
    bv = budgets.get(identity)
    if bv is None:
        bv = budgets.get(harness)
    if bv is None:
        return
    tokens = getattr(bv, "tokens", None) if not isinstance(bv, dict) else bv.get("tokens")
    tool_calls = getattr(bv, "tool_calls", None) if not isinstance(bv, dict) else bv.get("tool_calls")
    wall = getattr(bv, "wall_time_s", None) if not isinstance(bv, dict) else bv.get("wall_time_s")
    budget: dict[str, Any] = {}
    if isinstance(tokens, int):
        budget["tokens"] = tokens
    if isinstance(tool_calls, int):
        budget["tool_calls"] = tool_calls
    if isinstance(wall, (int, float)) and math.isfinite(wall):
        budget["wall_time_s"] = float(wall)
    if budget:
        row["budget"] = budget


def build_insights(rows: list[dict[str, Any]]) -> list[str]:
    """Derive short, human-readable insight lines from ranked board rows (demo Act 3).

    Pure heuristic — no I/O. Ties fingerprint traits to ranking the way the gstack plan
    frames it ("we rank the harness, not the model"). Always returns at least one line;
    never raises on empty or partial rows.
    """
    if not rows:
        return ["No matches yet — submit two harnesses to populate the board."]

    def _elo(r: dict[str, Any]) -> float:
        try:
            val = float(r.get("elo", 0.0))
        except (TypeError, ValueError):
            return 0.0
        # Corrupted/degenerate ratings (NaN, +/-inf) would crash round() downstream
        # ("cannot convert float NaN to integer") and brick the whole board display.
        # One bad row must not take out Act 3 — treat a non-finite rating as 0.0.
        if not math.isfinite(val):
            return 0.0
        return val

    ranked = sorted(rows, key=lambda r: r.get("rank", 10**9))
    out: list[str] = []

    leader = ranked[0]
    out.append(
        f"#1 @{leader.get('identity', '?')} ({leader.get('harness_name', 'harness')}) "
        f"leads at {round(_elo(leader))} ELO."
    )

    # gstack vs non-gstack cohort ELO — the plan's core thesis.
    gstack = [r for r in rows if r.get("fingerprint_gstack")]
    non = [r for r in rows if not r.get("fingerprint_gstack")]
    if gstack and non:
        g_avg = sum(_elo(r) for r in gstack) / len(gstack)
        n_avg = sum(_elo(r) for r in non) / len(non)
        delta = round(g_avg - n_avg)
        if delta > 0:
            out.append(
                f"gstack harnesses average +{delta} ELO over non-gstack "
                f"({len(gstack)} vs {len(non)} entrants)."
            )
        elif delta < 0:
            out.append(
                f"non-gstack harnesses lead gstack by {abs(delta)} ELO so far "
                f"({len(non)} vs {len(gstack)} entrants) — small sample."
            )
        else:
            out.append("gstack and non-gstack harnesses are dead even so far.")

    # Tooling depth of the leader.
    details = leader.get("details") or {}
    n_skills = len(details.get("skills") or [])
    n_mcps = len(details.get("mcps") or [])
    n_plugins = len(details.get("plugins") or [])
    if n_skills or n_mcps or n_plugins:
        out.append(
            f"The leader runs {n_skills} skills, {n_mcps} MCP servers, "
            f"and {n_plugins} plugins."
        )

    return out
