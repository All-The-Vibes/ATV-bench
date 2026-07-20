"""Tests for the leaderboard insights generator (demo Act 3).

`build_insights(rows)` turns ranked League rows into short human-readable strings
without treating self-attested fingerprint tags as causal explanations of Elo.
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


def test_build_insights_mentions_gstack_only_as_self_attested_metadata():
    out = " ".join(build_insights(_rows())).lower()
    assert "gstack" in out
    assert "self-attested" in out
    assert "does not establish a harness effect" in out
    assert "average +" not in out


def test_build_insights_names_the_leader():
    out = " ".join(build_insights(_rows()))
    assert "ada" in out


def test_build_insights_handles_empty_board():
    # No rows -> a single graceful "no matches yet" style insight, never a crash.
    out = build_insights([])
    assert isinstance(out, list) and out
    assert all(isinstance(s, str) for s in out)


def test_build_insights_survives_nan_or_infinite_elo():
    # Corrupted/degenerate ELO (NaN or +/-inf) must not crash the board display.
    # round(float('nan')) / round(float('inf')) raise ValueError — build_insights
    # must guard so one bad row can't brick the demo Act 3.
    rows = [
        {"rank": 1, "elo": float("nan"), "identity": "nanbot",
         "harness_name": "claude-code", "fingerprint_gstack": True,
         "details": {"skills": ["gstack"], "mcps": [], "plugins": []}},
        {"rank": 2, "elo": float("inf"), "identity": "infbot",
         "harness_name": "copilot-cli", "fingerprint_gstack": False,
         "details": {"skills": [], "mcps": [], "plugins": []}},
    ]
    out = build_insights(rows)  # must not raise
    assert isinstance(out, list) and out
    assert all(isinstance(s, str) for s in out)
