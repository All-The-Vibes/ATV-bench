"""Tests for the leaderboard insights generator (demo Act 3).

`build_insights(rows)` turns ranked board rows into a short list of human-readable
insight strings tying fingerprint tags to ranking — the "listed insights from our
gstack plan" the demo shows next to the board. Pure function; RED before implementation.
"""
from __future__ import annotations

from atv_bench.leaderboard import build_insights


def _rows():
    return [
        {
            "rank": 1, "elo": 1675.0, "rated": True, "identity": "ada",
            "harness_name": "claude-code", "fingerprint_gstack": True,
            "match_count": 24, "wins": 21, "losses": 3, "draws": 0, "forfeits": 0,
            "details": {"skills": ["gstack", "tdd", "office-hours"], "mcps": ["github", "grafana"],
                        "plugins": ["compound-engineering"], "unknown": []},
        },
        {
            "rank": 2, "elo": 1500.0, "rated": True, "identity": "grace",
            "harness_name": "claude-code", "fingerprint_gstack": True,
            "match_count": 24, "wins": 12, "losses": 12, "draws": 0, "forfeits": 0,
            "details": {"skills": ["gstack"], "mcps": ["github"], "plugins": [], "unknown": []},
        },
        {
            "rank": 3, "elo": 1320.0, "rated": True, "identity": "linus",
            "harness_name": "copilot-cli", "fingerprint_gstack": False,
            "match_count": 24, "wins": 3, "losses": 21, "draws": 0, "forfeits": 0,
            "details": {"skills": [], "mcps": [], "plugins": [], "unknown": []},
        },
    ]


def test_build_insights_returns_nonempty_strings():
    out = build_insights(_rows())
    assert isinstance(out, list) and out
    assert all(isinstance(s, str) and s.strip() for s in out)


def test_build_insights_mentions_gstack_advantage():
    # gstack harnesses (avg of ada+grace = 1587.5) beat the non-gstack (linus = 1320).
    out = " ".join(build_insights(_rows())).lower()
    assert "gstack" in out


def test_build_insights_names_the_leader():
    out = " ".join(build_insights(_rows()))
    assert "ada" in out


def test_build_insights_handles_empty_board():
    # No rows -> a single graceful "no matches yet" style insight, never a crash.
    out = build_insights([])
    assert isinstance(out, list) and out
    assert all(isinstance(s, str) for s in out)
