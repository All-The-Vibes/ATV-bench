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


def test_rank_matches_display_order_stable_above_provisional():
    """Santa round-1 (Reviewer B): builder ranked purely by ELO, but the viewer
    demotes low-confidence rows below stable ones — so JSON rank 1 could render below
    others. The builder must assign rank in the SAME demoted order the viewer shows:
    stable (rated, >=5 matches) first, provisional/low-confidence last."""
    # 'newcomer' has a high ELO but only 3 matches (low-confidence); 'veteran' is
    # stable with a lower ELO. Display + rank must put veteran above newcomer.
    matches = (
        [MatchResult("veteran", f"filler{i}", Outcome.A_WINS, match_id=f"v{i}") for i in range(6)]
        + [MatchResult("newcomer", "veteran", Outcome.A_WINS, match_id="n1"),
           MatchResult("newcomer", "veteran", Outcome.A_WINS, match_id="n2"),
           MatchResult("newcomer", "veteran", Outcome.A_WINS, match_id="n3")]
    )
    names = {"veteran", "newcomer"} | {f"filler{i}" for i in range(6)}
    subs = {n: {"fingerprint": _fingerprint(), "identity": n, "bot_sha256": "d" * 64,
                "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l"}
            for n in names}
    doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
    by_id = {r["identity"]: r for r in doc["rows"]}
    # any stable (not low_confidence) rated row must rank above any low_confidence row
    stable_ranks = [r["rank"] for r in doc["rows"] if r["rated"] and not r["low_confidence"]]
    low_ranks = [r["rank"] for r in doc["rows"] if r["low_confidence"]]
    if stable_ranks and low_ranks:
        assert max(stable_ranks) < min(low_ranks), \
            "every stable row must rank above every low-confidence row"
    assert by_id["newcomer"]["low_confidence"] is True


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


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(document.cookie)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "  javascript:alert(1)",
    "ftp://internal/secret",
])
def test_validator_rejects_non_http_urls(bad_url):
    """Santa round-1 (both reviewers): a submission-controlled pr_url/logs_url of
    javascript: is a stored-XSS-on-click vector. The schema must reject any URL that
    is not http(s), on BOTH pr_url and logs_url."""
    matches = [MatchResult("alice", "bob", Outcome.A_WINS, match_id="m1")]
    for field in ("pr_url", "logs_url"):
        subs = {
            "alice": {"fingerprint": _fingerprint(), "identity": "octocat", "bot_sha256": "a" * 64,
                      "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l"},
            "bob": {"fingerprint": _fingerprint(), "identity": "hubot", "bot_sha256": "b" * 64,
                    "pr_url": "https://github.com/x/y/pull/2", "logs_url": "https://x/l"},
        }
        subs["alice"][field] = bad_url
        doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
        with pytest.raises(jsonschema.ValidationError):
            validate_leaderboard(doc)


def test_validator_accepts_http_and_https_urls():
    matches = [MatchResult("alice", "bob", Outcome.A_WINS, match_id="m1")]
    subs = {
        "alice": {"fingerprint": _fingerprint(), "identity": "octocat", "bot_sha256": "a" * 64,
                  "pr_url": "https://github.com/x/y/pull/1", "logs_url": "http://x.io/logs/1"},
        "bob": {"fingerprint": _fingerprint(), "identity": "hubot", "bot_sha256": "b" * 64,
                "pr_url": "https://github.com/x/y/pull/2", "logs_url": "https://x.io/logs/2"},
    }
    doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
    validate_leaderboard(doc)  # no raise


def test_variance_gate_marks_low_signal_rows():
    """R2-Fix D: the A/A variance gate must be WIRED into the published board, not
    dead code. A rated player whose ELO signal is below the gate's numeric threshold
    (too few matches / CI too wide) must be marked low_confidence in the row."""
    # alice has only 2 matches -> below the gate's min-match threshold -> low signal
    matches = [
        MatchResult("alice", "bob", Outcome.A_WINS, match_id="m1"),
        MatchResult("alice", "bob", Outcome.A_WINS, match_id="m2"),
    ]
    subs = {n: {"fingerprint": _fingerprint(), "identity": n, "bot_sha256": "e" * 64,
                "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l"}
            for n in ("alice", "bob")}
    doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
    alice = next(r for r in doc["rows"] if r["identity"] == "alice")
    assert alice["low_confidence"] is True  # gate fired: insufficient signal


def test_variance_gate_clears_high_signal_rows():
    # a player with many matches and a real spread clears the gate (not low_confidence)
    matches = [MatchResult("champ", f"opp{i}", Outcome.A_WINS, match_id=f"c{i}") for i in range(12)]
    names = {"champ"} | {f"opp{i}" for i in range(12)}
    subs = {n: {"fingerprint": _fingerprint(), "identity": n, "bot_sha256": "f" * 64,
                "pr_url": "https://github.com/x/y/pull/1", "logs_url": "https://x/l"}
            for n in names}
    doc = build_leaderboard_doc(matches, subs, updated_at="2026-07-15T18:00:00Z")
    champ = next(r for r in doc["rows"] if r["identity"] == "champ")
    assert champ["low_confidence"] is False
