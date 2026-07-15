"""Leaderboard JSON contract tests (master test plan: 'Leaderboard JSON contract').

The Action writes this JSON; the static viewer validates it on load. Locking the
schema here means the UI can never be asked to render a field the emitter forgot,
and a schema drift breaks CI instead of shipping a broken board.

Design: design-review T1, docs/COMMUNITY_LEAGUE.md 'Leaderboard JSON contract'.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from atv_bench.leaderboard import (
    LEADERBOARD_SCHEMA,
    build_leaderboard_doc,
    validate_leaderboard,
)
from atv_bench.elo import MatchResult, Outcome


SCHEMA_PATH = Path(__file__).parent.parent / "leaderboard" / "schema.json"


def _fingerprint(harness="claude-code"):
    return {
        "harness": harness,
        "model": "claude-opus-4-8",
        "gstack": True,
        "skills": ["gstack", "office-hours"],
        "mcps": ["github"],
        "plugins": ["compound-engineering"],
        "custom_agents_count": 7,
        "unknown": [],
        "probe_version": "1.0.0",
    }


def test_schema_file_exists_and_is_valid_jsonschema():
    assert SCHEMA_PATH.exists(), "leaderboard/schema.json must be committed for the viewer"
    schema = json.loads(SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    # the in-code schema and the committed file agree
    assert schema == LEADERBOARD_SCHEMA


def test_leaderboard_schema_golden():
    matches = [MatchResult("alice", "bob", Outcome.A_WINS, match_id="m1")]
    submissions = {
        "alice": {"fingerprint": _fingerprint(), "identity": "octocat",
                  "bot_sha256": "a" * 64, "pr_url": "https://github.com/x/y/pull/1",
                  "logs_url": "https://x.github.io/logs/1"},
        "bob": {"fingerprint": _fingerprint("copilot-cli"), "identity": "hubot",
                "bot_sha256": "b" * 64, "pr_url": "https://github.com/x/y/pull/2",
                "logs_url": "https://x.github.io/logs/2"},
    }
    doc = build_leaderboard_doc(matches, submissions, updated_at="2026-07-15T18:00:00Z")

    # validates against the locked schema
    validate_leaderboard(doc)

    # required top-level shape
    assert doc["schema_version"] == 1
    assert doc["updated_at"] == "2026-07-15T18:00:00Z"
    assert isinstance(doc["rows"], list)

    row = next(r for r in doc["rows"] if r["identity"] == "octocat")
    required = {
        "rank", "elo", "rated", "match_count", "ci", "identity", "harness_name",
        "fingerprint_summary", "details", "bot_sha256", "fingerprint_probe_version",
        "pr_url", "logs_url",
    }
    assert required <= set(row)
    assert set(row["ci"]) == {"lo", "hi"}
    assert set(row["details"]) == {"skills", "mcps", "plugins", "unknown"}
    assert row["rank"] == 1  # alice won -> rank 1
    assert row["harness_name"] == "claude-code"


def test_ranks_are_dense_and_ordered():
    matches = [
        MatchResult("alice", "bob", Outcome.A_WINS, match_id="m1"),
        MatchResult("alice", "carol", Outcome.A_WINS, match_id="m2"),
    ]
    subs = {n: {"fingerprint": _fingerprint(), "identity": n, "bot_sha256": "c" * 64,
                "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l"}
            for n in ("alice", "bob", "carol")}
    doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
    ranks = [r["rank"] for r in sorted(doc["rows"], key=lambda r: r["rank"])]
    assert ranks == [1, 2, 3]
    elos = [r["elo"] for r in sorted(doc["rows"], key=lambda r: r["rank"])]
    assert elos == sorted(elos, reverse=True)  # rank 1 has highest ELO


def test_validator_rejects_missing_required_field():
    bad = {"schema_version": 1, "updated_at": "2026-07-15T18:00:00Z", "rows": [{"elo": 1500}]}
    with pytest.raises(jsonschema.ValidationError):
        validate_leaderboard(bad)


def test_validator_rejects_unknown_reason_enum():
    matches = [MatchResult("alice", "bob", Outcome.A_WINS, match_id="m1")]
    fp = _fingerprint()
    fp["unknown"] = [{"field": "x", "reason": "totally_made_up_reason"}]
    subs = {"alice": {"fingerprint": fp, "identity": "octocat", "bot_sha256": "a" * 64,
                      "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l"},
            "bob": {"fingerprint": _fingerprint(), "identity": "hubot", "bot_sha256": "b" * 64,
                    "pr_url": "https://github.com/x/y/pull/2", "logs_url": "https://x/l"}}
    doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
    with pytest.raises(jsonschema.ValidationError):
        validate_leaderboard(doc)
